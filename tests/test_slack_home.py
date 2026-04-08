import unittest
from unittest.mock import patch

import server
import slack_home
from codex_threads import ThreadSummary


class DummyViewClient:
    def __init__(self):
        self.calls = []

    def views_publish(self, **kwargs):
        self.calls.append(kwargs)


class SlackHomeViewTests(unittest.TestCase):
    def test_format_binding_summary_rows_empty(self):
        text = slack_home.format_binding_summary_rows([])
        self.assertIn("No bindings yet", text)

    def test_format_recent_sessions_rows_empty(self):
        text = slack_home.format_recent_sessions_rows([])
        self.assertIn("No recent sessions found", text)

    def test_build_home_view_contains_refresh_action(self):
        view = slack_home.build_home_view(
            default_workdir="/tmp/project",
            default_model="gpt-5.4",
            default_effort="xhigh",
            bindings_summary="binding summary",
            recent_sessions_summary="recent summary",
            help_text="help",
        )
        self.assertEqual(view["type"], "home")
        action_ids = [
            element.get("action_id")
            for block in view["blocks"]
            if block.get("type") == "actions"
            for element in block.get("elements", [])
        ]
        self.assertIn("home_refresh", action_ids)
        header_texts = [
            block.get("text", {}).get("text", "")
            for block in view["blocks"]
            if block.get("type") == "header"
        ]
        self.assertIn("codex-slack", header_texts)

    def test_build_home_view_keeps_legacy_summary_inputs(self):
        view = slack_home.build_home_view(
            default_workdir="/tmp/project",
            default_model="gpt-5.4",
            default_effort="xhigh",
            bindings_summary="legacy bindings",
            recent_sessions_summary="legacy recent",
            help_text="legacy help",
        )
        section_texts = [
            block.get("text", {}).get("text", "")
            for block in view["blocks"]
            if block.get("type") == "section"
        ]
        self.assertIn("legacy bindings", "\n".join(section_texts))
        self.assertIn("legacy recent", "\n".join(section_texts))
        context_text = "\n".join(
            element.get("text", "")
            for block in view["blocks"]
            if block.get("type") == "context"
            for element in block.get("elements", [])
        )
        self.assertIn("legacy help", context_text)

    def test_build_home_view_renders_rich_rows_with_actions(self):
        view = slack_home.build_home_view(
            default_workdir="/tmp/project",
            default_model="gpt-5.4",
            default_effort="xhigh",
            bindings_summary="ignored",
            recent_sessions_summary="ignored",
            bindings_rows=[
                {
                    "label": "DM Control",
                    "session_id": "sess-1",
                    "mode": "observe",
                    "cwd": "/tmp/project",
                    "updated_at": "2026-04-07 10:00:00",
                    "status_text": "Direct Message",
                    "action_id": "binding_rename_open",
                    "action_text": "Rename",
                    "action_value": "{\"thread_key\":\"D1:1\",\"session_id\":\"sess-1\"}",
                }
            ],
            recent_sessions_rows=[
                {
                    "label": "Recent 1",
                    "thread_id": "thr-1",
                    "title": "Fix flaky tests",
                    "cwd": "/tmp/project",
                    "status": "idle",
                }
            ],
            quick_hints=["Use takeover when you need write access."],
        )
        section_texts = [
            block.get("text", {}).get("text", "")
            for block in view["blocks"]
            if block.get("type") == "section"
        ]
        self.assertIn("DM Control", "\n".join(section_texts))
        self.assertIn("Fix flaky tests", "\n".join(section_texts))
        buttons = [
            element
            for block in view["blocks"]
            if block.get("type") == "actions"
            for element in block.get("elements", [])
        ]
        self.assertTrue(any(btn.get("action_id") == "home_refresh" for btn in buttons))
        accessory_buttons = [
            block.get("accessory", {})
            for block in view["blocks"]
            if block.get("type") == "section" and block.get("accessory")
        ]
        self.assertTrue(any(btn.get("action_id") == "binding_rename_open" for btn in accessory_buttons))
        context_text = "\n".join(
            element.get("text", "")
            for block in view["blocks"]
            if block.get("type") == "context"
            for element in block.get("elements", [])
        )
        self.assertIn("Use takeover when you need write access.", context_text)

    def test_build_home_view_collapses_multiline_binding_labels(self):
        view = slack_home.build_home_view(
            default_workdir="/tmp/project",
            default_model="gpt-5.4",
            default_effort="xhigh",
            bindings_summary="ignored",
            recent_sessions_summary="ignored",
            bindings_rows=[
                {
                    "label": "Three-body analyticity\n/Users/fkg/Coding/Agents/ThreeBody_Analytic",
                    "session_id": "sess-1",
                    "mode": "control",
                    "cwd": "/tmp/project",
                    "updated_at": "2026-04-07 16:12:42",
                    "status_text": "Direct\nMessage",
                    "action_id": "binding_rename_open",
                    "action_text": "Rename",
                    "action_value": "{\"thread_key\":\"D1:1\",\"session_id\":\"sess-1\"}",
                }
            ],
            recent_sessions_rows=[],
        )
        section_texts = [
            block.get("text", {}).get("text", "")
            for block in view["blocks"]
            if block.get("type") == "section"
        ]
        joined = "\n".join(section_texts)
        self.assertIn("*1. Three-body analyticity /Users/fkg/Coding/Agents/ThreeBody＿Analytic*", joined)
        self.assertIn("_Direct Message_", joined)

    def test_build_home_view_sanitizes_mrkdwn_control_characters_in_labels(self):
        view = slack_home.build_home_view(
            default_workdir="/tmp/project",
            default_model="gpt-5.4",
            default_effort="xhigh",
            bindings_summary="ignored",
            recent_sessions_summary="ignored",
            bindings_rows=[
                {
                    "label": "alpha *beta* _gamma_ `delta` <tag>",
                    "session_id": "sess`-1",
                    "mode": "con<trol>",
                    "cwd": "/tmp/<project>`",
                    "updated_at": "2026-04-07 16:12:42",
                    "status_text": "Direct _Message_",
                    "action_id": "binding_rename_open",
                    "action_text": "Rename",
                    "action_value": "{\"thread_key\":\"D1:1\",\"session_id\":\"sess-1\"}",
                }
            ],
            recent_sessions_rows=[],
        )
        section_texts = [
            block.get("text", {}).get("text", "")
            for block in view["blocks"]
            if block.get("type") == "section"
        ]
        joined = "\n".join(section_texts)
        self.assertIn("*1. alpha ∗beta∗ ＿gamma＿ ˋdeltaˋ &lt;tag&gt;*", joined)
        self.assertIn("`sessˋ-1` | mode=`con&lt;trol&gt;`", joined)
        self.assertIn("cwd=`/tmp/&lt;project&gt;ˋ`", joined)
        self.assertIn("_Direct ＿Message＿_", joined)

    def test_build_home_view_renders_clean_empty_states_for_rich_rows(self):
        view = slack_home.build_home_view(
            default_workdir="/tmp/project",
            default_model="gpt-5.4",
            default_effort="xhigh",
            bindings_summary="ignored",
            recent_sessions_summary="ignored",
            bindings_rows=[],
            recent_sessions_rows=[],
        )
        section_texts = [
            block.get("text", {}).get("text", "")
            for block in view["blocks"]
            if block.get("type") == "section"
        ]
        joined = "\n".join(section_texts)
        self.assertIn("No bindings yet", joined)
        self.assertIn("No recent sessions found", joined)

    def test_build_home_view_truncates_overlong_recent_row_text(self):
        long_title = "very-long-title-" * 400
        view = slack_home.build_home_view(
            default_workdir="/tmp/project",
            default_model="gpt-5.4",
            default_effort="xhigh",
            bindings_summary="ignored",
            recent_sessions_summary="ignored",
            bindings_rows=[],
            recent_sessions_rows=[
                {
                    "label": "Recent 1",
                    "thread_id": "thr-1",
                    "title": long_title,
                    "cwd": "/tmp/project",
                    "status": "idle",
                }
            ],
        )

        section_blocks = [block for block in view["blocks"] if block.get("type") == "section"]
        long_row_block = next(
            block for block in section_blocks if "thr-1" in block.get("text", {}).get("text", "")
        )
        self.assertLessEqual(len(long_row_block["text"]["text"]), 3000)
        self.assertIn("...", long_row_block["text"]["text"])

    def test_build_home_view_splits_overlong_context_text(self):
        long_help = "help-line " * 500
        view = slack_home.build_home_view(
            default_workdir="/tmp/project",
            default_model="gpt-5.4",
            default_effort="xhigh",
            bindings_summary="binding summary",
            recent_sessions_summary="recent summary",
            quick_hints=["hint"] * 40,
            help_text=long_help,
        )

        context_blocks = [block for block in view["blocks"] if block.get("type") == "context"]
        self.assertGreaterEqual(len(context_blocks), 1)
        for block in context_blocks:
            for element in block.get("elements", []):
                self.assertLessEqual(len(element.get("text", "")), 3000)


class ServerAppHomeHelpersTests(unittest.TestCase):
    def test_get_home_recent_sessions_rows_success(self):
        summaries = [
            ThreadSummary(
                thread_id="thr_1",
                preview="Fix test",
                cwd="/repo",
                updated_at=1700000000,
                created_at=1690000000,
                status_type="idle",
                source="cli",
                name="test thread",
            )
        ]
        with patch.object(server.thread_views, "list_threads", return_value={"data": []}):
            with patch.object(server.thread_views, "extract_thread_summaries", return_value=summaries):
                rows = server.get_home_recent_sessions_rows(limit=5)
        self.assertEqual(rows[0]["thread_id"], "thr_1")
        self.assertEqual(rows[0]["status"], "idle")
        self.assertEqual(rows[0]["cwd"], "/repo")

    def test_get_home_recent_sessions_rows_excludes_existing_bindings(self):
        summaries = [
            ThreadSummary(
                thread_id="thr_1",
                preview="Bound thread",
                cwd="/repo/a",
                updated_at=1700000000,
                created_at=1690000000,
                status_type="idle",
                source="cli",
                name="bound",
            ),
            ThreadSummary(
                thread_id="thr_2",
                preview="Fresh thread",
                cwd="/repo/b",
                updated_at=1700000001,
                created_at=1690000001,
                status_type="active",
                source="cli",
                name="fresh",
            ),
        ]
        with patch.object(server.thread_views, "list_threads", return_value={"data": []}):
            with patch.object(server.thread_views, "extract_thread_summaries", return_value=summaries):
                rows = server.get_home_recent_sessions_rows(limit=5, exclude_thread_ids=["thr_1"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["thread_id"], "thr_2")

    def test_get_home_recent_sessions_rows_error(self):
        with patch.object(server.thread_views, "list_threads", side_effect=RuntimeError("boom")):
            rows = server.get_home_recent_sessions_rows(limit=5)
        self.assertEqual(rows[0]["thread_id"], "-")
        self.assertIn("boom", rows[0]["title"])

    def test_get_home_bindings_rows_prefers_active_turn_session_id(self):
        server.SESSION_STORE.set("D1:1", "sess-old", owner_user_id="U123", session_cwd="/repo")
        server.ACTIVE_TURN_REGISTRY.set("D1:1", "sess-new", "turn-1")
        try:
            with patch.object(server, "get_thread_display_title", return_value="new thread"):
                rows = server.get_home_bindings_rows("U123", limit=5)
        finally:
            server.ACTIVE_TURN_REGISTRY.clear_for_thread("D1:1")
            server.SESSION_STORE.delete("D1:1")

        self.assertEqual(rows[0]["session_id"], "sess-new")
        self.assertEqual(rows[0]["label"], "new thread")
        self.assertEqual(rows[0]["action_value"], "{\"thread_key\":\"D1:1\",\"session_id\":\"sess-new\"}")

    def test_publish_home_view_calls_views_publish(self):
        client = DummyViewClient()
        with patch.object(server, "get_home_bindings_rows", return_value=[]):
            with patch.object(server, "get_home_recent_sessions_rows", return_value=[]):
                with patch.object(server, "get_codex_settings", return_value=("codex", "gpt-5.4", "/repo", 900, "workspace-write", "", False)):
                    with patch.object(server, "get_default_reasoning_effort", return_value="xhigh"):
                        server.publish_home_view(client, "U123")

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["user_id"], "U123")
        self.assertEqual(client.calls[0]["view"]["type"], "home")
        text_blocks = []
        for block in client.calls[0]["view"]["blocks"]:
            if block.get("type") == "section":
                text_blocks.append(block.get("text", {}).get("text", ""))
            if block.get("type") == "context":
                for element in block.get("elements", []):
                    text_blocks.append(element.get("text", ""))
        joined = "\n".join(text_blocks)
        self.assertIn("workspace-write", joined)
        self.assertIn("full_auto=`0`", joined)

    def test_publish_home_view_excludes_bound_session_ids_from_recent_rows(self):
        client = DummyViewClient()
        binding_rows = [
            {
                "label": "DM Control",
                "session_id": "sess-1",
                "mode": "control",
                "cwd": "/repo",
                "updated_at": "2026-04-08 20:00:00",
            }
        ]
        with patch.object(server, "get_home_bindings_rows", return_value=binding_rows):
            with patch.object(server, "get_home_recent_sessions_rows", return_value=[]) as get_recent_rows:
                with patch.object(server, "get_codex_settings", return_value=("codex", "gpt-5.4", "/repo", 900, "workspace-write", "", False)):
                    with patch.object(server, "get_default_reasoning_effort", return_value="xhigh"):
                        server.publish_home_view(client, "U123")

        self.assertEqual(get_recent_rows.call_args.kwargs["exclude_thread_ids"], ["sess-1"])


if __name__ == "__main__":
    unittest.main()
