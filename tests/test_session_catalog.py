import unittest
from unittest.mock import patch

import codex_threads
import session_catalog


class SessionCatalogFormattingTests(unittest.TestCase):
    def test_format_thread_summaries_includes_source_and_current_marker(self):
        summaries = [
            codex_threads.ThreadSummary(
                thread_id="thr-1",
                preview="Fix tests",
                cwd="/repo/a",
                updated_at=1700000000,
                created_at=1699990000,
                status_type="active",
                source="cli",
                name="A",
            ),
            codex_threads.ThreadSummary(
                thread_id="thr-2",
                preview="",
                cwd=None,
                updated_at=None,
                created_at=None,
                status_type="notLoaded",
                source=None,
                name=None,
            ),
        ]

        text = session_catalog.format_thread_summaries(
            summaries,
            heading="Recent Sessions:",
            current_session_id="thr-1",
        )

        self.assertIn("Recent Sessions:", text)
        self.assertIn("`thr-1` (current)", text)
        self.assertIn("source=`cli`", text)
        self.assertIn("`thr-2`", text)
        self.assertIn("source=`-`", text)
        self.assertIn("(untitled)", text)

    def test_format_thread_summaries_handles_empty(self):
        text = session_catalog.format_thread_summaries([], heading="Recent Sessions:")
        self.assertIn("Recent Sessions:", text)
        self.assertIn("当前没有可显示的 Codex sessions。", text)


class SessionCatalogSelectionTests(unittest.TestCase):
    def test_cache_thread_summaries_and_resolve_recent_selector(self):
        cache = session_catalog.SessionSelectionCache()
        summaries = [
            codex_threads.ThreadSummary(
                thread_id="thr-1",
                preview="a",
                cwd=None,
                updated_at=None,
                created_at=None,
                status_type="idle",
                source="cli",
                name=None,
            ),
            codex_threads.ThreadSummary(
                thread_id="thr-2",
                preview="b",
                cwd=None,
                updated_at=None,
                created_at=None,
                status_type="idle",
                source="cli",
                name=None,
            ),
        ]
        session_catalog.cache_thread_summaries(cache, "C1:1", summaries)
        snapshot = cache.get("C1:1")

        self.assertEqual(snapshot.thread_ids, ("thr-1", "thr-2"))
        self.assertEqual(session_catalog.resolve_recent_selector(snapshot, "2"), "thr-2")
        self.assertEqual(session_catalog.resolve_recent_selector(snapshot, 1), "thr-1")

    def test_parse_recent_index_rejects_invalid_values(self):
        with self.assertRaisesRegex(RuntimeError, "attach recent <n>"):
            session_catalog.parse_recent_index("")
        with self.assertRaisesRegex(RuntimeError, "序号无效"):
            session_catalog.parse_recent_index("abc")
        with self.assertRaisesRegex(RuntimeError, "必须从 1 开始"):
            session_catalog.parse_recent_index("0")

    def test_resolve_recent_selector_rejects_expired_snapshot(self):
        stale = session_catalog.SessionSelectionSnapshot(thread_ids=("thr-1",), created_at=1)
        with patch("session_catalog.time.time", return_value=10_000):
            with self.assertRaisesRegex(RuntimeError, "已经过期"):
                session_catalog.resolve_recent_selector(stale, "1", ttl_seconds=1)

    def test_resolve_recent_selector_rejects_out_of_range(self):
        snapshot = session_catalog.SessionSelectionSnapshot(thread_ids=("thr-1",), created_at=1_000)
        with patch("session_catalog.time.time", return_value=1_001):
            with self.assertRaisesRegex(RuntimeError, "超出可选范围"):
                session_catalog.resolve_recent_selector(snapshot, 2, ttl_seconds=10)


class SessionCatalogFetchTests(unittest.TestCase):
    def test_fetch_recent_thread_summaries_filters_by_cwd_unless_all(self):
        marker = object()
        with patch("session_catalog.list_threads", return_value=marker) as list_threads:
            with patch(
                "session_catalog.extract_thread_summaries",
                return_value=[
                    codex_threads.ThreadSummary(
                        thread_id="thr-1",
                        preview="a",
                        cwd="/repo/a",
                        updated_at=1,
                        created_at=1,
                        status_type="idle",
                        source="cli",
                        name=None,
                    ),
                    codex_threads.ThreadSummary(
                        thread_id="",
                        preview="invalid",
                        cwd=None,
                        updated_at=None,
                        created_at=None,
                        status_type="unknown",
                        source=None,
                        name=None,
                    ),
                ],
            ):
                result = session_catalog.fetch_recent_thread_summaries(
                    config="cfg",
                    cwd="/repo/a",
                    include_all=False,
                    limit=5,
                )

        list_threads.assert_called_once_with(
            "cfg",
            archived=False,
            cwd="/repo/a",
            limit=5,
            sort_key="updated_at",
            sort_direction="desc",
        )
        self.assertEqual([s.thread_id for s in result], ["thr-1"])

    def test_fetch_recent_thread_summaries_include_all_ignores_cwd_filter(self):
        with patch("session_catalog.list_threads", return_value={"data": []}) as list_threads:
            with patch("session_catalog.extract_thread_summaries", return_value=[]):
                session_catalog.fetch_recent_thread_summaries(
                    config="cfg",
                    cwd="/repo/a",
                    include_all=True,
                    limit=3,
                )

        list_threads.assert_called_once_with(
            "cfg",
            archived=False,
            cwd=None,
            limit=3,
            sort_key="updated_at",
            sort_direction="desc",
        )


class ThreadRenamePlumbingTests(unittest.TestCase):
    def test_rename_thread_normalizes_title_and_calls_set_name(self):
        with patch("codex_threads.set_thread_name") as set_thread_name:
            title = codex_threads.rename_thread("cfg", "thr-1", "  release prep  ")

        self.assertEqual(title, "release prep")
        set_thread_name.assert_called_once_with("cfg", "thr-1", "release prep")

    def test_rename_thread_requires_session_id(self):
        with self.assertRaisesRegex(RuntimeError, "还没有可重命名的 session"):
            codex_threads.rename_thread("cfg", "", "name")

    def test_rename_thread_requires_non_empty_title(self):
        with self.assertRaisesRegex(RuntimeError, "name` 后面需要一个非空标题"):
            codex_threads.rename_thread("cfg", "thr-1", "   ")


if __name__ == "__main__":
    unittest.main()
