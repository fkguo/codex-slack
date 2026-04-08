import asyncio
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import codex_threads

try:
    from codex_app_server_sdk.client import (
        _TurnSession,
        _extract_turn_id,
        _is_transport_error_event,
        _thread_config_to_params,
        _turn_overrides_to_params,
        make_error_response,
        make_result_response,
    )
    from codex_app_server_sdk.models import ConversationStep
    from codex_app_server_sdk.transport import CodexTransportError
    from codex_app_server_sdk.errors import CodexProtocolError, CodexTimeoutError
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency `codex-app-server-sdk`. "
        "Run `pip install -r requirements.txt` before starting codex-slack."
    ) from exc


REQUEST_USER_INPUT_METHOD = "item/tool/requestUserInput"


@dataclass(frozen=True)
class RuntimeUserInputQuestionOption:
    label: str
    description: str


@dataclass(frozen=True)
class RuntimeUserInputQuestion:
    id: str
    header: str
    question: str
    is_other: bool = False
    is_secret: bool = False
    options: list[RuntimeUserInputQuestionOption] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimeUserInputRequest:
    request_id: int | str
    thread_id: str
    turn_id: str
    item_id: str
    questions: list[RuntimeUserInputQuestion] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimeActiveTurn:
    session_id: str
    turn_id: str
    started_at: float


@dataclass(frozen=True)
class RuntimeTurnResult:
    session_id: str
    turn_id: str
    final_text: str
    steps: list[ConversationStep] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False


class AppServerRuntime:
    def __init__(self, config_factory):
        self._config_factory = config_factory
        self._guard = threading.Lock()
        self._loop_ready = threading.Event()
        self._loop = None
        self._thread = None
        self._client = None
        self._closed = False
        self._active_turns = {}
        self._active_turns_guard = threading.Lock()
        self._client_init_lock = None
        self._turn_user_input_handlers = {}
        self._last_client_diagnostics = ""

    def reset(self, timeout=10):
        with self._guard:
            loop = self._loop
            thread = self._thread
            closed = self._closed

        if closed or loop is None or thread is None:
            return

        future = asyncio.run_coroutine_threadsafe(self._reset_client_async(), loop)
        with suppress(Exception):
            future.result(timeout=timeout)

    def last_client_diagnostics(self):
        with self._guard:
            return self._last_client_diagnostics

    def get_active_turn(self, session_id) -> Optional[RuntimeActiveTurn]:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return None
        with self._active_turns_guard:
            return self._active_turns.get(normalized_session_id)

    def run_turn(
        self,
        *,
        session_id=None,
        input_items,
        thread_config=None,
        turn_overrides=None,
        collaboration_mode=None,
        heartbeat_seconds=None,
        on_turn_started: Optional[Callable[[str, str], None]] = None,
        on_step: Optional[Callable[[ConversationStep], None]] = None,
        on_heartbeat: Optional[Callable[[str, str, float], None]] = None,
        on_user_input_request: Optional[Callable[[RuntimeUserInputRequest], Any]] = None,
    ) -> RuntimeTurnResult:
        future = self._submit(
            self._run_turn_async(
                session_id=session_id,
                input_items=list(input_items or []),
                thread_config=thread_config,
                turn_overrides=turn_overrides,
                collaboration_mode=collaboration_mode,
                heartbeat_seconds=heartbeat_seconds,
                on_turn_started=on_turn_started,
                on_step=on_step,
                on_heartbeat=on_heartbeat,
                on_user_input_request=on_user_input_request,
            )
        )
        return future.result()

    def steer_turn(self, session_id, text):
        active_turn = self.get_active_turn(session_id)
        if not active_turn:
            raise RuntimeError("当前没有可 steer 的 runtime 活跃 turn。")
        return self.steer_active_turn(active_turn, text)

    def interrupt_turn(self, session_id):
        active_turn = self.get_active_turn(session_id)
        if not active_turn:
            raise RuntimeError("当前没有可中断的 runtime 活跃 turn。")
        return self.interrupt_active_turn(active_turn)

    def steer_active_turn(self, active_turn: RuntimeActiveTurn, text):
        future = self._submit(self._steer_turn_async(active_turn, text))
        future.result()
        return active_turn

    def interrupt_active_turn(self, active_turn: RuntimeActiveTurn):
        future = self._submit(self._interrupt_turn_async(active_turn))
        future.result()
        return active_turn

    def close(self):
        with self._guard:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread

        if loop is None or thread is None:
            return

        future = asyncio.run_coroutine_threadsafe(self._shutdown_async(), loop)
        with suppress(Exception):
            future.result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)

    def _submit(self, coro):
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def _ensure_loop(self):
        with self._guard:
            if self._closed:
                raise RuntimeError("runtime is already closed")
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop_worker, daemon=True)
                self._thread.start()

        self._loop_ready.wait()
        return self._loop

    def _loop_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        # The app-server client lives on this dedicated loop, so its init lock must too.
        self._client_init_lock = asyncio.Lock()
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                with suppress(Exception):
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            with suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _shutdown_async(self):
        await self._reset_client_async()

    async def _ensure_client_async(self):
        if self._client is not None:
            return self._client
        async with self._client_init_lock:
            if self._client is not None:
                return self._client
            config = self._config_factory()
            client = codex_threads.create_app_server_client(config)
            await client.start()
            await codex_threads.initialize_app_server_client(client, config)
            self._install_server_request_hook(client)
            self._client = client
            return client

    async def _reset_client_async(self):
        client = self._client
        self._client = None
        self._turn_user_input_handlers.clear()
        if client is None:
            return
        self._remember_client_diagnostics(client)
        with suppress(Exception):
            await client.close()

    def _remember_client_diagnostics(self, client, *, prefix=None):
        tail = codex_threads.get_client_stderr_tail(client)
        if not tail:
            return
        diagnostics = tail if not prefix else f"{prefix}\n{tail}"
        with self._guard:
            self._last_client_diagnostics = diagnostics

    def _install_server_request_hook(self, client):
        if getattr(client, "_codex_slack_request_user_input_hooked", False):
            return

        original_handler = client._handle_server_request
        runtime = self

        async def wrapped_handle_server_request(*, request_id, method, payload):
            if method == REQUEST_USER_INPUT_METHOD:
                return await runtime._handle_request_user_input_request(
                    client,
                    request_id=request_id,
                    payload=payload,
                )
            return await original_handler(
                request_id=request_id,
                method=method,
                payload=payload,
            )

        client._handle_server_request = wrapped_handle_server_request
        client._codex_slack_request_user_input_hooked = True

    async def _handle_request_user_input_request(self, client, *, request_id, payload):
        params = payload.get("params")
        if not isinstance(params, Mapping):
            error = make_error_response(
                request_id,
                -32602,
                f"{REQUEST_USER_INPUT_METHOD} received invalid params",
            )
            async with client._send_lock:
                await client._transport.send(error)
            return True

        try:
            request = self._parse_user_input_request(request_id=request_id, params=params)
        except CodexProtocolError as exc:
            error = make_error_response(request_id, -32602, str(exc))
            async with client._send_lock:
                await client._transport.send(error)
            return True

        handler = self._turn_user_input_handlers.get(request.turn_id)
        if handler is None:
            error = make_error_response(
                request_id,
                -32601,
                f"{REQUEST_USER_INPUT_METHOD} is not available for this turn",
            )
            async with client._send_lock:
                await client._transport.send(error)
            return True

        client._spawn_background_task(
            self._run_user_input_request_handler(
                client,
                request=request,
                handler=handler,
            )
        )
        return True

    def _parse_user_input_request(self, *, request_id, params) -> RuntimeUserInputRequest:
        thread_id = self._require_string_field(params, "threadId", REQUEST_USER_INPUT_METHOD)
        turn_id = self._require_string_field(params, "turnId", REQUEST_USER_INPUT_METHOD)
        item_id = self._require_string_field(params, "itemId", REQUEST_USER_INPUT_METHOD)
        questions_value = params.get("questions")
        if not isinstance(questions_value, list):
            raise CodexProtocolError(
                f"{REQUEST_USER_INPUT_METHOD} requires an array `questions` field"
            )

        questions = []
        for raw_question in questions_value:
            if not isinstance(raw_question, Mapping):
                raise CodexProtocolError(
                    f"{REQUEST_USER_INPUT_METHOD} question payload must be an object"
                )
            options = []
            options_value = raw_question.get("options")
            if options_value is not None:
                if not isinstance(options_value, list):
                    raise CodexProtocolError(
                        f"{REQUEST_USER_INPUT_METHOD} question options must be an array"
                    )
                for raw_option in options_value:
                    if not isinstance(raw_option, Mapping):
                        raise CodexProtocolError(
                            f"{REQUEST_USER_INPUT_METHOD} option payload must be an object"
                        )
                    options.append(
                        RuntimeUserInputQuestionOption(
                            label=self._require_string_field(
                                raw_option,
                                "label",
                                REQUEST_USER_INPUT_METHOD,
                            ),
                            description=self._require_string_field(
                                raw_option,
                                "description",
                                REQUEST_USER_INPUT_METHOD,
                            ),
                        )
                    )

            questions.append(
                RuntimeUserInputQuestion(
                    id=self._require_string_field(
                        raw_question,
                        "id",
                        REQUEST_USER_INPUT_METHOD,
                    ),
                    header=self._require_string_field(
                        raw_question,
                        "header",
                        REQUEST_USER_INPUT_METHOD,
                    ),
                    question=self._require_string_field(
                        raw_question,
                        "question",
                        REQUEST_USER_INPUT_METHOD,
                    ),
                    is_other=bool(raw_question.get("isOther")),
                    is_secret=bool(raw_question.get("isSecret")),
                    options=options,
                )
            )

        return RuntimeUserInputRequest(
            request_id=request_id,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            questions=questions,
        )

    async def _run_user_input_request_handler(self, client, *, request, handler):
        response_payload = {"answers": {}}
        try:
            response = handler(request)
            if asyncio.iscoroutine(response):
                response = await response
            response_payload = self._normalize_user_input_response(response, request=request)
        except Exception:
            response_payload = {"answers": {}}

        try:
            result = make_result_response(request.request_id, response_payload)
            async with client._send_lock:
                await client._transport.send(result)
        except (CodexProtocolError, CodexTransportError):
            return

    def _normalize_user_input_response(self, response, *, request):
        if response is None:
            return {"answers": {}}

        answers_value = response.get("answers") if isinstance(response, Mapping) else None
        if not isinstance(answers_value, Mapping):
            return {"answers": {}}

        normalized_answers = {}
        valid_question_ids = {question.id for question in request.questions}
        for question_id, raw_answer in answers_value.items():
            normalized_question_id = str(question_id or "").strip()
            if not normalized_question_id or normalized_question_id not in valid_question_ids:
                continue
            if not isinstance(raw_answer, Mapping):
                continue
            raw_answers = raw_answer.get("answers")
            if not isinstance(raw_answers, list):
                continue
            answers = [str(value).strip() for value in raw_answers if str(value or "").strip()]
            normalized_answers[normalized_question_id] = {"answers": answers}
        return {"answers": normalized_answers}

    def _require_string_field(self, params, field_name, method_name):
        value = params.get(field_name)
        if isinstance(value, str) and value:
            return value
        raise CodexProtocolError(f"{method_name} requires string field `{field_name}`")

    @staticmethod
    def _extract_final_text_from_session(session):
        last_unknown_phase_text = ""
        last_completed_agent_message_text = ""
        last_plan_text = ""

        for event in reversed(session.raw_events):
            if not isinstance(event, Mapping):
                continue
            if event.get("method") != "item/completed":
                continue
            params = event.get("params")
            if not isinstance(params, Mapping):
                continue
            item = params.get("item")
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            text = str(item.get("text") or "").strip()
            if item_type == "plan":
                if text and not last_plan_text:
                    last_plan_text = text
                continue
            if item_type != "agentMessage":
                continue
            if not text:
                continue
            if not last_completed_agent_message_text:
                last_completed_agent_message_text = text
            phase = item.get("phase")
            if phase == "final_answer":
                return text
            if phase is None and not last_unknown_phase_text:
                last_unknown_phase_text = text

        if last_completed_agent_message_text:
            return last_completed_agent_message_text
        if last_plan_text:
            return f"<proposed_plan>\n{last_plan_text.rstrip()}\n</proposed_plan>"
        return last_unknown_phase_text

    async def _read_turn_agent_message_async(
        self,
        client,
        *,
        thread_id,
        turn_id,
    ):
        try:
            response = await client.read_thread(thread_id, include_turns=True)
        except Exception:
            return None

        if not isinstance(response, Mapping):
            return None
        thread = response.get("thread")
        if not isinstance(thread, Mapping):
            return None
        turns = thread.get("turns")
        if not isinstance(turns, list):
            return None

        target_turn = None
        for turn in turns:
            if isinstance(turn, Mapping) and turn.get("id") == turn_id:
                target_turn = turn
                break
        if target_turn is None and turns:
            last_turn = turns[-1]
            if isinstance(last_turn, Mapping):
                target_turn = last_turn
        if not isinstance(target_turn, Mapping):
            return None

        items = target_turn.get("items")
        if not isinstance(items, list):
            return None

        final_message = None
        final_plan = None
        for item in items:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type == "agentMessage":
                text = str(item.get("text") or "").strip()
                if text:
                    final_message = text
            elif item_type == "plan":
                text = str(item.get("text") or "").strip()
                if text:
                    final_plan = text

        if final_message:
            return final_message
        if final_plan:
            return f"<proposed_plan>\n{final_plan.rstrip()}\n</proposed_plan>"
        return None

    async def _resolve_thread_async(self, session_id, thread_config):
        if session_id:
            attempt_count = 0
            last_exc = None
            max_attempts = max(
                1,
                int(
                    getattr(
                        self._config_factory(),
                        "resume_max_retries",
                        1,
                    )
                ),
            )
            while attempt_count < max_attempts:
                attempt_count += 1
                client = await self._ensure_client_async()
                try:
                    result = await self._resume_thread_async(
                        client,
                        session_id=session_id,
                        thread_config=thread_config,
                    )
                    return result
                except (CodexTimeoutError, CodexTransportError) as exc:
                    last_exc = exc
                    self._remember_client_diagnostics(
                        client,
                        prefix=(
                            f"thread/resume attempt {attempt_count}/{max_attempts} failed: "
                            f"{exc.__class__.__name__}: {exc}"
                        ),
                    )
                    await self._reset_client_async()
            raise last_exc
        else:
            client = await self._ensure_client_async()
            result = await client.start_thread(config=thread_config)
            return result.thread_id

    async def _resume_thread_async(self, client, *, session_id, thread_config):
        params = {"threadId": session_id}
        params.update(_thread_config_to_params(thread_config))
        config = self._config_factory()
        timeout = getattr(config, "resume_request_timeout", None) or getattr(
            config, "request_timeout", None
        )
        result = await client.request("thread/resume", params, timeout=timeout)
        return (result.get("thread") or {}).get("id") or result.get("threadId") or session_id

    async def _run_turn_async(
        self,
        *,
        session_id=None,
        input_items,
        thread_config=None,
        turn_overrides=None,
        collaboration_mode=None,
        heartbeat_seconds=None,
        on_turn_started=None,
        on_step=None,
        on_heartbeat=None,
        on_user_input_request=None,
    ) -> RuntimeTurnResult:
        client = None
        active_thread_id = None
        turn_id = None
        start_monotonic = time.monotonic()
        heartbeat_interval = None
        if heartbeat_seconds:
            heartbeat_interval = max(1.0, float(heartbeat_seconds))

        try:
            client = await self._ensure_client_async()
            active_thread_id = await self._resolve_thread_async(session_id, thread_config)

            turn_params = {
                "threadId": active_thread_id,
                "input": [dict(item) for item in (input_items or [])],
            }
            turn_params.update(_turn_overrides_to_params(turn_overrides))
            if collaboration_mode:
                turn_params["collaborationMode"] = dict(collaboration_mode)
            try:
                turn_result = await client.request("turn/start", turn_params)
            except CodexProtocolError as exc:
                if collaboration_mode and self._is_missing_experimental_capability_error(
                    exc,
                    "turn/start.collaborationMode",
                ):
                    turn_params.pop("collaborationMode", None)
                    turn_result = await client.request("turn/start", turn_params)
                else:
                    raise
            turn_id = _extract_turn_id(turn_result)
            if not turn_id:
                raise RuntimeError("turn/start succeeded but no turn id found")

            if on_user_input_request:
                self._turn_user_input_handlers[turn_id] = on_user_input_request

            active_turn = RuntimeActiveTurn(
                session_id=active_thread_id,
                turn_id=turn_id,
                started_at=time.time(),
            )
            with self._active_turns_guard:
                self._active_turns[active_thread_id] = active_turn

            if on_turn_started:
                on_turn_started(active_thread_id, turn_id)

            session = _TurnSession(thread_id=active_thread_id, turn_id=turn_id)
            interrupted = False

            while True:
                if session.failed:
                    failure_message = session.failure_message or "turn 已结束为 failed，但 app-server 没有提供更具体的错误信息。"
                    normalized_failure = failure_message.lower()
                    if "interrupt" in normalized_failure or "cancel" in normalized_failure:
                        interrupted = True
                        break
                    raise RuntimeError(failure_message)

                if session.completed:
                    break

                try:
                    event = await client._receive_turn_event(
                        turn_id,
                        inactivity_timeout=heartbeat_interval,
                    )
                except asyncio.TimeoutError:
                    if on_heartbeat:
                        on_heartbeat(
                            active_thread_id,
                            turn_id,
                            time.monotonic() - start_monotonic,
                        )
                    continue

                if _is_transport_error_event(event):
                    message = (
                        ((event.get("params") or {}).get("message"))
                        if isinstance(event, dict)
                        else None
                    )
                    raise CodexTransportError(
                        message or "app-server 连接中断，未收到更具体的传输层错误信息。"
                    )

                step_count_before = len(session.step_records)
                client._apply_event_to_session(session, event)

                for record in session.step_records[step_count_before:]:
                    if on_step:
                        on_step(record.step)

            final_text = self._extract_final_text_from_session(session)
            if not final_text:
                final_text = await self._read_turn_agent_message_async(
                    client,
                    thread_id=active_thread_id,
                    turn_id=turn_id,
                )
            if interrupted and not final_text:
                final_text = "当前 turn 已被中断。"

            with self._guard:
                self._last_client_diagnostics = ""

            return RuntimeTurnResult(
                session_id=active_thread_id,
                turn_id=turn_id,
                final_text=final_text,
                steps=[record.step for record in session.step_records],
                raw_events=list(session.raw_events),
                interrupted=interrupted,
            )
        except Exception as exc:
            if isinstance(exc, (CodexTransportError, CodexTimeoutError)):
                await self._reset_client_async()
            raise
        finally:
            if turn_id:
                self._turn_user_input_handlers.pop(turn_id, None)
            if active_thread_id and turn_id:
                with self._active_turns_guard:
                    current = self._active_turns.get(active_thread_id)
                    if current and current.turn_id == turn_id:
                        self._active_turns.pop(active_thread_id, None)

    async def _steer_turn_async(self, active_turn: RuntimeActiveTurn, text):
        client = await self._ensure_client_async()
        await client.steer_turn(
            thread_id=active_turn.session_id,
            expected_turn_id=active_turn.turn_id,
            input_items=[{"type": "text", "text": text}],
        )

    async def _interrupt_turn_async(self, active_turn: RuntimeActiveTurn):
        client = await self._ensure_client_async()
        await client.request(
            "turn/interrupt",
            {
                "threadId": active_turn.session_id,
                "turnId": active_turn.turn_id,
            },
        )

    @staticmethod
    def _is_missing_experimental_capability_error(exc, descriptor):
        message = str(exc or "").lower()
        return (
            isinstance(exc, CodexProtocolError)
            and "requires experimentalapi capability" in message
            and descriptor.lower() in message
        )
