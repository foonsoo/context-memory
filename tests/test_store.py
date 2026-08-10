import sqlite3
import tempfile
import unittest
from pathlib import Path

from context_memory.store import MemoryStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "data" / "memory.db")

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_wal_permissions_and_persistence(self):
        project = self.store.create_project("demo", "Demo")
        self.assertEqual(self.store.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(oct(self.store.path.parent.stat().st_mode & 0o777), "0o700")
        self.assertEqual(self.store.list_projects()[0]["id"], project["id"])

    def test_event_is_append_only_and_idempotent(self):
        p = self.store.create_project("demo")
        first = self.store.record_event(p["id"], "observation", "SQLite is selected", idempotency_key="evt-1")
        again = self.store.record_event(p["id"], "observation", "SQLite is selected", idempotency_key="evt-1")
        self.assertEqual(first, again)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        with self.assertRaises(ValueError): self.store.record_event(p["id"], "observation", "Different", idempotency_key="evt-1")
        with self.assertRaises(sqlite3.IntegrityError): self.store.conn.execute("UPDATE events SET content='changed' WHERE id=?", (first["id"],))

    def test_search_context_provenance_and_budget(self):
        p = self.store.create_project("demo")
        e = self.store.record_event(p["id"], "decision", "Use SQLite WAL for durable local writes")
        m = self.store.upsert_memory(p["id"], "Persistence choice", "SQLite WAL provides local transactional persistence",
                                     "decision", "active", .95, .9, source_event_ids=[e["id"]], tags=["sqlite", "architecture"])
        results = self.store.search(p["id"], "SQLite persistence")
        self.assertEqual(results[0]["id"], m["id"]); self.assertEqual(results[0]["sources"][0]["id"], e["id"])
        context = self.store.get_context(p["id"], "SQLite", 1000)
        self.assertEqual(m["id"], context["items"][0]["memory_id"]); self.assertLessEqual(context["used"], context["budget"])
        self.assertEqual(self.store.get_context(p["id"], "SQLite", 10)["items"], [])

    def test_proposed_excluded_and_transitions_audited(self):
        p = self.store.create_project("demo")
        old = self.store.upsert_memory(p["id"], "Old port", "The port is 8000", "fact", "active")
        replacement = self.store.upsert_memory(p["id"], "New port", "The port is 8765", "fact", "proposed")
        self.assertEqual(self.store.get_context(p["id"], "port", 1000)["items"][0]["memory_id"], old["id"])
        self.store.transition(replacement["id"], "active")
        changed = self.store.transition(old["id"], "superseded", replacement["id"], "configuration changed")
        self.assertEqual(changed["status"], "superseded")
        edge = self.store.conn.execute("SELECT * FROM edges").fetchone()
        self.assertEqual((edge["from_memory_id"], edge["to_memory_id"]), (replacement["id"], old["id"]))
        self.assertEqual([x["action"] for x in self.store.audit("memory", old["id"])], ["created", "status:superseded"])

    def test_upsert_rolls_back_if_source_invalid(self):
        p = self.store.create_project("demo")
        with self.assertRaises(ValueError): self.store.upsert_memory(p["id"], "Bad", "No evidence", source_event_ids=["missing"])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM memories").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM memories_fts").fetchone()[0], 0)

    def test_workspace_path_resolves_stably_and_separates_projects(self):
        first = self.store.resolve_project(str(Path(self.temp.name) / "project-a"))
        again = self.store.resolve_project(str(Path(self.temp.name) / "project-a"))
        other = self.store.resolve_project(str(Path(self.temp.name) / "project-b"))
        self.assertTrue(first["created"])
        self.assertEqual(first["project"]["id"], again["project"]["id"])
        self.assertNotEqual(first["project"]["id"], other["project"]["id"])

    def test_export_project_contains_provenance_and_audit(self):
        p = self.store.create_project("export-demo")
        e = self.store.record_event(p["id"], "decision", "Use an evidence ledger")
        self.store.upsert_memory(p["id"], "Ledger", "The ledger is authoritative", "decision", "active", source_event_ids=[e["id"]])
        records = self.store.export_project(p["id"])
        kinds = [record["record_type"] for record in records]
        self.assertEqual(kinds[0], "project")
        self.assertIn("event", kinds)
        self.assertIn("memory", kinds)
        self.assertIn("memory_source", kinds)
        self.assertIn("audit", kinds)
        self.assertEqual(records, self.store.export_project(p["id"]), "export must be deterministic")

    def test_export_import_round_trip(self):
        p = self.store.create_project("round-trip")
        e = self.store.record_event(p["id"], "fact", "Portable evidence")
        self.store.upsert_memory(p["id"], "Portable", "Portable memory", "fact", "active", source_event_ids=[e["id"]], tags=["export"])
        self.store.set_search_aliases(p["id"], "transfer", ["portable"])
        records = self.store.export_project(p["id"])
        other_path = Path(self.temp.name) / "other" / "memory.db"
        other = MemoryStore(other_path)
        try:
            result = other.import_project(records)
            self.assertEqual(result["records"], len(records))
            self.assertEqual(other.get_context(p["id"], "Portable", 1000)["items"][0]["text"].splitlines()[-1], f"source_events: {e['id']}")
            self.assertTrue(other.search(p["id"], "transfer"))
            with self.assertRaises(ValueError): other.import_project(records)
        finally:
            other.close()

    def test_search_aliases_expand_domain_vocabulary(self):
        p = self.store.create_project("aliases")
        e = self.store.record_event(p["id"], "decision", "SQLite provides durable persistence")
        memory = self.store.upsert_memory(p["id"], "Persistence", "SQLite provides durable persistence", "decision", "active", source_event_ids=[e["id"]])
        self.assertEqual(self.store.search(p["id"], "database"), [])
        saved = self.store.set_search_aliases(p["id"], "database", ["sqlite", "data store"])
        self.assertEqual(saved["aliases"], ["data store", "sqlite"])
        self.assertEqual(self.store.search(p["id"], "database")[0]["id"], memory["id"])
        self.assertEqual(self.store.list_search_aliases(p["id"])[0]["term"], "database")

    def test_verified_graph_traversal_hides_superseded_nodes(self):
        p = self.store.create_project("graph")
        service = self.store.upsert_memory(p["id"], "Service", "Payment service", "fact", "active")
        database = self.store.upsert_memory(p["id"], "Database", "PostgreSQL database", "fact", "active")
        region = self.store.upsert_memory(p["id"], "Region", "Seoul region", "fact", "active")
        old = self.store.upsert_memory(p["id"], "Old", "Old dependency", "fact", "superseded")
        self.store.create_relation(p["id"], service["id"], database["id"], "depends_on")
        self.store.create_relation(p["id"], database["id"], region["id"], "related_to")
        self.store.create_relation(p["id"], service["id"], old["id"], "depends_on")
        graph = self.store.traverse(p["id"], service["id"], 2, "outgoing")
        self.assertEqual({node["title"] for node in graph["nodes"]}, {"Service", "Database", "Region"})
        self.assertEqual(len(graph["edges"]), 2)
        with self.assertRaises(ValueError): self.store.traverse(p["id"], service["id"], 6)

    def test_rebuild_fts_repairs_projection(self):
        p = self.store.create_project("repair")
        memory = self.store.upsert_memory(p["id"], "Repairable", "Projection can be rebuilt", "fact", "active")
        self.store.conn.execute("DELETE FROM memories_fts WHERE memory_id=?", (memory["id"],))
        self.assertEqual(self.store.search(p["id"], "Repairable"), [])
        result = self.store.rebuild_fts(p["id"])
        self.assertEqual(result["indexed_memories"], 1)
        self.assertEqual(self.store.search(p["id"], "Repairable")[0]["id"], memory["id"])


if __name__ == "__main__": unittest.main()
