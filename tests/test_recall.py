import tempfile
import unittest
from pathlib import Path

from context_memory.mcp import CORE_TOOL_NAMES, MCPServer, TOOL_BY_NAME
from context_memory.recall import estimate_tokens
from context_memory.store import MemoryStore


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "memory.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _memory(self, title, content, memory_type="task"):
        project = self.store.resolve_project(self.temp.name)["project"]
        event = self.store.record_event(project["id"], memory_type, content)
        return self.store.upsert_memory(
            project["id"],
            title,
            content,
            memory_type,
            "active",
            source_event_ids=[event["id"]],
        )

    def test_estimator_handles_korean_and_english_without_dependency(self):
        self.assertGreaterEqual(estimate_tokens("블로그 4화를 continue"), 8)
        self.assertLess(estimate_tokens("short context"), 10)

    def test_recall_is_session_independent_and_token_bounded(self):
        target = self._memory(
            "블로그 4화",
            "블로그 4화 주제는 의사결정 문서이며 1화부터 3화의 문체를 따른다.",
        )
        self._memory("배포", "호스팅 배포 보안 정책과 운영 문서")
        before = self.store._row(
            "SELECT COUNT(*) AS n FROM sessions", ()
        )["n"]

        result = self.store.context_recall(
            self.temp.name, "블로그 4화를 이어서 작성하자", 96
        )

        after = self.store._row(
            "SELECT COUNT(*) AS n FROM sessions", ()
        )["n"]
        self.assertEqual(before, after)
        self.assertEqual(result["contract"], "context-recall/v1")
        self.assertEqual(result["items"][0]["memory_id"], target["id"])
        self.assertLessEqual(result["budget"]["used"], 96)
        self.assertFalse(result["retrieval"]["session_created"])
        self.assertNotIn("호스팅", str(result["items"]))

    def test_recall_excludes_superseded_error(self):
        wrong = self._memory("원고 없음", "블로그 1화부터 3화 원고가 없다")
        correction = self._memory(
            "원고 위치",
            "블로그 1화부터 3화 원고는 docs/blog 경로에 있다",
            "fact",
        )
        self.store.transition(
            wrong["id"], "superseded", related_memory_id=correction["id"]
        )

        result = self.store.context_recall(
            self.temp.name, "블로그 4화를 이어서 작성하자", 128
        )

        rendered = str(result["items"])
        self.assertIn("docs/blog", rendered)
        self.assertNotIn("원고가 없다", rendered)

    def test_mcp_exposes_read_only_recall_contract(self):
        self.assertIn("context_recall", CORE_TOOL_NAMES)
        self.assertTrue(
            TOOL_BY_NAME["context_recall"]["annotations"]["readOnlyHint"]
        )
        server = MCPServer(self.store, "core")
        result = server.call(
            "context_recall",
            {"cwd": self.temp.name, "query": "continue", "token_budget": 64},
        )
        self.assertEqual(result["budget"]["limit"], 64)
