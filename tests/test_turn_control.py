import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server
import turn_control
from codex_threads import CodexAppServerConfig


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def make_config():
    return CodexAppServerConfig(
        codex_bin="codex",
        workdir="/tmp",
        env={},
    )


def make_thread_response(thread_id="thr-1", status_type="active", turns=None):
    return ns(thread=ns(id=thread_id, status=ns(type=status_type), turns=list(turns or [])))


def make_turn(turn_id, status):
    return ns(id=turn_id, status=status)


class DummyClient:
    def __init__(self):
        self.messages = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)


class TurnControlHelperTests(unittest.TestCase):
    def test_find_active_turn_prefers_latest_in_progress_turn(self):
        response = make_thread_response(
            turns=[
                make_turn("turn-old", "completed"),
                make_turn("turn-running-1", ns(value="inProgress")),
                make_turn("turn-running-2", "inProgress"),
            ]
        )

        active = turn_control.find_active_turn(response)

        self.assertIsNotNone(active)
        self.assertEqual(active.thread_id, "thr-1")
        self.assertEqual(active.turn_id, "turn-running-2")

    def test_find_active_turn_requires_active_thread_status(self):
        response = make_thread_response(
            status_type="idle",
            turns=[make_turn("turn-running", "inProgress")],
        )

        self.assertIsNone(turn_control.find_active_turn(response))

    def test_interrupt_active_turn_raises_when_no_active_turn(self):
        with patch.object(turn_control, "read_thread_response", return_value=make_thread_response(status_type="idle")):
            with self.assertRaises(RuntimeError):
                turn_control.interrupt_active_turn(make_config(), "session-1")

    def test_steer_active_turn_calls_sdk_with_expected_payload(self):
        response = make_thread_response(
            thread_id="thr-steer",
            turns=[make_turn("turn-steer", "inProgress")],
        )

        with patch.object(turn_control, "read_thread_response", return_value=response):
            with patch.object(turn_control, "steer_turn") as steer_turn:
                active = turn_control.steer_active_turn(make_config(), "session-2", "focus on tests")

        self.assertEqual(active.turn_id, "turn-steer")
        steer_turn.assert_called_once_with(
            make_config(),
            thread_id="thr-steer",
            expected_turn_id="turn-steer",
            input_items=[{"type": "text", "text": "focus on tests"}],
        )


class TurnCommandParsingTests(unittest.TestCase):
    def test_interrupt_and_steer_command_variants(self):
        self.assertTrue(server.is_interrupt_command("/interrupt"))
        self.assertTrue(server.is_interrupt_command("stop turn"))
        self.assertFalse(server.is_interrupt_command("interrupt now please"))
        self.assertTrue(server.is_steer_command("steer focus tests"))
        self.assertEqual(server.strip_steer_command("/steer keep it short"), "keep it short")


class ProcessPromptTurnCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.store = server.SlackThreadSessionStore(Path(self.tmpdir.name) / "sessions.json")
        self.session_store_patcher = patch.object(server, "SESSION_STORE", self.store)
        self.session_store_patcher.start()
        self.addCleanup(self.session_store_patcher.stop)
        self.active_turn_registry_patcher = patch.object(server, "ACTIVE_TURN_REGISTRY", turn_control.ActiveTurnRegistry())
        self.active_turn_registry_patcher.start()
        self.addCleanup(self.active_turn_registry_patcher.stop)

        server.WATCHERS.clear()
        self.addCleanup(server.WATCHERS.clear)

        self.client = DummyClient()
        self.channel = "C1"
        self.thread_ts = "1"
        self.user_id = "U111"
        self.thread_key = server.make_thread_key(self.channel, self.thread_ts)
        self.session_id = "019d5868-71ba-7101-9143-81867f3db5bf"

    def test_interrupt_requires_session(self):
        server.process_prompt(self.client, self.channel, self.thread_ts, "interrupt", self.user_id)
        self.assertIn("还没有 Codex session", self.client.messages[0]["text"])

    def test_interrupt_allowed_in_observe_mode(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)
        active_turn = turn_control.ActiveTurnInfo(thread_id=self.session_id, turn_id="turn-123", status="inProgress")

        with patch.object(server, "get_codex_app_server_config", return_value=make_config()) as get_cfg:
            with patch.object(server, "interrupt_active_turn", return_value=active_turn) as interrupt_active_turn:
                server.process_prompt(self.client, self.channel, self.thread_ts, "interrupt", self.user_id)

        get_cfg.assert_called_once()
        interrupt_active_turn.assert_called_once_with(make_config(), self.session_id)
        self.assertIn("已发送中断请求", self.client.messages[0]["text"])
        cached = server.ACTIVE_TURN_REGISTRY.get_for_thread(self.thread_key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.turn_id, "turn-123")

    def test_steer_requires_control_mode(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)

        with patch.object(server, "steer_active_turn") as steer_active_turn:
            server.process_prompt(self.client, self.channel, self.thread_ts, "steer focus tests", self.user_id)

        steer_active_turn.assert_not_called()
        self.assertIn("`steer` 只在 `control` 模式下可用", self.client.messages[0]["text"])

    def test_steer_requires_payload(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)

        server.process_prompt(self.client, self.channel, self.thread_ts, "steer", self.user_id)

        self.assertIn("用法：`steer <", self.client.messages[0]["text"])

    def test_steer_in_control_mode_calls_turn_control(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        active_turn = turn_control.ActiveTurnInfo(thread_id=self.session_id, turn_id="turn-456", status="inProgress")

        with patch.object(server, "get_codex_app_server_config", return_value=make_config()) as get_cfg:
            with patch.object(server, "steer_active_turn", return_value=active_turn) as steer_active_turn:
                server.process_prompt(self.client, self.channel, self.thread_ts, "steer focus on failing tests first", self.user_id)

        get_cfg.assert_called_once()
        steer_active_turn.assert_called_once_with(
            make_config(),
            self.session_id,
            "focus on failing tests first",
        )
        text = self.client.messages[0]["text"]
        self.assertIn("已向 session", text)
        self.assertIn("turn-456", text)
        cached = server.ACTIVE_TURN_REGISTRY.get_for_thread(self.thread_key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.turn_id, "turn-456")


if __name__ == "__main__":
    unittest.main()
