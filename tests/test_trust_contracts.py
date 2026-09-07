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

    def test_import_enforces_active_sources_and_rolls_back_everything(self):
        source = MemoryStore(Path(self.temporary.name) / "source.db")
        try:
            project = source.create_project("import-source-check")
            proposed = source.upsert_memory(
                project["id"], "Draft", "source-less draft"
            )
            records = source.export_project(project["id"])
        finally:
            source.close()
        for record in records:
            if record["record_type"] == "memory":
                record["data"]["status"] = "active"
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "source event"):
            self.store.import_project(records)
        self.assertEqual(before, self._counts())

        result = self.store.import_project(
            records, allow_legacy_active_without_sources=True
        )
        warning = result["legacy_active_without_sources"]
        self.assertEqual(warning["count"], 1)
        self.assertEqual(warning["memory_ids"], [proposed["id"]])
        self.assertIn("Traceability is absent", warning["warning"])
        self.assertNotIn("source-less draft", str(result))

    def test_import_accepts_proposed_and_supported_active_memories(self):
        for slug, active in (("proposed-import", False), ("active-import", True)):
            source = MemoryStore(Path(self.temporary.name) / f"{slug}.db")
            target = MemoryStore(Path(self.temporary.name) / f"{slug}-target.db")
            try:
                project = source.create_project(slug)
                event = source.record_event(project["id"], "fact", "evidence")
                memory = source.upsert_memory(
                    project["id"],
                    "Memory",
                    "body",
                    status="active" if active else "proposed",
                    source_event_ids=[event["id"]] if active else None,
                )
                target.import_project(source.export_project(project["id"]))
                restored = target.memories.get(memory["id"])
                self.assertEqual(restored["status"], memory["status"])
            finally:
                source.close()
                target.close()

    def test_import_rejects_foreign_source_even_in_legacy_mode(self):
        foreign_project = self.store.create_project("foreign-existing")
        foreign = self.store.record_event(
            foreign_project["id"], "fact", "foreign evidence"
        )
        source = MemoryStore(Path(self.temporary.name) / "foreign-source.db")
        try:
            project = source.create_project("foreign-import")
            event = source.record_event(project["id"], "fact", "local evidence")
            memory = source.upsert_memory(
                project["id"], "Active", "body", status="active",
                source_event_ids=[event["id"]],
            )
            records = source.export_project(project["id"])
        finally:
            source.close()
        for record in records:
            if record["record_type"] == "memory_source":
                record["data"]["event_id"] = foreign["id"]
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "invalid imported source"):
            self.store.import_project(
                records, allow_legacy_active_without_sources=True
            )
        self.assertEqual(before, self._counts())
        self.assertIsNone(self.store.memories.get(memory["id"]))

    def test_import_rejects_nonexistent_source_even_in_legacy_mode(self):
        source = MemoryStore(Path(self.temporary.name) / "missing-source.db")
        try:
            project = source.create_project("missing-source-import")
            event = source.record_event(project["id"], "fact", "evidence")
            source.upsert_memory(
                project["id"], "Active", "body", status="active",
                source_event_ids=[event["id"]],
            )
            records = source.export_project(project["id"])
        finally:
            source.close()
        records = [
            record for record in records if record["record_type"] != "event"
        ]
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "invalid imported source"):
            self.store.import_project(
                records, allow_legacy_active_without_sources=True
            )
        self.assertEqual(before, self._counts())

    def test_scope_and_alias_share_one_path_owner(self):
        first = self.store.create_project("path-owner-first")
        second = self.store.create_project("path-owner-second")
        scope_path = str(Path(self.temporary.name) / "shared-scope")
        alias_path = str(Path(self.temporary.name) / "shared-alias")
        first_scope = self.store.create_scope(first["id"], "root", scope_path)
        with self.assertRaisesRegex(ValueError, "another project"):
            self.store.set_project_alias(second["id"], "path", scope_path)
        self.assertEqual(
            self.store.create_scope(first["id"], "again", scope_path)["id"],
            first_scope["id"],
        )

        self.store.set_project_alias(first["id"], "path", alias_path)
        with self.assertRaisesRegex(ValueError, "another project"):
            self.store.create_scope(second["id"], "root", alias_path)
        with self.assertRaisesRegex(ValueError, "another project"):
            self.store.create_scope(second["id"], "root", scope_path)
        with self.assertRaisesRegex(ValueError, "another project"):
            self.store.set_project_alias(second["id"], "path", alias_path)
        self.assertEqual(
            self.store.project_evidence.find_project(scope_path)["project"]["id"],
            first["id"],
        )
        moved = self.store.resolve_project(alias_path)
        self.assertEqual(moved["project"]["id"], first["id"])

    def test_conflicting_scope_import_rolls_back(self):
        owner = self.store.create_project("existing-owner")
        shared = str(Path(self.temporary.name) / "import-shared")
        self.store.set_project_alias(owner["id"], "path", shared)
        source = MemoryStore(Path(self.temporary.name) / "scope-import.db")
        try:
            project = source.create_project("scope-import")
            source.create_scope(project["id"], "root", shared)
            records = source.export_project(project["id"])
        finally:
            source.close()
        before = self._counts()
        with self.assertRaisesRegex(ValueError, "another project"):
            self.store.import_project(records)
        self.assertEqual(before, self._counts())
        found = self.store.project_evidence.find_project(shared)
        self.assertFalse(found["ambiguous"])
        self.assertEqual(found["project"]["id"], owner["id"])


if __name__ == "__main__":
    unittest.main()
