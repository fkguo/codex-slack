import threading
import time
from dataclasses import dataclass
from typing import Optional

from codex_threads import (
    ThreadSummary,
    extract_thread_summaries,
    list_threads,
)


DEFAULT_SELECTION_TTL_SECONDS = 15 * 60
DEFAULT_RECENT_LIMIT = 10


@dataclass(frozen=True)
class SessionSelectionSnapshot:
    thread_ids: tuple[str, ...]
    created_at: int


class SessionSelectionCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._snapshots = {}

    def put(self, thread_key, thread_ids):
        with self._lock:
            self._snapshots[thread_key] = SessionSelectionSnapshot(
                thread_ids=tuple(thread_ids),
                created_at=int(time.time()),
            )

    def get(self, thread_key) -> Optional[SessionSelectionSnapshot]:
        with self._lock:
            return self._snapshots.get(thread_key)

    def clear(self, thread_key):
        with self._lock:
            self._snapshots.pop(thread_key, None)


def cache_thread_summaries(cache: SessionSelectionCache, thread_key: str, summaries: list[ThreadSummary]):
    cache.put(thread_key, [summary.thread_id for summary in summaries if summary.thread_id])


def is_snapshot_fresh(snapshot: Optional[SessionSelectionSnapshot], ttl_seconds=DEFAULT_SELECTION_TTL_SECONDS):
    if snapshot is None:
        return False
    return (int(time.time()) - snapshot.created_at) <= max(1, int(ttl_seconds))


def resolve_recent_index(snapshot: Optional[SessionSelectionSnapshot], index: int, ttl_seconds=DEFAULT_SELECTION_TTL_SECONDS):
    if not is_snapshot_fresh(snapshot, ttl_seconds=ttl_seconds):
        raise RuntimeError("最近一次 recent/sessions 列表已经过期，请先重新发送 `recent` 或 `sessions`。")

    if index < 1 or index > len(snapshot.thread_ids):
        raise RuntimeError(f"`attach recent {index}` 超出可选范围，请先重新查看 `recent`。")

    return snapshot.thread_ids[index - 1]


def parse_recent_index(raw_index):
    if isinstance(raw_index, int):
        index = raw_index
    else:
        normalized = str(raw_index or "").strip()
        if not normalized:
            raise RuntimeError("请使用 `attach recent <n>`，例如 `attach recent 2`。")
        if not normalized.isdigit():
            raise RuntimeError(f"`attach recent {normalized}` 里的序号无效，请使用正整数。")
        index = int(normalized)

    if index <= 0:
        raise RuntimeError("`attach recent <n>` 的序号必须从 1 开始。")
    return index


def resolve_recent_selector(
    snapshot: Optional[SessionSelectionSnapshot],
    selector,
    ttl_seconds=DEFAULT_SELECTION_TTL_SECONDS,
):
    return resolve_recent_index(snapshot, parse_recent_index(selector), ttl_seconds=ttl_seconds)


def fetch_recent_thread_summaries(
    config,
    *,
    cwd: Optional[str],
    include_all: bool = False,
    limit: int = DEFAULT_RECENT_LIMIT,
    archived: bool = False,
):
    safe_limit = max(1, int(limit))
    response = list_threads(
        config,
        archived=archived,
        cwd=None if include_all else (cwd or None),
        limit=safe_limit,
        sort_key="updated_at",
        sort_direction="desc",
    )
    summaries = extract_thread_summaries(response)
    return [summary for summary in summaries if summary.thread_id]


def _format_updated_at(updated_at):
    if not updated_at:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated_at))


def format_thread_summaries(
    summaries: list[ThreadSummary],
    *,
    heading: Optional[str] = None,
    current_session_id: Optional[str] = None,
):
    lines = []
    if heading:
        lines.append(heading)
        lines.append("")

    if not summaries:
        lines.append("当前没有可显示的 Codex sessions。")
        return "\n".join(lines).strip()

    for index, summary in enumerate(summaries, start=1):
        title = summary.name or summary.preview or "(untitled)"
        current_marker = " (current)" if current_session_id and summary.thread_id == current_session_id else ""
        source_value = summary.source or "-"
        lines.append(
            f"{index}. `{summary.thread_id}`{current_marker} | {title} | cwd=`{summary.cwd or '-'}` | "
            f"updated=`{_format_updated_at(summary.updated_at)}` | status=`{summary.status_type}` | source=`{source_value}`"
        )

    return "\n".join(lines).strip()
