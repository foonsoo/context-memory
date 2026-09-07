import tempfile
import unittest
from pathlib import Path

from context_memory.cli import doctor
from context_memory.store import MemoryStore


class TrustContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temporary.name) / "memory.db")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def _counts(self):
        tables = [
            row["name"]
            for row in self.store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if not row["name"].endswith(("_fts_data", "_fts_idx"))
        ]
        return {
            table: self.store.conn.execute(
                f'SELECT count(*) FROM "{table}"'
            ).fetchone()[0]
            for table in tables
        }

    def test_same_leaf_name_does_not_merge_projects(self):
        first = self.store.resolve_project(
            str(Path(self.temporary.name) / "company-a" / "backend")
        )
        second = self.store.resolve_project(
            str(Path(self.temporary.name) / "company-b" / "backend")
        )
        self.assertNotEqual(first["project"]["id"], second["project"]["id"])
        again = self.store.resolve_project(
            str(Path(self.temporary.name) / "company-a" / "backend")
        )
        self.assertEqual(first["project"]["id"], again["project"]["id"])

    def test_explicit_moved_path_alias_resolves_and_collision_is_rejected(self):
        first = self.store.create_project("first")
        second = self.store.create_project("second")
        moved = str(Path(self.temporary.name) / "moved" / "backend")
        self.store.set_project_alias(first["id"], "path", moved)
        self.assertEqual(
            self.store.resolve_project(moved)["project"]["id"], first["id"]
        )
        with self.assertRaisesRegex(ValueError, "another project"):
            self.store.set_project_alias(second["id"], "path", moved)

    def test_active_memory_requires_same_project_source_atomically(self):
        project = self.store.create_project("one")
        other = self.store.create_project("two")
        foreign = self.store.record_event(other["id"], "decision", "foreign")
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "record_event"):
            self.store.upsert_memory(
                project["id"], "No source", "unsafe", status="active"
            )
        self.assertEqual(before, self._counts())
        with self.assertRaisesRegex(ValueError, "invalid source event"):
            self.store.upsert_memory(
                project["id"], "Foreign", "unsafe", status="active",
                source_event_ids=[foreign["id"]],
            )
        self.assertEqual(before, self._counts())

        proposed = self.store.upsert_memory(project["id"], "Draft", "ok")
        with self.assertRaisesRegex(ValueError, "record_event"):
            self.store.transition(proposed["id"], "active")
        source = self.store.record_event(project["id"], "decision", "confirmed")
        supported = self.store.upsert_memory(
            project["id"], "Supported", "traceable", status="active",
            source_event_ids=[source["id"]],
        )
        self.assertEqual(supported["status"], "active")
        self.assertEqual(
            self.store.transition(supported["id"], "active")["status"],
            "active",
        )

    def test_doctor_reports_legacy_active_without_exposing_content(self):
        project = self.store.create_project("legacy")
        memory = self.store.upsert_memory(
            project["id"], "Draft", "sensitive-memory-body"
        )
        self.store.conn.execute(
            "UPDATE memories SET status='active' WHERE id=?", (memory["id"],)
        )
        result = doctor(self.store)
        self.assertEqual(result["active_without_sources"]["count"], 1)
        self.assertEqual(
            result["active_without_sources"]["memory_ids"], [memory["id"]]
        )
        self.assertNotIn("sensitive-memory-body", str(result))

    def test_recall_is_logically_read_only_for_known_and_unknown_paths(self):
        known = Path(self.temporary.name) / "known"
        resolved = self.store.resolve_project(str(known))
        source = self.store.record_event(
            resolved["project"]["id"], "decision", "Use SQLite"
        )
        self.store.upsert_memory(
            resolved["project"]["id"], "Database", "Use SQLite",
            "decision", "active", source_event_ids=[source["id"]],
        )
        for cwd, query in (
            (known, "database"),
            (Path(self.temporary.name) / "unknown", "database"),
            (Path(self.temporary.name) / "unknown-error", "no result"),
        ):
            before_counts = self._counts()
            before_changes = self.store.conn.total_changes
            self.store.context_recall(str(cwd), query)
            self.assertEqual(before_counts, self._counts())
            self.assertEqual(before_changes, self.store.conn.total_changes)


if __name__ == "__main__":
    unittest.main()
