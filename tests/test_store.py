import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from context_memory.embeddings import LocalHashEmbedding
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

    def test_message_events_have_stable_project_cursors_and_paginate(self):
        p = self.store.create_project("messages")
        other = self.store.create_project("other")
        first = self.store.record_event(p["id"], "message", "session A is editing schema.py", metadata={"to":"all"})
        self.store.record_event(p["id"], "decision", "unrelated event")
        second = self.store.record_event(p["id"], "message", "session A finished")
        other_first = self.store.record_event(other["id"], "message", "separate project")
        self.assertEqual((first["event_seq"], second["event_seq"], other_first["event_seq"]), (1, 3, 1))
        page = self.store.read_events_since(p["id"], 0, ["message"], limit=1)
        self.assertEqual([event["id"] for event in page["events"]], [first["id"]])
        self.assertTrue(page["has_more"]); self.assertEqual(page["next_cursor"], 1)
        final = self.store.read_events_since(p["id"], page["next_cursor"], ["message"])
        self.assertEqual([event["id"] for event in final["events"]], [second["id"]])
        self.assertFalse(final["has_more"]); self.assertEqual(final["next_cursor"], 3)
        self.assertEqual(final["events"][0]["metadata"], {})

    def test_get_context_keeps_recent_messages_separate_and_budgeted(self):
        p = self.store.create_project("message-context")
        self.store.upsert_memory(p["id"], "Stable choice", "SQLite remains authoritative", "decision", "active")
        message = self.store.record_event(p["id"], "message", "Do not edit schema.py while migration is running")
        result = self.store.get_context(p["id"], "SQLite", 1000, event_cursor=0, event_char_budget=120)
        self.assertEqual(result["items"][0]["memory_id"], self.store.search(p["id"], "SQLite")[0]["id"])
        self.assertEqual(result["recent_events"][0]["event_id"], message["id"])
        self.assertEqual(result["next_event_cursor"], message["event_seq"])
        self.assertLessEqual(result["used"], result["budget"])
        self.assertEqual(self.store.search(p["id"], "schema"), [], "messages must not enter memory FTS")
        caught_up = self.store.get_context(p["id"], "SQLite", 1000, event_cursor=message["event_seq"])
        self.assertEqual(caught_up["recent_events"], [])
        self.assertEqual(caught_up["memory_budget"], caught_up["budget"], "unused event allowance must remain available")

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

    def test_fts_triggers_and_validity_keep_search_consistent(self):
        p = self.store.create_project("consistent-search")
        memory = self.store.upsert_memory(p["id"], "Original", "obsolete_token searchable wording", "fact", "active")
        self.store.conn.execute("UPDATE memories SET title='Changed',content='replacement_token searchable wording' WHERE id=?", (memory["id"],))
        self.assertEqual(self.store.search(p["id"], "obsolete_token"), [])
        self.assertEqual(self.store.search(p["id"], "replacement_token")[0]["id"], memory["id"])
        self.assertTrue(self.store.search_health(p["id"])["ok"])
        expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.store.conn.execute("UPDATE memories SET valid_until=? WHERE id=?", (expired,memory["id"]))
        self.assertEqual(self.store.search(p["id"], "replacement_token"), [])

    def test_local_similarity_projection_finds_partial_korean_wording_and_is_rebuildable(self):
        self.store.close()
        self.store = MemoryStore(Path(self.temp.name) / "data" / "memory.db", LocalHashEmbedding(128))
        p = self.store.create_project("local-similarity")
        memory = self.store.upsert_memory(p["id"], "검색 성능", "개인화된 기억을 빠르게 검색합니다", "decision", "active")
        results = self.store.search(p["id"], "개인화 기억 검색")
        self.assertEqual(results[0]["id"], memory["id"])
        self.assertIsNotNone(results[0]["retrieval"]["semantic_similarity"])
        self.store.conn.execute("DELETE FROM memory_embeddings WHERE memory_id=?", (memory["id"],))
        self.assertFalse(self.store.search_health(p["id"])["ok"])
        repaired = self.store.rebuild_fts(p["id"])
        self.assertEqual(repaired["embedded_memories"], 1)
        self.assertTrue(self.store.search_health(p["id"])["ok"])

    def test_personal_feedback_and_confirmation_metadata_affect_projection(self):
        p = self.store.create_project("feedback")
        memory = self.store.upsert_memory(p["id"], "Preferred editor", "Use Neovim for editing", "preference", "active",
                                          observed_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(memory["observed_at"], "2026-01-01T00:00:00+00:00")
        self.assertIsNotNone(memory["last_confirmed_at"])
        self.store.record_memory_feedback(memory["id"], "retrieved")
        self.store.record_memory_feedback(memory["id"], "used")
        usage = self.store.record_memory_feedback(memory["id"], "helpful")
        self.assertEqual((usage["retrieved_count"], usage["used_count"], usage["helpful_count"]), (1, 1, 1))
        result = self.store.search(p["id"], "Neovim editor")[0]
        self.assertEqual(result["usage"]["helpful_count"], 1)
        self.assertGreater(result["importance"], memory["importance"])

    def test_session_end_extracts_proposed_candidates_and_flags_conflicts(self):
        p = self.store.create_project("candidate-review")
        session = self.store.start_session(p["id"], "test", external_id="candidate-session")
        self.store.upsert_memory(p["id"], "Service port", "Service port is 8000", "decision", "active")
        event = self.store.record_event(p["id"], "decision", "Service port is 8765", session_id=session["id"])
        ended = self.store.end_session(session["id"])
        self.assertEqual(len(ended["review"]["created"]), 1)
        candidate = ended["review"]["created"][0]
        self.assertEqual(candidate["status"], "proposed")
        self.assertEqual(candidate["type"], "decision")
        self.assertEqual(candidate["id"], self.store.review_queue(p["id"])[0]["id"])
        self.assertTrue(ended["review"]["conflicts"])
        self.assertEqual(self.store._row("SELECT event_id FROM memory_sources WHERE memory_id=?", (candidate["id"],))["event_id"], event["id"])

    def test_review_correction_can_supersede_existing_memory(self):
        p = self.store.create_project("correction")
        old = self.store.upsert_memory(p["id"], "Editor", "Use Vim", "preference", "active")
        correction = self.store.propose_correction(p["id"], old["id"], "Use Neovim")
        self.store.review_candidate(correction["id"], "supersede", old["id"], "preference changed")
        self.assertEqual(self.store._row("SELECT status FROM memories WHERE id=?", (correction["id"],))["status"], "active")
        self.assertEqual(self.store._row("SELECT status FROM memories WHERE id=?", (old["id"],))["status"], "superseded")

    def test_global_visibility_is_searchable_from_another_project(self):
        owner = self.store.create_project("global-owner")
        consumer = self.store.create_project("global-consumer")
        memory = self.store.upsert_memory(owner["id"], "Preferred language", "Use Korean replies", "preference", "active", visibility="global")
        self.assertEqual(self.store.search(consumer["id"], "Korean replies")[0]["id"], memory["id"])
        local = self.store.upsert_memory(owner["id"], "Private setting", "Use secret-local-setting", "preference", "active")
        self.assertEqual(self.store.search(consumer["id"], "secret-local-setting"), [])
        self.assertIsNotNone(local)

    def test_context_deduplicates_near_identical_memories(self):
        p = self.store.create_project("dedup")
        self.store.upsert_memory(p["id"], "Database choice", "Use SQLite WAL for persistence", "decision", "active")
        self.store.upsert_memory(p["id"], "Database choice", "Use SQLite WAL for persistence", "decision", "active")
        context = self.store.get_context(p["id"], "SQLite persistence", 2000)
        self.assertEqual(len(context["items"]), 1)

    def test_feedback_changes_personal_ranking_and_exposes_score_components(self):
        p = self.store.create_project("ranking-feedback")
        first = self.store.upsert_memory(p["id"], "Editor choice", "Use editor for Python", "preference", "active")
        second = self.store.upsert_memory(p["id"], "Editor choice", "Use editor for Python", "preference", "active")
        initial = self.store.search(p["id"], "editor Python", 2)
        lower = initial[1]["id"]
        higher = initial[0]["id"]
        self.store.record_memory_feedback(lower, "helpful")
        self.store.record_memory_feedback(higher, "incorrect")
        reranked = self.store.search(p["id"], "editor Python", 2)
        self.assertEqual(reranked[0]["id"], lower)
        components = reranked[0]["retrieval"]["components"]
        self.assertEqual(set(components), {"lexical_rrf", "semantic_rrf", "importance", "confidence", "freshness", "feedback", "total"})
        self.assertAlmostEqual(reranked[0]["retrieval"]["score"], sum(value for key, value in components.items() if key != "total"))
        self.assertEqual({first["id"], second["id"]}, {item["id"] for item in reranked})

    def test_context_budget_is_capped_by_project_policy(self):
        p = self.store.create_project("bounded-context")
        self.store.upsert_memory(p["id"], "Budget", "bounded context " * 20, "constraint", "active")
        self.store.set_policy(p["id"], max_context_chars=1000, max_context_items=1)
        result = self.store.get_context(p["id"], "bounded context", 100000)
        self.assertEqual(result["requested_budget"], 100000)
        self.assertEqual(result["budget"], 1000)
        self.assertTrue(result["budget_capped"])
        self.assertLessEqual(len(result["items"]), 1)
        with self.assertRaises(ValueError): self.store.set_policy(p["id"], max_context_chars=100000)

    def test_maintenance_purges_terminal_memory_but_preserves_sources_and_checkpoints_audit(self):
        p = self.store.create_project("retention")
        source = self.store.record_event(p["id"], "fact", "original evidence remains")
        memory = self.store.upsert_memory(p["id"], "Old", "terminal memory", "fact", "superseded", source_event_ids=[source["id"]])
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        self.store.conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (old,memory["id"]))
        for index in range(105): self.store.record_event(p["id"], "noise", f"audit item {index}")
        self.store.set_policy(p["id"], audit_keep_entries=100, terminal_memory_days=1)
        plan = self.store.maintain(p["id"])
        self.assertEqual(plan["terminal_memories"], 1); self.assertGreater(plan["audit_entries_to_checkpoint"], 0)
        result = self.store.maintain(p["id"], True)
        self.assertEqual(result["terminal_memories_purged"], 1)
        self.assertIsNone(self.store._row("SELECT id FROM memories WHERE id=?", (memory["id"],)))
        self.assertEqual(self.store.get_source(source["id"])["content"], "original evidence remains")
        self.assertLessEqual(self.store.maintenance_status(p["id"])["counts"]["audit_entries"], 100)
        self.assertEqual(len(self.store.maintenance_status(p["id"])["audit_checkpoints"]), 1)
        exported = self.store.export_project(p["id"])
        restored = MemoryStore(Path(self.temp.name) / "retention-copy" / "memory.db")
        try:
            restored.import_project(exported)
            self.assertEqual(len(restored.maintenance_status(p["id"])["audit_checkpoints"]), 1)
            self.assertEqual(restored.get_policy(p["id"])["audit_keep_entries"], 100)
        finally: restored.close()
        with self.assertRaises(sqlite3.IntegrityError): self.store.conn.execute("DELETE FROM audit_log WHERE project_id=?", (p["id"],))

    def test_online_backup_captures_committed_wal_data(self):
        p = self.store.create_project("backup")
        event = self.store.record_event(p["id"], "decision", "committed WAL data")
        destination = Path(self.temp.name) / "snapshots" / "memory.db"
        result = self.store.backup_to(destination)
        self.assertTrue(result["ok"]); self.assertEqual(result["integrity"], "ok"); self.assertEqual(len(result["sha256"]), 64)
        snapshot = MemoryStore(destination)
        try: self.assertEqual(snapshot.get_source(event["id"])["content"], "committed WAL data")
        finally: snapshot.close()


if __name__ == "__main__": unittest.main()
