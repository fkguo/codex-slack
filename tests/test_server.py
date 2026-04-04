import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class CommandParsingTests(unittest.TestCase):
    def test_fresh_command_supports_slash_and_plain_variants(self):
        self.assertTrue(server.is_fresh_command("/fresh summarize status"))
        self.assertTrue(server.is_fresh_command("fresh summarize status"))
        self.assertTrue(server.is_fresh_command("Fresh summarize status"))
        self.assertTrue(server.is_fresh_command("/fresh"))
        self.assertTrue(server.is_fresh_command("fresh"))
        self.assertFalse(server.is_fresh_command("freshness check"))

    def test_strip_fresh_command_returns_payload(self):
        self.assertEqual(server.strip_fresh_command("/fresh do the task"), "do the task")
        self.assertEqual(server.strip_fresh_command("fresh do the task"), "do the task")
        self.assertEqual(server.strip_fresh_command("/fresh"), "")
        self.assertEqual(server.strip_fresh_command("fresh"), "")
        self.assertEqual(server.strip_fresh_command("freshness check"), "")

    def test_attach_command_supports_slash_and_plain_variants(self):
        self.assertTrue(server.is_attach_command("/attach 019-test"))
        self.assertTrue(server.is_attach_command("attach 019-test"))
        self.assertTrue(server.is_attach_command("ATTACH 019-test"))
        self.assertTrue(server.is_attach_command("/attach"))
        self.assertTrue(server.is_attach_command("attach"))
        self.assertEqual(server.strip_attach_command("/attach 019-test"), "019-test")
        self.assertEqual(server.strip_attach_command("attach 019-test"), "019-test")
        self.assertEqual(server.strip_attach_command("ATTACH 019-test"), "019-test")
        self.assertEqual(server.strip_attach_command("/attach"), "")
        self.assertEqual(server.strip_attach_command("attach"), "")
        self.assertEqual(server.strip_attach_command("attach "), "")

    def test_status_and_session_commands_support_plain_text(self):
        self.assertTrue(server.is_status_command("/where"))
        self.assertTrue(server.is_status_command("whoami"))
        self.assertTrue(server.is_status_command("status"))
        self.assertTrue(server.is_session_command("/session"))
        self.assertTrue(server.is_session_command("session"))
        self.assertTrue(server.is_session_command("session id"))
        self.assertFalse(server.is_status_command("where are you going"))


class SlackAccessTests(unittest.TestCase):
    def test_allowed_user_ids_support_commas_and_whitespace(self):
        with patch.dict(
            server.ENV,
            {"ALLOWED_SLACK_USER_IDS": "U111, U222\nU333\tU444"},
            clear=False,
        ):
            self.assertEqual(
                server.get_allowed_slack_user_ids(),
                {"U111", "U222", "U333", "U444"},
            )

    def test_blank_allowlist_means_unrestricted(self):
        with patch.dict(server.ENV, {"ALLOWED_SLACK_USER_IDS": ""}, clear=False):
            self.assertTrue(server.is_allowed_slack_user("U111"))

    def test_allowlist_restricts_unknown_user(self):
        with patch.dict(server.ENV, {"ALLOWED_SLACK_USER_IDS": "U111,U222"}, clear=False):
            self.assertTrue(server.is_allowed_slack_user("U111"))
            self.assertFalse(server.is_allowed_slack_user("U999"))

    def test_attach_accepts_uuid_in_single_user_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            with patch.dict(
                server.ENV,
                {"ALLOWED_SLACK_USER_IDS": "U111", "ALLOW_SHARED_ATTACH": "0"},
                clear=False,
            ):
                self.assertIsNone(
                    server.get_attach_error(
                        "U111",
                        "019d5868-71ba-7101-9143-81867f3db5bf",
                        session_store=store,
                    )
                )

    def test_attach_rejects_non_uuid_session_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            with patch.dict(server.ENV, {"ALLOWED_SLACK_USER_IDS": "U111"}, clear=False):
                error = server.get_attach_error("U111", "thread-name", session_store=store)
        self.assertIn("只接受 Codex session UUID", error)

    def test_attach_rejects_unseen_session_in_multi_user_mode_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            with patch.dict(
                server.ENV,
                {"ALLOWED_SLACK_USER_IDS": "U111,U222", "ALLOW_SHARED_ATTACH": "0"},
                clear=False,
            ):
                error = server.get_attach_error(
                    "U111",
                    "019d5868-71ba-7101-9143-81867f3db5bf",
                    session_store=store,
                )
        self.assertIn("ALLOW_SHARED_ATTACH=1", error)

    def test_attach_rejects_session_owned_by_another_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            session_id = "019d5868-71ba-7101-9143-81867f3db5bf"
            store.set("C1:1", session_id, owner_user_id="U111")
            with patch.dict(
                server.ENV,
                {"ALLOWED_SLACK_USER_IDS": "U111,U222", "ALLOW_SHARED_ATTACH": "1"},
                clear=False,
            ):
                error = server.get_attach_error("U222", session_id, session_store=store)
        self.assertIn("不允许跨用户接管", error)

    def test_attach_session_atomically_rejects_cross_user_takeover(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            session_id = "019d5868-71ba-7101-9143-81867f3db5bf"
            store.set("C1:1", session_id, owner_user_id="U111")

            previous_session_id, error = store.attach_session(
                "C2:2",
                session_id,
                owner_user_id="U222",
                allow_unseen=True,
            )

            self.assertIsNone(store.get("C2:2"))

        self.assertIsNone(previous_session_id)
        self.assertIn("不允许跨用户接管", error)

    def test_attach_session_returns_previous_thread_session_on_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set("C1:1", "019d5868-71ba-7101-9143-81867f3db5bf", owner_user_id="U111")

            previous_session_id, error = store.attach_session(
                "C1:1",
                "019d5868-71ba-7101-9143-81867f3db5c0",
                owner_user_id="U111",
                allow_unseen=True,
            )

        self.assertEqual(previous_session_id, "019d5868-71ba-7101-9143-81867f3db5bf")
        self.assertIsNone(error)

    def test_attach_session_rejects_cross_user_thread_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set("C1:1", "019d5868-71ba-7101-9143-81867f3db5bf", owner_user_id="U111")

            previous_session_id, error = store.attach_session(
                "C1:1",
                "019d5868-71ba-7101-9143-81867f3db5c0",
                owner_user_id="U222",
                allow_unseen=True,
            )

        self.assertEqual(previous_session_id, "019d5868-71ba-7101-9143-81867f3db5bf")
        self.assertIn("不允许跨用户覆盖", error)

    def test_thread_owner_access_error_rejects_non_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set("C1:1", "019d5868-71ba-7101-9143-81867f3db5bf", owner_user_id="U111")
            error = server.get_thread_owner_access_error("C1:1", "U222", session_store=store)

        self.assertIn("不允许跨用户继续使用", error)

    def test_thread_owner_access_error_allows_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set("C1:1", "019d5868-71ba-7101-9143-81867f3db5bf", owner_user_id="U111")
            error = server.get_thread_owner_access_error("C1:1", "U111", session_store=store)

        self.assertIsNone(error)


class FormattingTests(unittest.TestCase):
    def test_handoff_footer_includes_terminal_verification(self):
        text = server.append_handoff_footer("Current Goal:\nkeep context", "019-test", "/tmp/workdir")
        self.assertIn("In-Session Verify Command:", text)
        self.assertIn("如果你已经在目标 Codex 会话内部，可运行：", text)
        self.assertIn("`printenv CODEX_THREAD_ID && pwd`", text)
        self.assertIn("Expected Session ID: `019-test`", text)
        self.assertIn("Expected Workdir: `/tmp/workdir`", text)

    def test_recap_footer_includes_current_session_id(self):
        text = server.append_recap_footer("Recent Progress:\nupdated docs", "019-test")
        self.assertIn("Current Session ID: `019-test`", text)

    def test_clean_codex_output_filters_progress_noise(self):
        raw = textwrap.dedent(
            """
            thinking about the plan
            running tests
            useful line

            commentary hidden
            final answer
            """
        ).strip()
        self.assertEqual(server.clean_codex_output(raw), "useful line\n\nfinal answer")

    def test_chunk_text_splits_long_messages(self):
        self.assertEqual(server.chunk_text("abcdef", max_length=3), ["abc", "def"])


class LoggingTests(unittest.TestCase):
    def test_summarize_text_for_log_redacts_content(self):
        self.assertEqual(server.summarize_text_for_log("hello"), "<chars=5>")
        self.assertEqual(server.summarize_text_for_log(""), "<chars=0>")
        self.assertEqual(server.summarize_text_for_log(None), "<chars=0>")


class JsonEventParsingTests(unittest.TestCase):
    def test_parse_codex_json_events_extracts_session_and_messages(self):
        raw = "\n".join(
            [
                '{"type":"thread.started","thread_id":"019-test"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"first reply"}}',
                '{"type":"item.completed","item":{"type":"tool_result","text":"ignored"}}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"second reply"}}',
            ]
        )
        session_id, message = server.parse_codex_json_events(raw)
        self.assertEqual(session_id, "019-test")
        self.assertEqual(message, "first reply\n\nsecond reply")

    def test_parse_codex_json_events_ignores_invalid_lines(self):
        raw = "\n".join(
            [
                "not json",
                '{"type":"thread.started","thread_id":"019-test"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"reply"}}',
            ]
        )
        session_id, message = server.parse_codex_json_events(raw)
        self.assertEqual(session_id, "019-test")
        self.assertEqual(message, "reply")


class SessionRecoveryTests(unittest.TestCase):
    def test_should_rebuild_invalid_session_requires_nonzero_exit(self):
        result = server.CodexRunResult(
            session_id="019d5868-71ba-7101-9143-81867f3db5bf",
            text="I fixed the issue after seeing 'session not found' in a pasted log.",
            exit_code=0,
            raw_output="",
            final_output="",
            json_output="",
            cleaned_output="",
            timed_out=False,
        )
        self.assertFalse(server.should_rebuild_invalid_session(result))

    def test_should_rebuild_invalid_session_on_failed_resume_error_text(self):
        result = server.CodexRunResult(
            session_id="019d5868-71ba-7101-9143-81867f3db5bf",
            text="Codex exited with status 1.\n\nsession not found",
            exit_code=1,
            raw_output='{"type":"error","message":"session not found"}',
            final_output="",
            json_output="",
            cleaned_output="",
            timed_out=False,
        )
        self.assertTrue(server.should_rebuild_invalid_session(result))

    def test_should_update_session_activity_rejects_timeout(self):
        timed_out_result = server.CodexRunResult(
            session_id="019d5868-71ba-7101-9143-81867f3db5bf",
            text="Codex timed out after 1 seconds.",
            exit_code=None,
            raw_output="",
            final_output="",
            json_output="",
            cleaned_output="",
            timed_out=True,
        )
        self.assertFalse(server.should_update_session_activity(timed_out_result))


class ThreadLockTests(unittest.TestCase):
    def test_thread_lock_entries_are_reused_then_evicted(self):
        original = server.THREAD_LOCKS.copy()
        server.THREAD_LOCKS.clear()
        thread_key = "C123:1712345.6789"

        try:
            lock_one = server.claim_thread_lock(thread_key)
            lock_two = server.claim_thread_lock(thread_key)

            self.assertIs(lock_one, lock_two)
            self.assertEqual(server.THREAD_LOCKS[thread_key].waiters, 2)

            server.release_thread_lock(thread_key)
            self.assertIn(thread_key, server.THREAD_LOCKS)
            self.assertEqual(server.THREAD_LOCKS[thread_key].waiters, 1)

            server.release_thread_lock(thread_key)
            self.assertNotIn(thread_key, server.THREAD_LOCKS)
        finally:
            server.THREAD_LOCKS.clear()
            server.THREAD_LOCKS.update(original)


class SessionLockTests(unittest.TestCase):
    def test_session_lock_entries_are_reused_then_evicted(self):
        original = server.SESSION_LOCKS.copy()
        server.SESSION_LOCKS.clear()
        session_id = "019d5868-71ba-7101-9143-81867f3db5bf"

        try:
            lock_one = server.claim_session_lock(session_id)
            lock_two = server.claim_session_lock(session_id)

            self.assertIs(lock_one, lock_two)
            self.assertEqual(server.SESSION_LOCKS[session_id].waiters, 2)

            server.release_session_lock(session_id)
            self.assertIn(session_id, server.SESSION_LOCKS)
            self.assertEqual(server.SESSION_LOCKS[session_id].waiters, 1)

            server.release_session_lock(session_id)
            self.assertNotIn(session_id, server.SESSION_LOCKS)
        finally:
            server.SESSION_LOCKS.clear()
            server.SESSION_LOCKS.update(original)


class ProcessPromptAccessTests(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.messages = []

        def chat_postMessage(self, **kwargs):
            self.messages.append(kwargs)

    def test_process_prompt_rejects_non_owner_before_resuming(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set("C1:1", "019d5868-71ba-7101-9143-81867f3db5bf", owner_user_id="U111")
            client = self.FakeClient()

            with patch.object(server, "SESSION_STORE", store):
                with patch.object(server, "run_codex") as run_codex:
                    server.process_prompt(client, "C1", "1", "continue working", "U222")

        self.assertEqual(len(client.messages), 1)
        self.assertIn("不允许跨用户继续使用", client.messages[0]["text"])
        run_codex.assert_not_called()

    def test_process_prompt_rejects_non_owner_fresh_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set("C1:1", "019d5868-71ba-7101-9143-81867f3db5bf", owner_user_id="U111")
            client = self.FakeClient()

            with patch.object(server, "SESSION_STORE", store):
                with patch.object(server, "run_codex") as run_codex:
                    server.process_prompt(client, "C1", "1", "fresh do something new", "U222")

        self.assertEqual(len(client.messages), 1)
        self.assertIn("不允许跨用户继续使用", client.messages[0]["text"])
        run_codex.assert_not_called()

    def test_process_prompt_rejects_non_owner_handoff_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set("C1:1", "019d5868-71ba-7101-9143-81867f3db5bf", owner_user_id="U111")
            client = self.FakeClient()

            with patch.object(server, "SESSION_STORE", store):
                with patch.object(server, "run_codex") as run_codex:
                    server.process_prompt(client, "C1", "1", "handoff", "U222")

        self.assertEqual(len(client.messages), 1)
        self.assertIn("不允许跨用户继续使用", client.messages[0]["text"])
        run_codex.assert_not_called()


class RunCodexTests(unittest.TestCase):
    def test_build_codex_resume_args_uses_supported_resume_flags_only(self):
        with patch.object(
            server,
            "get_codex_settings",
            return_value=("codex", "gpt-5.4", "/tmp/work", 900, "danger-full-access", "--profile x", True),
        ):
            codex_bin, args, timeout, workdir = server.build_codex_resume_args(
                "019d5868-71ba-7101-9143-81867f3db5bf",
                "continue task",
                "/tmp/out.txt",
            )

        self.assertEqual(codex_bin, "codex")
        self.assertEqual(timeout, 900)
        self.assertEqual(workdir, "/tmp/work")
        self.assertNotIn("--sandbox", args)
        self.assertNotIn("--color", args)
        self.assertNotIn("--profile", args)
        self.assertEqual(
            args,
            [
                "exec",
                "resume",
                "--model",
                "gpt-5.4",
                "--skip-git-repo-check",
                "--output-last-message",
                "/tmp/out.txt",
                "--json",
                "--full-auto",
                "019d5868-71ba-7101-9143-81867f3db5bf",
                "continue task",
            ],
        )

    def test_run_codex_cleans_up_temp_file_on_timeout(self):
        output_path = Path(tempfile.gettempdir()) / "codex-slack-timeout-test.txt"
        output_path.unlink(missing_ok=True)

        class FakeTempFile:
            def __init__(self, path):
                self.name = str(path)

            def __enter__(self):
                output_path.write_text("", encoding="utf-8")
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeChild:
            def __init__(self, *args, **kwargs):
                self.exitstatus = None
                self.signalstatus = None
                self._alive = True

            def expect(self, _pattern):
                raise server.pexpect.TIMEOUT("timed out")

            def close(self, force=False):
                self._alive = False

            def isalive(self):
                return self._alive

        with patch.object(server.tempfile, "NamedTemporaryFile", return_value=FakeTempFile(output_path)):
            with patch.object(server, "get_codex_settings", return_value=("codex", "gpt-5.4", "/tmp", 1, "danger-full-access", "", False)):
                with patch.object(server.pexpect, "spawn", return_value=FakeChild()):
                    result = server.run_codex("test timeout")

        self.assertEqual(result.text, "Codex timed out after 1 seconds.")
        self.assertTrue(result.timed_out)
        self.assertFalse(output_path.exists())

    def test_build_codex_child_env_strips_slack_secrets(self):
        with patch.dict(
            server.ENV,
            {
                "SLACK_BOT_TOKEN": "xoxb-secret",
                "SLACK_APP_TOKEN": "xapp-secret",
                "SLACK_SIGNING_SECRET": "signing-secret",
                "CODEX_BIN": "codex",
                "OPENAI_MODEL": "gpt-5.4",
            },
            clear=False,
        ):
            child_env = server.build_codex_child_env()

        self.assertNotIn("SLACK_BOT_TOKEN", child_env)
        self.assertNotIn("SLACK_APP_TOKEN", child_env)
        self.assertNotIn("SLACK_SIGNING_SECRET", child_env)
        self.assertEqual(child_env["CODEX_BIN"], "codex")


if __name__ == "__main__":
    unittest.main()
