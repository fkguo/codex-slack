import asyncio
import os
import subprocess
import threading
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Optional

try:
    from codex_app_server_sdk import CodexClient
    from codex_app_server_sdk.transport import CodexTransportError, StdioTransport
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency `codex-app-server-sdk`. "
        "Run `pip install -r requirements.txt` before starting codex-slack."
    ) from exc


DEFAULT_APP_SERVER_STDIO_LINE_LIMIT_BYTES = 32 * 1024 * 1024
DEFAULT_APP_SERVER_REQUEST_TIMEOUT_SECONDS = 90.0
DEFAULT_APP_SERVER_STDERR_TAIL_LINES = 80


@dataclass(frozen=True)
class CodexAppServerConfig:
    codex_bin: str
    workdir: str
    env: dict[str, str]
    line_limit_bytes: int = DEFAULT_APP_SERVER_STDIO_LINE_LIMIT_BYTES
    connect_timeout: float = 30.0
    request_timeout: float = DEFAULT_APP_SERVER_REQUEST_TIMEOUT_SECONDS
    resume_request_timeout: float = DEFAULT_APP_SERVER_REQUEST_TIMEOUT_SECONDS
    max_retries: int = 2
    resume_max_retries: int = 2
    stderr_tail_lines: int = DEFAULT_APP_SERVER_STDERR_TAIL_LINES


@dataclass(frozen=True)
class ConversationEvent:
    turn_id: str
    item_id: str
    role: str
    text: str


@dataclass(frozen=True)
class ProgressEvent:
    turn_id: str
    item_id: str
    phase: str
    text: str


@dataclass(frozen=True)
class ThreadSummary:
    thread_id: str
    preview: str
    cwd: Optional[str]
    updated_at: Optional[int]
    created_at: Optional[int]
    status_type: str
    source: Optional[str]
    name: Optional[str]


class WatchAnchorLostError(RuntimeError):
    pass


class LargePayloadStdioTransport(StdioTransport):
    """SDK stdio transport with a larger stdout line limit for huge thread/read payloads."""

    def __init__(
        self,
        command,
        *,
        cwd=None,
        env=None,
        connect_timeout=30.0,
        line_limit_bytes=None,
        stderr_tail_lines=None,
    ):
        super().__init__(
            command,
            cwd=cwd,
            env=env,
            connect_timeout=connect_timeout,
        )
        self._line_limit_bytes = int(line_limit_bytes or DEFAULT_APP_SERVER_STDIO_LINE_LIMIT_BYTES)
        self._stderr_tail = deque(
            maxlen=max(1, int(stderr_tail_lines or DEFAULT_APP_SERVER_STDERR_TAIL_LINES))
        )
        self._stderr_task = None

    async def connect(self) -> None:
        if self._proc is not None:
            return
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            
        try:
            self._proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *self._command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self._cwd,
                    env=self._env,
                    limit=self._line_limit_bytes,
                    **kwargs
                ),
                timeout=self._connect_timeout,
            )
        except Exception as exc:  # pragma: no cover
            raise CodexTransportError(
                f"failed to start stdio transport command: {self._command!r}"
            ) from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self):
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
        except Exception:
            return

    def stderr_tail_text(self, max_lines=20):
        if not self._stderr_tail:
            return ""
        tail = list(self._stderr_tail)[-max(1, int(max_lines)) :]
        return "\n".join(tail).strip()

    async def close(self) -> None:
        stderr_task = self._stderr_task
        self._stderr_task = None
        await super().close()
        if stderr_task is not None:
            stderr_task.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await stderr_task


def normalize_session_cwd(value):
    normalized = str(value or "").strip()
    return normalized or None


def read_field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def read_root(obj):
    if isinstance(obj, dict):
        return obj.get("root", obj)
    return getattr(obj, "root", obj)


def _run_coro_sync(coro_factory, *args, **kwargs):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        result = None
        exception = None
        def target():
            nonlocal result, exception
            try:
                result = asyncio.run(coro_factory(*args, **kwargs))
            except Exception as e:
                exception = e
        t = threading.Thread(target=target)
        t.start()
        t.join()
        if exception:
            raise exception
        return result
    else:
        return asyncio.run(coro_factory(*args, **kwargs))


def truncate_text(text, max_length=280):
    normalized = (text or "").strip()
    if len(normalized) <= max_length:
        return normalized
    if max_length <= 15:
        return normalized[:max_length]
    return normalized[: max_length - 14].rstrip() + "...<truncated>"


def create_app_server_client(config: CodexAppServerConfig):
    transport = LargePayloadStdioTransport(
        [config.codex_bin, "app-server"],
        cwd=config.workdir,
        env=config.env,
        line_limit_bytes=config.line_limit_bytes,
        connect_timeout=config.connect_timeout,
        stderr_tail_lines=config.stderr_tail_lines,
    )
    client = CodexClient(transport, request_timeout=config.request_timeout)
    client._codex_slack_transport = transport
    return client


def get_client_stderr_tail(client, max_lines=20):
    client_dict = getattr(client, "__dict__", None)
    transport = None
    if isinstance(client_dict, dict):
        transport = client_dict.get("_codex_slack_transport")
        if transport is None:
            transport = client_dict.get("_transport")
    if transport is None or not hasattr(transport, "stderr_tail_text"):
        return ""
    try:
        return transport.stderr_tail_text(max_lines=max_lines)
    except Exception:
        return ""


def build_initialize_params():
    return {
        "capabilities": {
            "experimentalApi": True,
        }
    }


async def initialize_app_server_client(client, config: CodexAppServerConfig):
    return await client.initialize(
        params=build_initialize_params(),
        timeout=config.request_timeout,
    )


async def read_thread_response_async(config: CodexAppServerConfig, session_id, *, include_turns=True):
    client = create_app_server_client(config)
    await client.start()
    await initialize_app_server_client(client, config)
    try:
        return await client.read_thread(session_id, include_turns=include_turns)
    finally:
        with suppress(Exception):
            await client.close()


def read_thread_response(config: CodexAppServerConfig, session_id, *, include_turns=True):
    last_error = None
    for _attempt in range(config.max_retries):
        try:
            return _run_coro_sync(
                read_thread_response_async,
                config,
                session_id,
                include_turns=include_turns,
            )
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"读取 thread 对话失败: {last_error}")


async def list_threads_async(
    config: CodexAppServerConfig,
    *,
    archived=None,
    cursor=None,
    cwd=None,
    limit=None,
    sort_key="updated_at",
    sort_direction="desc",
):
    client = create_app_server_client(config)
    await client.start()
    await initialize_app_server_client(client, config)
    try:
        return await client.list_threads(
            archived=archived,
            cursor=cursor,
            cwd=cwd,
            limit=limit,
            sort_key=sort_key,
            sort_direction=sort_direction,
        )
    finally:
        with suppress(Exception):
            await client.close()


def list_threads(
    config: CodexAppServerConfig,
    *,
    archived=None,
    cursor=None,
    cwd=None,
    limit=None,
    sort_key="updated_at",
    sort_direction="desc",
):
    last_error = None
    for _attempt in range(config.max_retries):
        try:
            return _run_coro_sync(
                list_threads_async,
                config,
                archived=archived,
                cursor=cursor,
                cwd=cwd,
                limit=limit,
                sort_key=sort_key,
                sort_direction=sort_direction,
            )
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"读取 thread 列表失败: {last_error}")


async def set_thread_name_async(config: CodexAppServerConfig, session_id, name):
    client = create_app_server_client(config)
    await client.start()
    await initialize_app_server_client(client, config)
    try:
        return await client.set_thread_name(session_id, name)
    finally:
        with suppress(Exception):
            await client.close()


def set_thread_name(config: CodexAppServerConfig, session_id, name):
    last_error = None
    for _attempt in range(config.max_retries):
        try:
            return _run_coro_sync(set_thread_name_async, config, session_id, name)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"更新 thread 名称失败: {last_error}")


def normalize_thread_title(title):
    normalized = str(title or "").strip()
    if not normalized:
        return None
    return normalized


def rename_thread(config: CodexAppServerConfig, session_id, title):
    normalized_title = normalize_thread_title(title)
    if not session_id:
        raise RuntimeError("当前还没有可重命名的 session。")
    if not normalized_title:
        raise RuntimeError("`name` 后面需要一个非空标题，例如 `name fix flaky test`。")
    set_thread_name(config, session_id, normalized_title)
    return normalized_title


async def interrupt_turn_async(config: CodexAppServerConfig, thread_id, turn_id):
    client = create_app_server_client(config)
    await client.start()
    await initialize_app_server_client(client, config)
    try:
        return await client.request(
            "turn/interrupt",
            {
                "threadId": thread_id,
                "turnId": turn_id,
            },
        )
    finally:
        with suppress(Exception):
            await client.close()


def interrupt_turn(config: CodexAppServerConfig, thread_id, turn_id):
    last_error = None
    for _attempt in range(config.max_retries):
        try:
            return _run_coro_sync(interrupt_turn_async, config, thread_id, turn_id)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"中断 turn 失败: {last_error}")


async def steer_turn_async(config: CodexAppServerConfig, thread_id, expected_turn_id, input_items):
    client = create_app_server_client(config)
    await client.start()
    await initialize_app_server_client(client, config)
    try:
        return await client.steer_turn(
            thread_id=thread_id,
            expected_turn_id=expected_turn_id,
            input_items=input_items,
        )
    finally:
        with suppress(Exception):
            await client.close()


def steer_turn(config: CodexAppServerConfig, thread_id, expected_turn_id, input_items):
    last_error = None
    for _attempt in range(config.max_retries):
        try:
            return _run_coro_sync(steer_turn_async, config, thread_id, expected_turn_id, input_items)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"追加 steer 输入失败: {last_error}")


def extract_thread_cwd(thread_read_response):
    thread = read_field(thread_read_response, "thread", thread_read_response)
    return normalize_session_cwd(read_field(thread, "cwd"))


def extract_thread_summaries(thread_list_response):
    data = read_field(thread_list_response, "data", []) or []
    summaries = []
    for item in data:
        status = read_field(item, "status", {}) or {}
        summaries.append(
            ThreadSummary(
                thread_id=read_field(item, "id", "") or "",
                preview=(read_field(item, "preview", "") or "").strip(),
                cwd=normalize_session_cwd(read_field(item, "cwd")),
                updated_at=read_field(item, "updatedAt"),
                created_at=read_field(item, "createdAt"),
                status_type=(read_field(status, "type", "") or "").strip() or "unknown",
                source=read_field(item, "source") or read_field(item, "sourceKind"),
                name=(read_field(item, "name", "") or "").strip() or None,
            )
        )
    return summaries


def format_user_input(user_input):
    root = read_root(user_input)
    input_type = read_field(root, "type")
    if input_type == "text":
        return (read_field(root, "text", "") or "").strip()
    if input_type == "image":
        return "[image]"
    if input_type == "localImage":
        return f"[local image: {read_field(root, 'path', '-') or '-'}]"
    if input_type == "skill":
        return f"[skill: {read_field(root, 'name', '-') or '-'}]"
    if input_type == "mention":
        return f"[mention: {read_field(root, 'name', '-') or '-'}]"
    return ""


def format_user_message_content(content_items):
    parts = []
    for item in content_items or []:
        part = format_user_input(item)
        if part:
            parts.append(part)
    return "\n".join(parts).strip()


def is_final_answer_phase(phase):
    return phase == "final_answer"


def is_progress_phase(phase):
    return bool(phase) and phase != "final_answer"


def extract_conversation_events(thread_read_response):
    thread = read_field(thread_read_response, "thread", thread_read_response)
    events = []
    fallback_index = 0

    for turn in read_field(thread, "turns", []) or []:
        turn_id = read_field(turn, "id") or f"turn-{fallback_index}"
        for item in read_field(turn, "items", []) or []:
            root = read_root(item)
            item_type = read_field(root, "type")
            item_id = read_field(root, "id") or f"{turn_id}:item-{fallback_index}"
            fallback_index += 1

            if item_type == "userMessage":
                text = format_user_message_content(read_field(root, "content", []) or [])
                if text:
                    events.append(ConversationEvent(turn_id=turn_id, item_id=item_id, role="user", text=text))
                continue

            if item_type != "agentMessage":
                continue

            if not is_final_answer_phase(read_field(root, "phase")):
                continue

            text = (read_field(root, "text", "") or "").strip()
            if text:
                events.append(ConversationEvent(turn_id=turn_id, item_id=item_id, role="assistant", text=text))

    return events


def extract_progress_events(thread_read_response):
    thread = read_field(thread_read_response, "thread", thread_read_response)
    events = []
    fallback_index = 0

    for turn in read_field(thread, "turns", []) or []:
        turn_id = read_field(turn, "id") or f"turn-{fallback_index}"
        for item in read_field(turn, "items", []) or []:
            root = read_root(item)
            item_type = read_field(root, "type")
            item_id = read_field(root, "id") or f"{turn_id}:progress-{fallback_index}"
            fallback_index += 1

            if item_type != "agentMessage":
                continue

            phase = read_field(root, "phase")
            if not is_progress_phase(phase):
                continue

            text = (read_field(root, "text", "") or "").strip()
            if text:
                events.append(ProgressEvent(turn_id=turn_id, item_id=item_id, phase=phase, text=text))

    return events


def get_event_key(event):
    return (event.turn_id, event.item_id)


def get_recent_turn_events(events):
    if not events:
        return []
    last_turn_id = events[-1].turn_id
    return [event for event in events if event.turn_id == last_turn_id]


def get_latest_completed_turn_events(events):
    if not events:
        return []

    grouped_turns = []
    current_turn_id = None
    current_events = []
    for event in events:
        if event.turn_id != current_turn_id:
            if current_events:
                grouped_turns.append(current_events)
            current_turn_id = event.turn_id
            current_events = [event]
        else:
            current_events.append(event)
    if current_events:
        grouped_turns.append(current_events)

    for turn_events in reversed(grouped_turns):
        if any(event.role == "assistant" for event in turn_events):
            return turn_events
    return []


def get_events_after_key(events, last_key):
    if last_key is None:
        return list(events)

    for index, event in enumerate(events):
        if get_event_key(event) == last_key:
            return events[index + 1 :]

    raise WatchAnchorLostError(f"watch anchor {last_key!r} is no longer present in the current thread view")


def format_conversation_events(events, heading=None):
    if not events:
        return "当前 thread 还没有可显示的对话内容。"

    blocks = []
    if heading:
        blocks.append(heading)

    for event in events:
        label = "User" if event.role == "user" else "Codex"
        quoted_text = "\n".join(
            f"> {line}" if line else ">"
            for line in (event.text or "").splitlines()
        ).strip()
        blocks.append(f"*{label}*\n{quoted_text}")

    return "\n\n".join(blocks).strip()


def build_watch_bootstrap(config: CodexAppServerConfig, session_id):
    events = extract_conversation_events(read_thread_response(config, session_id))
    bootstrap_events = get_latest_completed_turn_events(events) or get_recent_turn_events(events)
    last_event_key = get_event_key(bootstrap_events[-1]) if bootstrap_events else None
    return format_conversation_events(bootstrap_events, heading="最近一轮对话:"), last_event_key


def capture_progress_baseline(config: CodexAppServerConfig, session_id):
    try:
        progress_events = extract_progress_events(read_thread_response(config, session_id))
    except Exception:
        return {}
    return {event.item_id: event.text for event in progress_events}


def format_progress_message(text):
    quoted_text = "\n".join(
        f"> {line}" if line else ">"
        for line in (text or "").splitlines()
    ).strip()
    return f"*Codex Progress*\n{quoted_text}"


def build_progress_messages(progress_events, previous_text_by_item_id):
    messages = []

    for event in progress_events:
        previous_text = previous_text_by_item_id.get(event.item_id)
        current_text = event.text
        if previous_text == current_text:
            continue

        display_text = current_text
        if previous_text and current_text.startswith(previous_text):
            delta_text = current_text[len(previous_text) :].strip()
            if not delta_text:
                previous_text_by_item_id[event.item_id] = current_text
                continue
            display_text = delta_text
        else:
            display_text = truncate_text(current_text, max_length=1200)

        previous_text_by_item_id[event.item_id] = current_text
        messages.append(format_progress_message(display_text))

    return messages
