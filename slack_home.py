from typing import Mapping, Optional, Sequence

MAX_BLOCK_TEXT_LENGTH = 3000
MAX_ROW_LABEL_LENGTH = 120
MAX_ROW_TITLE_LENGTH = 160
MAX_ROW_CWD_LENGTH = 120
MAX_ROW_STATUS_LENGTH = 180

MRKDWN_TEXT_REPLACEMENTS = str.maketrans(
    {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "*": "∗",
        "_": "＿",
        "~": "∼",
        "`": "ˋ",
    }
)

MRKDWN_CODE_REPLACEMENTS = str.maketrans(
    {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "`": "ˋ",
    }
)


def _as_text(value, default="-"):
    normalized = str(value or "").strip()
    return normalized or default


def _as_optional_text(value):
    normalized = str(value or "").strip()
    return normalized or None


def _truncate_text(value, max_length):
    text = str(value or "")
    if max_length is None or max_length <= 0 or len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    return text[: max_length - 3].rstrip() + "..."


def _as_inline_text(value, default="-", max_length=None):
    normalized = _as_optional_text(value)
    if not normalized:
        return default
    return _truncate_text(" ".join(normalized.split()), max_length)


def _escape_mrkdwn_text(value, default="-", max_length=None):
    return _as_inline_text(value, default=default, max_length=max_length).translate(MRKDWN_TEXT_REPLACEMENTS)


def _escape_mrkdwn_code(value, default="-", max_length=None):
    return _truncate_text(_as_text(value, default=default), max_length).translate(MRKDWN_CODE_REPLACEMENTS)


def _as_rows(rows):
    if not rows:
        return []
    normalized_rows = []
    for row in rows:
        if isinstance(row, Mapping):
            normalized_rows.append(dict(row))
    return normalized_rows


def _binding_row_text(row, index):
    label = _escape_mrkdwn_text(
        row.get("label"),
        default=f"Binding {index}",
        max_length=MAX_ROW_LABEL_LENGTH,
    )
    session_id = _escape_mrkdwn_code(row.get("session_id"), max_length=120)
    mode = _escape_mrkdwn_code(row.get("mode"), max_length=40)
    cwd = _escape_mrkdwn_code(row.get("cwd"), max_length=MAX_ROW_CWD_LENGTH)
    updated_at = _escape_mrkdwn_code(row.get("updated_at"), max_length=64)
    status_text = _escape_mrkdwn_text(
        row.get("status_text"),
        default="",
        max_length=MAX_ROW_STATUS_LENGTH,
    )
    lines = [
        f"*{index}. {label}*",
        f"`{session_id}` | mode=`{mode}`",
        f"cwd=`{cwd}` | updated=`{updated_at}`",
    ]
    if status_text:
        lines.append(f"_{status_text}_")
    return "\n".join(lines)


def _recent_row_text(row, index):
    label = _escape_mrkdwn_text(
        row.get("label"),
        default=f"Session {index}",
        max_length=MAX_ROW_LABEL_LENGTH,
    )
    thread_id = _escape_mrkdwn_code(row.get("thread_id"), max_length=120)
    title = _escape_mrkdwn_text(
        row.get("title"),
        default="(untitled)",
        max_length=MAX_ROW_TITLE_LENGTH,
    )
    cwd = _escape_mrkdwn_code(row.get("cwd"), max_length=MAX_ROW_CWD_LENGTH)
    status = _escape_mrkdwn_code(row.get("status"), max_length=40)
    status_text = _escape_mrkdwn_text(
        row.get("status_text"),
        default="",
        max_length=MAX_ROW_STATUS_LENGTH,
    )
    lines = [
        f"*{index}. {label}*",
        f"`{thread_id}` | {title}",
        f"cwd=`{cwd}` | status=`{status}`",
    ]
    if status_text:
        lines.append(f"_{status_text}_")
    return "\n".join(lines)


def _build_row_section(text, row):
    safe_text = _truncate_text(text, MAX_BLOCK_TEXT_LENGTH)
    section = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": safe_text},
    }
    action_id = _as_optional_text(row.get("action_id"))
    action_value = _as_optional_text(row.get("action_value"))
    if action_id and action_value:
        section["accessory"] = {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": _as_text(row.get("action_text"), default="Action")},
            "value": action_value,
        }
    return section


def _append_rich_rows(blocks, *, title, rows, row_renderer, empty_text):
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": title}})
    if not rows:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": empty_text}})
        return
    for index, row in enumerate(rows, start=1):
        blocks.append(_build_row_section(row_renderer(row, index), row))


def _append_legacy_summary(blocks, *, title, summary):
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": title}})
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate_text(_as_text(summary, default="-"), MAX_BLOCK_TEXT_LENGTH),
            },
        }
    )


def _append_context_blocks(blocks, lines):
    pending = []
    current_length = 0
    max_chunk_length = MAX_BLOCK_TEXT_LENGTH

    for line in lines:
        normalized_line = _truncate_text(_as_text(line, default=""), max_chunk_length)
        projected = len(normalized_line) if not pending else current_length + 1 + len(normalized_line)
        if pending and projected > max_chunk_length:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "\n".join(pending)}],
                }
            )
            pending = [normalized_line]
            current_length = len(normalized_line)
            continue
        pending.append(normalized_line)
        current_length = projected

    if pending:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "\n".join(pending)}],
            }
        )


def format_binding_summary_rows(rows):
    normalized_rows = _as_rows(rows)
    if not normalized_rows:
        return "_No bindings yet._\nUse `fresh ...` or `attach <session_id>` in a Slack thread."

    lines = []
    for index, row in enumerate(normalized_rows, start=1):
        lines.append(_binding_row_text(row, index))
    return "\n".join(lines)


def format_recent_sessions_rows(rows):
    normalized_rows = _as_rows(rows)
    if not normalized_rows:
        return "_No recent sessions found._\nStart one with `fresh ...` in DM or `@bot ...` in a channel."

    lines = []
    for index, row in enumerate(normalized_rows, start=1):
        lines.append(_recent_row_text(row, index))
    return "\n".join(lines)


def build_home_view(
    *,
    default_workdir: str,
    default_model: str,
    default_effort: str,
    bindings_summary: str,
    recent_sessions_summary: str,
    help_text: Optional[str] = None,
    bindings_rows: Optional[Sequence[Mapping[str, object]]] = None,
    recent_sessions_rows: Optional[Sequence[Mapping[str, object]]] = None,
    quick_hints: Optional[Sequence[str]] = None,
):
    normalized_bindings_rows = _as_rows(bindings_rows)
    normalized_recent_rows = _as_rows(recent_sessions_rows)
    hint_lines = [line for line in (quick_hints or []) if _as_optional_text(line)]
    hint_text = _as_optional_text(help_text)
    subtitle_lines = [
        "*Operator Dashboard*",
        "Use Home for quick visibility and to manage your Slack thread bindings.",
    ]
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "codex-slack"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(subtitle_lines)},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Model*\n`{_as_text(default_model)}`"},
                {"type": "mrkdwn", "text": f"*Effort*\n`{_as_text(default_effort)}`"},
                {"type": "mrkdwn", "text": f"*Default Workdir*\n`{_as_text(default_workdir)}`"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "home_refresh",
                    "text": {"type": "plain_text", "text": "Refresh"},
                    "value": "refresh",
                }
            ],
        },
        {"type": "divider"},
    ]

    if bindings_rows is not None:
        _append_rich_rows(
            blocks,
            title="Your Slack Thread Bindings",
            rows=normalized_bindings_rows,
            row_renderer=_binding_row_text,
            empty_text="_No bindings yet._\nUse `fresh ...` or `attach <session_id>` in a Slack thread.",
        )
    else:
        _append_legacy_summary(
            blocks,
            title="Your Slack Thread Bindings",
            summary=bindings_summary,
        )

    blocks.append({"type": "divider"})

    if recent_sessions_rows is not None:
        _append_rich_rows(
            blocks,
            title="Recent Codex Sessions",
            rows=normalized_recent_rows,
            row_renderer=_recent_row_text,
            empty_text="_No recent sessions found._\nStart one with `fresh ...` in DM or `@bot ...` in a channel.",
        )
    else:
        _append_legacy_summary(
            blocks,
            title="Recent Codex Sessions",
            summary=recent_sessions_summary,
        )

    if hint_lines or hint_text:
        context_lines = []
        for line in hint_lines:
            context_lines.append(f"- {line}")
        if hint_text:
            context_lines.append(hint_text)
        blocks.append({"type": "divider"})
        _append_context_blocks(blocks, context_lines)
    return {
        "type": "home",
        "blocks": blocks,
    }
