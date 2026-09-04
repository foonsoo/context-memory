import tempfile
import unittest
from pathlib import Path
from unittest import mock

from context_memory.mcp import CORE_TOOL_NAMES, MCPServer, TOOL_BY_NAME
from context_memory.recall import (
    _artifact_paths,
    _cross_language_glosses,
    _expand_recall_query,
    _repository_artifacts,
    estimate_tokens,
)
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

    def test_korean_continuation_aliases_cover_short_and_inflected_terms(self):
        self.assertIn("blog", _expand_recall_query("전에 쓰던 글 계속해줘"))
        self.assertIn(
            "client",
            _expand_recall_query("다른 클라이언트에서 하던 작업 계속하자"),
        )

    def test_artifact_paths_expand_directory_elided_filenames(self):
        paths = _artifact_paths(
            "Files: docs/blog/01-first.md, 02-second.md, 03-third.md"
        )
        self.assertEqual(
            paths,
            [
                "docs/blog/01-first.md",
                "docs/blog/02-second.md",
                "docs/blog/03-third.md",
            ],
        )

    def test_repository_artifacts_are_relevant_and_bounded(self):
        root = Path(self.temp.name) / "repo"
        relevant = root / "docs" / "api.md"
        relevant.parent.mkdir(parents=True)
        relevant.write_text(
            "Cursor pagination is implemented in src/atlas/pagination.py",
            encoding="utf-8",
        )
        unrelated = root / "notes.txt"
        unrelated.write_text("unrelated", encoding="utf-8")

        artifacts = _repository_artifacts(
            str(root), "continue pagination", "opaque cursor", limit=2
        )

        self.assertEqual(
            artifacts, ["docs/api.md", "src/atlas/pagination.py"]
        )

    def test_repository_enumeration_stops_at_entry_budget(self):
        root = Path(self.temp.name) / "large-repo"
        root.mkdir()
        for index in range(20):
            (root / f"artifact-{index}.md").write_text(
                "matching continuation artifact", encoding="utf-8"
            )
        reads = 0
        original = Path.read_text

        def counted_read(path, *args, **kwargs):
            nonlocal reads
            reads += 1
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", counted_read):
            _repository_artifacts(
                str(root), "matching", "continuation", max_entries=5
            )

        self.assertLessEqual(reads, 5)

    def test_cross_language_glosses_are_deterministic_and_bounded(self):
        self.assertEqual(
            _cross_language_glosses(
                "같은 handoff가 반환되는지 검증하고 파일을 업데이트한다"
            ),
            ["same handoff", "update"],
        )

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

    def test_recall_combines_active_context_after_project_selection(self):
        project = self.store.resolve_project(self.temp.name)["project"]
        decision = self._memory(
            "배포 결정", "Nova는 10 percent canary를 사용한다", "decision"
        )
        task = self._memory(
            "배포 다음 단계", "다음은 staging health를 확인한다", "task"
        )

        result = self.store.context_recall(
            self.temp.name, "전에 합의한 롤백 방식으로 진행하자", 256
        )

        self.assertEqual(result["project"]["id"], project["id"])
        self.assertEqual(
            {item["memory_id"] for item in result["items"]},
            {decision["id"], task["id"]},
        )
        self.assertEqual(
            result["retrieval"]["selection_reason"], "cwd_project_context"
        )

    def test_recall_uses_recent_raw_events_without_active_memory(self):
        resolved = self.store.resolve_project(self.temp.name)
        event = self.store.record_event(
            resolved["project"]["id"],
            "task",
            "Continue the Atlas rollout from runbooks/atlas.md",
            scope_id=resolved["scope_id"],
            metadata={"repository_path": self.temp.name},
        )

        result = self.store.context_recall(
            self.temp.name, "continue the Atlas rollout", 128
        )

        self.assertEqual(result["project"]["id"], resolved["project"]["id"])
        self.assertEqual(
            result["repository_path"], str(Path(self.temp.name).resolve())
        )
        self.assertEqual(result["items"][0]["source_event_ids"], [event["id"]])
        self.assertNotIn("memory_id", result["items"][0])
        self.assertEqual(
            result["retrieval"]["selection_reason"], "cwd_recent_events"
        )

    def test_recall_discovers_unambiguous_raw_event_from_unknown_cwd(self):
        target_root = Path(self.temp.name) / "atlas-service"
        target_root.mkdir()
        target = self.store.resolve_project(str(target_root))
        event = self.store.record_event(
            target["project"]["id"],
            "task",
            "Atlas rollout continues with the canary verification",
            scope_id=target["scope_id"],
        )
        unknown = tempfile.TemporaryDirectory()
        self.addCleanup(unknown.cleanup)

        result = self.store.context_recall(
            unknown.name, "continue the Atlas canary", 128
        )

        self.assertEqual(result["project"]["id"], target["project"]["id"])
        self.assertEqual(result["items"][0]["source_event_ids"], [event["id"]])
        self.assertEqual(
            result["retrieval"]["selection_reason"],
            "unambiguous_recent_events",
        )

    def test_recall_rejects_ambiguous_cross_project_raw_events(self):
        for name in ("first", "second"):
            root = Path(self.temp.name) / name
            root.mkdir()
            resolved = self.store.resolve_project(str(root))
            self.store.record_event(
                resolved["project"]["id"],
                "task",
                "Continue the shared canary verification",
                scope_id=resolved["scope_id"],
            )
        unknown = tempfile.TemporaryDirectory()
        self.addCleanup(unknown.cleanup)

        result = self.store.context_recall(
            unknown.name, "continue the shared canary verification", 128
        )

        self.assertIsNone(result["project"])
        self.assertEqual(result["items"], [])

    def test_recall_adds_repository_artifacts_within_token_budget(self):
        project = self.store.resolve_project(self.temp.name)["project"]
        self.store.set_project_alias(project["id"], "path", self.temp.name)
        docs = Path(self.temp.name) / "runbooks"
        docs.mkdir()
        (docs / "deploy.md").write_text(
            "Nova canary deployment and rollback runbook", encoding="utf-8"
        )
        self._memory("Nova rollout", "Use a canary deployment", "decision")

        result = self.store.context_recall(
            self.temp.name, "Continue the Nova deployment", 128
        )

        self.assertIn("runbooks/deploy.md", result["items"][0]["artifacts"])
        self.assertLessEqual(result["budget"]["used"], 128)

    def test_recall_does_not_report_placeholder_as_selected_project(self):
        unrelated = tempfile.TemporaryDirectory()
        self.addCleanup(unrelated.cleanup)

        result = self.store.context_recall(
            unrelated.name, "전에 하던 작업 계속해줘", 128
        )

        self.assertIsNone(result["project"])
        self.assertIsNone(result["repository_path"])
        self.assertEqual(result["items"], [])

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
