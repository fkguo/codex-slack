import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from app_runtime import AppServerRuntime, RuntimeActiveTurn
from codex_app_server_sdk.errors import CodexProtocolError, CodexTimeoutError


class AppRuntimeControlCallTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_final_text_prefers_final_answer_over_commentary(self):
        session = SimpleNamespace(
            raw_events=[
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "text": "这是 commentary",
                            "phase": "commentary",
                        }
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "text": "这是最终方案",
                            "phase": "final_answer",
                        }
                    },
                },
            ]
        )

        result = AppServerRuntime._extract_final_text_from_session(session)

        self.assertEqual(result, "这是最终方案")

    def test_extract_final_text_does_not_fall_back_to_commentary_only(self):
        session = SimpleNamespace(
            raw_events=[
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "text": "我先检查现状",
                            "phase": "commentary",
                        }
                    },
                }
            ]
        )

        result = AppServerRuntime._extract_final_text_from_session(session)

        self.assertEqual(result, "")

    def test_extract_final_text_wraps_plan_item_when_no_final_answer(self):
        session = SimpleNamespace(
            raw_events=[
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "agentMessage",
                            "text": "我先做只读检查",
                            "phase": "commentary",
                        }
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "plan",
                            "text": "# Final plan\n- first\n- second\n",
                        }
                    },
                },
            ]
        )

        result = AppServerRuntime._extract_final_text_from_session(session)

        self.assertEqual(
            result,
            "<proposed_plan>\n# Final plan\n- first\n- second\n</proposed_plan>",
        )

    async def test_interrupt_turn_uses_thread_id_and_turn_id(self):
        runtime = AppServerRuntime(lambda: None)
        client = AsyncMock()
        runtime._ensure_client_async = AsyncMock(return_value=client)
        active_turn = RuntimeActiveTurn(
            session_id="019d5868-71ba-7101-9143-81867f3db5bf",
            turn_id="turn-123",
            started_at=0,
        )

        await runtime._interrupt_turn_async(active_turn)

        client.request.assert_awaited_once_with(
            "turn/interrupt",
            {
                "threadId": "019d5868-71ba-7101-9143-81867f3db5bf",
                "turnId": "turn-123",
            },
        )

    async def test_steer_turn_uses_thread_id_and_expected_turn_id(self):
        runtime = AppServerRuntime(lambda: None)
        client = AsyncMock()
        runtime._ensure_client_async = AsyncMock(return_value=client)
        active_turn = RuntimeActiveTurn(
            session_id="019d5868-71ba-7101-9143-81867f3db5bf",
            turn_id="turn-456",
            started_at=0,
        )

        await runtime._steer_turn_async(active_turn, "focus on tests")

        client.steer_turn.assert_awaited_once_with(
            thread_id="019d5868-71ba-7101-9143-81867f3db5bf",
            expected_turn_id="turn-456",
            input_items=[{"type": "text", "text": "focus on tests"}],
        )

    async def test_resolve_thread_retries_resume_after_timeout(self):
        runtime = AppServerRuntime(
            lambda: SimpleNamespace(resume_request_timeout=90.0, request_timeout=90.0, resume_max_retries=2)
        )
        first_client = AsyncMock()
        second_client = AsyncMock()
        first_client.request.side_effect = CodexTimeoutError(
            "request timed out for method='thread/resume' after 90.0s"
        )
        first_client._codex_slack_transport = SimpleNamespace(
            stderr_tail_text=lambda max_lines=20: "resume timeout tail"
        )
        second_client.request.return_value = {"thread": {"id": "sess-1"}}
        runtime._ensure_client_async = AsyncMock(side_effect=[first_client, second_client])
        runtime._reset_client_async = AsyncMock()

        thread_id = await runtime._resolve_thread_async("sess-1", None)

        self.assertEqual(thread_id, "sess-1")
        first_client.request.assert_awaited_once_with(
            "thread/resume",
            {"threadId": "sess-1"},
            timeout=90.0,
        )
        runtime._reset_client_async.assert_awaited_once()
        second_client.request.assert_awaited_once_with(
            "thread/resume",
            {"threadId": "sess-1"},
            timeout=90.0,
        )
        self.assertIn("thread/resume attempt 1/2 failed", runtime.last_client_diagnostics())
        self.assertIn("resume timeout tail", runtime.last_client_diagnostics())

    async def test_resolve_thread_uses_configured_resume_timeout_and_retry_budget(self):
        runtime = AppServerRuntime(
            lambda: SimpleNamespace(resume_request_timeout=42.0, request_timeout=90.0, resume_max_retries=3)
        )
        client = AsyncMock()
        calls = []

        async def request(method, params, timeout=None):
            calls.append((method, dict(params), timeout))
            if len(calls) < 3:
                raise CodexTimeoutError("request timed out for method='thread/resume' after 42.0s")
            return {"thread": {"id": "sess-1"}}

        client.request.side_effect = request
        client._codex_slack_transport = SimpleNamespace(
            stderr_tail_text=lambda max_lines=20: "resume tail"
        )
        runtime._ensure_client_async = AsyncMock(return_value=client)
        runtime._reset_client_async = AsyncMock()

        thread_id = await runtime._resolve_thread_async("sess-1", None)

        self.assertEqual(thread_id, "sess-1")
        self.assertEqual(
            calls,
            [
                ("thread/resume", {"threadId": "sess-1"}, 42.0),
                ("thread/resume", {"threadId": "sess-1"}, 42.0),
                ("thread/resume", {"threadId": "sess-1"}, 42.0),
            ],
        )
        self.assertEqual(runtime._reset_client_async.await_count, 2)

    async def test_run_turn_retries_without_collaboration_mode_when_capability_missing(self):
        runtime = AppServerRuntime(lambda: None)
        client = AsyncMock()
        request_payloads = []

        async def request(method, payload):
            request_payloads.append(dict(payload))
            if len(request_payloads) == 1:
                raise CodexProtocolError(
                    "turn/start failed: turn/start.collaborationMode requires experimentalApi capability"
                )
            return {"turnId": "turn-1"}

        client.request = AsyncMock(side_effect=request)
        client._receive_turn_event = AsyncMock(return_value={})

        def apply_event(session, _event):
            session.completed = True

        client._apply_event_to_session = apply_event
        runtime._ensure_client_async = AsyncMock(return_value=client)
        runtime._resolve_thread_async = AsyncMock(return_value="sess-1")

        with patch("app_runtime._extract_turn_id", return_value="turn-1"):
            result = await runtime._run_turn_async(
                session_id="sess-1",
                input_items=[{"type": "text", "text": "continue"}],
                collaboration_mode={"mode": "plan"},
            )

        self.assertEqual(result.session_id, "sess-1")
        self.assertEqual(client.request.await_count, 2)
        self.assertIn("collaborationMode", request_payloads[0])
        self.assertNotIn("collaborationMode", request_payloads[1])


if __name__ == "__main__":
    unittest.main()
