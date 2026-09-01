import json
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
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

    def test_local_hash_embeddings_are_enabled_by_default_and_can_be_disabled(self):
        self.assertIsInstance(self.store.embedding_provider, LocalHashEmbedding)
        self.assertEqual(self.store._provider_name(), "local-hash-v2-1024")
        self.store.close()
        with patch.dict("os.environ", {"CONTEXT_MEMORY_EMBEDDINGS":"off"}):
            self.store = MemoryStore(Path(self.temp.name) / "disabled" / "memory.db")
        self.assertIsNone(self.store.embedding_provider)
        self.assertIsNone(self.store._provider_name())

    def test_event_is_append_only_and_idempotent(self):
        p = self.store.create_project("demo")
        first = self.store.record_event(p["id"], "observation", "SQLite is selected", idempotency_key="evt-1")
        again = self.store.record_event(p["id"], "observation", "SQLite is selected", idempotency_key="evt-1")
        self.assertEqual(first, again)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        with self.assertRaises(ValueError): self.store.record_event(p["id"], "observation", "Different", idempotency_key="evt-1")
        with self.assertRaises(sqlite3.IntegrityError): self.store.conn.execute("UPDATE events SET content='changed' WHERE id=?", (first["id"],))

    def test_checkpoint_is_idempotent_and_preserves_session_and_recovery_state(self):
        p = self.store.create_project("checkpoint")
        session = self.store.start_session(p["id"], "test", external_id="checkpoint-session")
        source = self.store.record_event(p["id"], "decision", "Use explicit checkpoints", session_id=session["id"])
        first = self.store.create_checkpoint(
            p["id"], "interim", "material_change", "Implement checkpoint core", "checkpoint-1",
            session["id"], completed=["Store API implemented"], next_step="Expose MCP tool",
            blockers=[], source_event_cursor=source["event_seq"], context_usage=.61)
        again = self.store.create_checkpoint(
            p["id"], "interim", "material_change", "Implement checkpoint core", "checkpoint-1",
            session["id"], completed=["Store API implemented"], next_step="Expose MCP tool",
            blockers=[], source_event_cursor=source["event_seq"], context_usage=.61)
        self.assertEqual(first, again)
        self.assertEqual(first["source_event_cursor"], source["event_seq"])
        self.assertIsNone(self.store._row("SELECT ended_at FROM sessions WHERE id=?", (session["id"],))["ended_at"])
        event = self.store.get_source(first["checkpoint_id"])
        self.assertEqual(event["kind"], "checkpoint")
        self.assertEqual(json.loads(event["metadata_json"])["checkpoint"]["next_step"], "Expose MCP tool")
        with self.assertRaisesRegex(ValueError, "different request"):
            self.store.create_checkpoint(p["id"], "final", "completed", "Different", "checkpoint-1")
        with self.assertRaisesRegex(ValueError, "context_usage"):
            self.store.create_checkpoint(p["id"], "interim", "manual", "Goal", "bad-usage", context_usage=1.1)
        automatic = self.store.create_checkpoint(p["id"], "interim", "elapsed", "Keep working", "automatic-cursor")
        self.store.record_event(p["id"], "fact", "A later event must not break checkpoint retries")
        retried = self.store.create_checkpoint(p["id"], "interim", "elapsed", "Keep working", "automatic-cursor")
        self.assertEqual(automatic, retried)

    def test_checkpoint_separates_repository_facts_and_supplied_test_results(self):
        p = self.store.create_project("checkpoint-objective")
        repository = Path(self.temp.name) / "repo"; repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        (repository / "tracked.txt").write_text("first\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True)
        (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (repository / "new.txt").write_text("new\n", encoding="utf-8")
        result = self.store.create_checkpoint(
            p["id"], "interim", "material_change", "Objective checkpoint", "objective-1",
            repository_path=str(repository), test_results=[
                {"name":"unit tests", "status":"passed", "command":"python -m unittest", "details":"51 tests"},
                {"name":"integration", "status":"skipped"},
            ])
        objective = result["objective"]
        self.assertEqual(result["schema_version"], 5)
        self.assertEqual(objective["repository"]["branch"], "main")
        self.assertTrue(objective["repository"]["dirty"])
        self.assertEqual({item["path"] for item in objective["repository"]["changed_files"]}, {"tracked.txt", "new.txt"})
        self.assertEqual(objective["test_results"][0]["status"], "passed")
        event = self.store.get_source(result["checkpoint_id"])
        self.assertEqual(json.loads(event["content"])["objective"], objective)

    def test_interim_checkpoint_cannot_end_session_mutate_git_or_promote_working_state(self):
        p = self.store.create_project("checkpoint-interim-guardrails")
        session = self.store.start_session(p["id"], "test", external_id="interim-guardrails")
        proposed = self.store.upsert_memory(
            p["id"], "Unverified working state", "Implementation may be ready", "task", "proposed")
        repository = Path(self.temp.name) / "guarded-repo"; repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        (repository / "working.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "working.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=repository, check=True, capture_output=True)
        (repository / "working.txt").write_text("uncommitted\n", encoding="utf-8")
        before_git = subprocess.run(
            ["git", "status", "--porcelain=v2", "--branch"], cwd=repository,
            check=True, capture_output=True, text=True).stdout

        checkpoint = self.store.create_checkpoint(
            p["id"], "interim", "material_change", "Continue implementation", "interim-guardrails",
            session_id=session["id"], repository_path=str(repository),
            completed=["Drafted implementation"], next_step="Verify behavior",
            test_results=[{"name":"focused tests", "status":"passed"}])

        after_git = subprocess.run(
            ["git", "status", "--porcelain=v2", "--branch"], cwd=repository,
            check=True, capture_output=True, text=True).stdout
        self.assertEqual(before_git, after_git)
        self.assertIsNone(self.store._row("SELECT ended_at FROM sessions WHERE id=?", (session["id"],))["ended_at"])
        self.assertEqual(self.store._row("SELECT status FROM memories WHERE id=?", (proposed["id"],))["status"], "proposed")
        self.assertEqual(checkpoint["claims"], {"completion": False, "verification": False})
        self.assertEqual(checkpoint["objective"]["test_results"][0]["status"], "passed")
        with self.assertRaisesRegex(ValueError, "cannot claim completed"):
            self.store.create_checkpoint(p["id"], "interim", "completed", "Done", "interim-completed")
        self.store.end_session(session["id"])
        with self.assertRaisesRegex(ValueError, "active session"):
            self.store.create_checkpoint(
                p["id"], "interim", "manual", "Resume", "interim-ended-session", session_id=session["id"])

    def test_final_checkpoint_atomically_replaces_handoff_links_evidence_and_ends_session(self):
        p = self.store.create_project("checkpoint-final")
        session = self.store.start_session(p["id"], "test", external_id="final-checkpoint")
        evidence = self.store.record_event(p["id"], "deployment", "Tests passed for commit", session_id=session["id"])
        previous = self.store.upsert_memory(
            p["id"], "Previous handoff", "Continue final semantics", "task", "active",
            source_event_ids=[evidence["id"]])
        repository = Path(self.temp.name) / "final-repo"; repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        (repository / "done.txt").write_text("done\n", encoding="utf-8")
        subprocess.run(["git", "add", "done.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "done"], cwd=repository, check=True, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()

        result = self.store.create_checkpoint(
            p["id"], "final", "completed", "Finish final semantics", "final-1",
            session_id=session["id"], completed=["All tests passed"], repository_path=str(repository),
            verified_event_ids=[evidence["id"]], handoff_title="Next implementation handoff",
            handoff_content="Final semantics shipped; implement hooks next", previous_handoff_memory_id=previous["id"],
            commit=head[:12], test_results=[{"name":"unit tests", "status":"passed"}])

        self.assertTrue(result["session_ended"]); self.assertEqual(result["commit"], head)
        self.assertIsNotNone(self.store._row("SELECT ended_at FROM sessions WHERE id=?", (session["id"],))["ended_at"])
        self.assertEqual(self.store._row("SELECT status FROM memories WHERE id=?", (previous["id"],))["status"], "superseded")
        handoff = self.store._row("SELECT * FROM memories WHERE id=?", (result["handoff_memory_id"],))
        self.assertEqual(handoff["status"], "active")
        sources = {row[0] for row in self.store.conn.execute("SELECT event_id FROM memory_sources WHERE memory_id=?", (handoff["id"],))}
        self.assertEqual(sources, {evidence["id"], result["checkpoint_id"]})
        edge = self.store._row("SELECT * FROM edges WHERE from_memory_id=? AND to_memory_id=?", (handoff["id"], previous["id"]))
        self.assertEqual(edge["relation"], "supersedes")
        self.assertEqual(result, self.store.create_checkpoint(
            p["id"], "final", "completed", "Finish final semantics", "final-1",
            session_id=session["id"], completed=["All tests passed"], repository_path=str(repository),
            verified_event_ids=[evidence["id"]], handoff_title="Next implementation handoff",
            handoff_content="Final semantics shipped; implement hooks next", previous_handoff_memory_id=previous["id"],
            commit=head[:12], test_results=[{"name":"unit tests", "status":"passed"}]))

    def test_checkpoint_rejects_invalid_objective_evidence(self):
        p = self.store.create_project("checkpoint-invalid-objective")
        with self.assertRaisesRegex(ValueError, "test result status"):
            self.store.create_checkpoint(p["id"], "interim", "manual", "Goal", "bad-test",
                                         test_results=[{"name":"unit", "status":"maybe"}])
        with self.assertRaisesRegex(ValueError, "Git worktree"):
            self.store.create_checkpoint(p["id"], "interim", "manual", "Goal", "bad-repo",
                                         repository_path=self.temp.name)

    def test_checkpoint_evaluation_uses_context_thresholds_and_material_change(self):
        p = self.store.create_project("checkpoint-thresholds")
        self.store.set_policy(p["id"], checkpoint_soft_usage=.55, checkpoint_hard_usage=.8)
        quiet = self.store.evaluate_checkpoint(p["id"], context_usage=.6)
        self.assertFalse(quiet["should_checkpoint"])
        self.store.record_event(p["id"], "observation", "Material progress")
        soft = self.store.evaluate_checkpoint(p["id"], context_usage=.6)
        self.assertTrue(soft["should_checkpoint"]); self.assertEqual(soft["trigger"], "soft_context_usage_after_material_change")
        hard = self.store.evaluate_checkpoint(p["id"], context_usage=.8)
        self.assertTrue(hard["should_checkpoint"]); self.assertEqual(hard["trigger"], "hard_context_usage")
        with self.assertRaisesRegex(ValueError, "less than"):
            self.store.set_policy(p["id"], checkpoint_soft_usage=.9)

    def test_checkpoint_evaluation_uses_event_and_repository_fallbacks(self):
        p = self.store.create_project("checkpoint-fallbacks")
        self.store.set_policy(p["id"], checkpoint_event_count=2)
        self.store.record_event(p["id"], "observation", "one")
        self.assertFalse(self.store.evaluate_checkpoint(p["id"])["should_checkpoint"])
        self.store.record_event(p["id"], "observation", "two")
        events = self.store.evaluate_checkpoint(p["id"])
        self.assertEqual(events["trigger"], "event_count")
        repository = Path(self.temp.name) / "fallback-repo"; repository.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        (repository / "tracked.txt").write_text("first\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True)
        repository_signal = self.store.evaluate_checkpoint(p["id"], repository_path=str(repository))
        self.assertTrue(repository_signal["signals"]["repository_changed"])

    def test_checkpoint_evaluation_suppresses_unchanged_cooldown_and_hysteresis(self):
        p = self.store.create_project("checkpoint-storm-control")
        self.store.set_policy(p["id"], checkpoint_cooldown_seconds=300, checkpoint_hysteresis=.05)
        self.store.record_event(p["id"], "observation", "initial progress")
        created = self.store.create_checkpoint(
            p["id"], "interim", "context_budget", "Ship safely", "storm-1",
            completed=["core"], next_step="tests", context_usage=.61)
        unchanged = self.store.evaluate_checkpoint(
            p["id"], context_usage=.8, goal="Ship safely", completed=["core"], next_step="tests")
        self.assertFalse(unchanged["should_checkpoint"])
        self.assertEqual(unchanged["suppression"], "unchanged_recovery_state")
        self.assertEqual(unchanged["recovery_hash"], created["recovery_hash"])
        self.assertEqual(unchanged["suggested_idempotency_key"],
                         self.store.evaluate_checkpoint(p["id"], context_usage=.8, goal="Ship safely", completed=["core"], next_step="tests")["suggested_idempotency_key"])
        self.store.record_event(p["id"], "observation", "more progress")
        cooldown = self.store.evaluate_checkpoint(p["id"], context_usage=.8, goal="Ship safely", completed=["core"], next_step="tests")
        self.assertEqual(cooldown["suppression"], "cooldown")
        self.store.set_policy(p["id"], checkpoint_cooldown_seconds=0)
        hysteresis = self.store.evaluate_checkpoint(p["id"], context_usage=.64, goal="Ship safely", completed=["core"], next_step="tests")
        self.assertEqual(hysteresis["suppression"], "hysteresis")
        advanced = self.store.evaluate_checkpoint(p["id"], context_usage=.67, goal="Ship safely", completed=["core"], next_step="tests")
        self.assertTrue(advanced["should_checkpoint"])
        self.assertEqual(advanced["trigger"], "soft_context_usage_after_material_change")


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

    def test_durable_event_receipts_redeliver_until_acknowledged(self):
        p = self.store.create_project("durable-receipts")
        first = self.store.record_event(p["id"], "message", "first")
        second = self.store.record_event(p["id"], "message", "second")
        delivered = self.store.poll_events(p["id"], "worker-a", ["message"], limit=1)
        self.assertEqual([event["id"] for event in delivered["events"]], [first["id"]])
        repeated = self.store.poll_events(p["id"], "worker-a", ["message"], limit=1)
        self.assertEqual([event["id"] for event in repeated["events"]], [first["id"]])
        receipt = self.store.acknowledge_events(p["id"], "worker-a", delivered["next_cursor"], ["message"])
        self.assertEqual(receipt["acknowledged_cursor"], first["event_seq"])
        resumed = self.store.poll_events(p["id"], "worker-a", ["message"])
        self.assertEqual([event["id"] for event in resumed["events"]], [second["id"]])
        with self.assertRaisesRegex(ValueError, "beyond the delivered"):
            self.store.acknowledge_events(p["id"], "worker-a", second["event_seq"] + 1, ["message"])
        independent = self.store.poll_events(p["id"], "worker-b", ["message"])
        self.assertEqual([event["id"] for event in independent["events"]], [first["id"], second["id"]])

    def test_message_expiry_policy_hides_expired_messages_without_deleting_events(self):
        p = self.store.create_project("message-expiry")
        self.store.set_policy(p["id"], message_ttl_seconds=60)
        future = self.store.record_event(p["id"], "message", "temporary")
        self.assertIn("expires_at", json.loads(future["metadata_json"]))
        expired = self.store.record_event(p["id"], "message", "expired", metadata={"expires_at":"2000-01-01T00:00:00+00:00"})
        events = self.store.read_events_since(p["id"], 0, ["message"])["events"]
        self.assertEqual([event["id"] for event in events], [future["id"]])
        self.assertEqual(self.store.get_source(expired["id"])["content"], "expired")

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

    def test_compact_context_is_lossless_and_omits_duplicate_rendering(self):
        p = self.store.create_project("compact")
        e = self.store.record_event(p["id"], "decision", "Keep the exact source")
        m = self.store.upsert_memory(p["id"], "Exact title", "Exact stored content", "decision", "active",
                                     .9, .8, source_event_ids=[e["id"]])
        legacy = self.store.get_context(p["id"], "Exact", 1000)
        compact = self.store.get_context(p["id"], "Exact", 1000, response_format="compact")
        self.assertIn("context", legacy)
        self.assertNotIn("context", compact)
        item = compact["items"][0]
        self.assertEqual((item["memory_id"], item["status"], item["confidence"]), (m["id"], "active", .9))
        self.assertEqual((item["title"], item["content"]), ("Exact title", "Exact stored content"))
        self.assertEqual(item["source_event_ids"], [e["id"]])
        self.assertFalse(item["truncated"])
        self.assertEqual(self.store.get_source(item["source_event_ids"][0])["content"], "Keep the exact source")

    def test_decision_context_reconstructs_cited_current_choice_and_history(self):
        p = self.store.create_project("decision-brief")
        old_event = self.store.record_event(p["id"], "decision", "Originally choose files")
        old = self.store.upsert_memory(p["id"], "Old choice", "Use flat files", "decision", "superseded",
                                       source_event_ids=[old_event["id"]], observed_at="2026-01-01T00:00:00+00:00")
        current_event = self.store.record_event(p["id"], "decision", "Choose SQLite for transactions")
        current = self.store.upsert_memory(p["id"], "Current choice", "Use SQLite", "decision", "active",
                                           source_event_ids=[current_event["id"]], observed_at="2026-02-01T00:00:00+00:00")
        reason_event = self.store.record_event(p["id"], "fact", "Concurrent writes need transactions")
        self.store.upsert_memory(p["id"], "Storage rationale", "Storage transactions prevent partial writes", "fact", "active",
                                 source_event_ids=[reason_event["id"]], tags=["rationale"])
        rejected_event = self.store.record_event(p["id"], "decision", "Postgres adds operations overhead")
        rejected = self.store.upsert_memory(p["id"], "Storage alternative", "Do not operate Postgres storage", "decision", "rejected",
                                            source_event_ids=[rejected_event["id"]], tags=["alternative"])

        brief = self.store.decision_context(p["id"], "storage choice", 5000)

        self.assertEqual(brief["contract_version"], "decision-brief/v1")
        self.assertEqual([item["citations"]["memory_id"] for item in brief["current_decisions"]], [current["id"]])
        self.assertEqual(brief["rationale"][0]["citations"]["source_event_ids"], [reason_event["id"]])
        self.assertEqual(brief["alternatives"][0]["citations"]["memory_id"], rejected["id"])
        self.assertEqual([item["citations"]["memory_id"] for item in brief["history"]], [old["id"], current["id"], rejected["id"]])
        self.assertIsNone(brief["recommendation"])
        self.assertNotIn("missing_rationale", [item["reason"] for item in brief["uncertainty"]])
        self.assertEqual({item["memory_id"] for item in brief["retrieval"]["items"]},
                         {old["id"], current["id"], rejected["id"], brief["rationale"][0]["citations"]["memory_id"]})

    def test_decision_context_labels_disputes_proposals_and_missing_sources(self):
        p = self.store.create_project("decision-uncertainty")
        disputed = self.store.upsert_memory(p["id"], "Contested", "Launch date is Friday", "decision", "disputed")
        proposed = self.store.upsert_memory(p["id"], "Possible outcome", "Conversion may improve", "fact", "proposed", tags=["outcome"])
        brief = self.store.decision_context(p["id"], "launch outcome", 3000)
        self.assertEqual(brief["disputes"][0]["citations"]["memory_id"], disputed["id"])
        reasons = {(item["citations"] or {}).get("memory_id"): item["reason"] for item in brief["uncertainty"]}
        self.assertIn(reasons[proposed["id"]], {"unreviewed_proposed_memory", "missing_source_event"})
        self.assertIn("missing_source_event", [item["reason"] for item in brief["uncertainty"]])

    def test_decision_context_labels_missing_rationale(self):
        p = self.store.create_project("decision-missing-rationale")
        event = self.store.record_event(p["id"], "decision", "Choose the Vega format")
        self.store.upsert_memory(p["id"], "Vega format", "Use Parquet for Vega exports", "decision", "active",
                                 source_event_ids=[event["id"]])
        brief = self.store.decision_context(p["id"], "Vega export format", 3000)
        self.assertIn("missing_rationale", [item["reason"] for item in brief["uncertainty"]])

    def test_decision_context_reranks_only_bounded_candidates_with_exposed_components(self):
        p = self.store.create_project("decision-rerank")
        handoff = self.store.upsert_memory(
            p["id"], "Cache latency handoff summary", "Cache latency cache latency next step", "summary", "active",
            tags=["handoff", "summary"],
        )
        event = self.store.record_event(p["id"], "decision", "Choose the cache after measuring latency")
        decision = self.store.upsert_memory(
            p["id"], "Cache choice", "Choose the cache for lower latency", "decision", "active",
            source_event_ids=[event["id"]],
        )

        search_ids = [item["id"] for item in self.store.search(p["id"], "cache latency")]
        brief = self.store.decision_context(p["id"], "cache latency decision", 5000, discover_projects=False)

        self.assertEqual(brief["retrieval"]["decision_rerank"], {
            "mode":"bounded_post_retrieval", "candidate_count":2, "general_search_unchanged":True,
        })
        self.assertEqual(brief["retrieval"]["items"][0]["memory_id"], decision["id"])
        rerank = brief["retrieval"]["items"][0]["decision_rerank"]
        self.assertEqual(rerank["roles"], ["decision"])
        self.assertAlmostEqual(rerank["score"], sum(
            value for name, value in rerank["components"].items() if name != "total"))
        handoff_item = next(item for item in brief["retrieval"]["items"] if item["memory_id"] == handoff["id"])
        self.assertLess(handoff_item["decision_rerank"]["components"]["repetitive_handoff_penalty"], 0)
        self.assertEqual(search_ids, [item["id"] for item in self.store.search(p["id"], "cache latency")])

    def test_decision_context_expands_one_hop_from_current_decision_within_budget(self):
        p = self.store.create_project("decision-expansion")
        decision_event = self.store.record_event(p["id"], "decision", "Choose SQLite storage")
        decision = self.store.upsert_memory(p["id"], "Storage choice", "Choose SQLite storage", "decision", "active",
                                            source_event_ids=[decision_event["id"]])
        rationale_event = self.store.record_event(p["id"], "fact", "Transactions avoid partial writes")
        rationale = self.store.upsert_memory(p["id"], "Transaction rationale", "Transactions avoid partial writes",
                                             "fact", "active", source_event_ids=[rationale_event["id"]],
                                             tags=["rationale"])
        unrelated = self.store.upsert_memory(p["id"], "Unrelated", "Unrelated evidence", "fact", "active")
        self.store.create_relation(p["id"], rationale["id"], decision["id"], "supports")
        brief = self.store.decision_context(p["id"], "SQLite storage decision", 5000, discover_projects=False)

        expanded = next(item for item in brief["retrieval"]["items"] if item["memory_id"] == rationale["id"])
        self.assertEqual(expanded["decision_expansion"]["depth"], 1)
        self.assertEqual(expanded["decision_expansion"]["paths"][0]["relation"], "supports")
        self.assertEqual(expanded["decision_expansion"]["paths"][0]["direction"], "incoming")
        self.assertNotIn(unrelated["id"], {item["memory_id"] for item in brief["retrieval"]["items"]})
        self.assertEqual(brief["retrieval"]["decision_expansion"]["added"], 1)
        self.assertLessEqual(brief["retrieval"]["used"], brief["retrieval"]["budget"])

    def test_decision_context_expands_shared_investigation_and_respects_tiny_budget(self):
        p = self.store.create_project("investigation-expansion")
        investigation = self.store.create_investigation(p["id"], "Which queue?", "Retries", "Choose queue", initiator="user")
        recorded = self.store.record_source_analysis(investigation["id"], {
            "source_type":"benchmark", "stable_source_id":"queue-test", "source_version":"1",
            "access_reason":"Measure retries", "analysis_method":"benchmark",
        }, [
            {"key":"evidence", "role":"evidence", "content":"Duplicate deliveries were safely absorbed", "memory_status":"active"},
            {"key":"decision", "role":"decision", "content":"Choose the durable queue",
             "evidence_claim_keys":["evidence"], "memory_status":"active"},
        ])
        decision_id = recorded["claims"][1]["memory_id"]
        evidence_id = recorded["claims"][0]["memory_id"]
        brief = self.store.decision_context(p["id"], "durable queue decision", 5000, discover_projects=False)
        expanded = next(item for item in brief["retrieval"]["items"] if item["memory_id"] == evidence_id)
        self.assertEqual(expanded["decision_expansion"]["paths"][0]["kind"], "investigation_relation")
        self.assertIn(decision_id, brief["retrieval"]["decision_expansion"]["seed_memory_ids"])

        tiny = self.store.decision_context(p["id"], "durable queue decision", 400, discover_projects=False)
        self.assertLessEqual(tiny["retrieval"]["used"], tiny["retrieval"]["budget"])

    def test_research_provenance_records_cited_chain_and_source_versions(self):
        p = self.store.create_project("research-chain")
        investigation = self.store.create_investigation(
            p["id"], "Which queue should billing use?", "Retries can duplicate charges",
            "Select the billing queue", ["No external service"], "user", idempotency_key="intent-1")
        source = {"source_type":"documentation","stable_source_id":"billing-queue-page",
                  "canonical_uri":"https://example.invalid/billing","source_version":"v3",
                  "access_reason":"Verify delivery guarantees","analysis_method":"manual claim extraction"}
        claims = [
            {"key":"evidence","role":"evidence","content":"The durable queue provides at-least-once delivery", "memory_status":"active"},
            {"key":"inference","role":"inference","content":"Idempotent consumers are required", "evidence_claim_keys":["evidence"]},
            {"key":"decision","role":"decision","content":"Use the durable queue for billing", "evidence_claim_keys":["evidence","inference"]},
        ]
        recorded = self.store.record_source_analysis(investigation["id"], source, claims)
        chain = self.store.get_investigation(investigation["id"])
        self.assertEqual(chain["contract_version"], "research-provenance/v1")
        self.assertEqual(chain["investigation"]["constraints"], ["No external service"])
        self.assertEqual([claim["role"] for claim in chain["source_analyses"][0]["claims"]],
                         ["evidence", "inference", "decision"])
        by_role = {claim["role"]: claim for claim in chain["source_analyses"][0]["claims"]}
        self.assertEqual(self.store.get_source(by_role["evidence"]["event_id"])["source_uri"], source["canonical_uri"])
        inference = self.store._row("SELECT * FROM memories WHERE id=?", (by_role["inference"]["memory_id"],))
        self.assertEqual(inference["status"], "proposed")
        self.assertEqual(by_role["decision"]["evidence"][0]["relation"], "informed")
        repeated = self.store.record_source_analysis(investigation["id"], source, claims)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["source_analysis_id"], recorded["source_analysis_id"])
        self.assertEqual([claim["claim_key"] for claim in repeated["claims"]], ["evidence", "inference", "decision"])
        newer = self.store.record_source_analysis(investigation["id"], {**source,"source_version":"v4"}, claims)
        self.assertNotEqual(newer["source_analysis_id"], recorded["source_analysis_id"])
        completed = self.store.complete_investigation(investigation["id"])
        self.assertEqual(completed["status"], "completed")
        with self.assertRaisesRegex(ValueError, "completed"):
            self.store.record_source_analysis(investigation["id"], {**source,"source_version":"v5"}, claims)

    def test_source_reinspection_request_is_explicit_append_only_and_idempotent(self):
        project = self.store.create_project("source-reinspection")
        investigation = self.store.create_investigation(
            project["id"], "Is the policy current?", "The decision cites it", "Keep or revise guidance")
        recorded = self.store.record_source_analysis(investigation["id"], {
            "source_type":"documentation", "stable_source_id":"policy", "canonical_uri":"https://example.invalid/policy",
            "source_version":"v1", "access_reason":"Verify policy", "analysis_method":"client extraction",
        }, [{"key":"policy","role":"evidence","content":"The current limit is ten"}])
        analysis_id = recorded["source_analysis_id"]

        requested = self.store.request_source_reinspection(
            analysis_id, "newer_version_known", "Client observed a release notice", "v2", "same-request")
        repeated = self.store.request_source_reinspection(
            analysis_id, "newer_version_known", "Client observed a release notice", "v2", "same-request")
        self.assertEqual(requested, repeated)
        self.assertEqual(requested["execution"], {"owner":"client","core_fetch_performed":False,"state":"requested"})
        self.assertEqual(requested["source"]["inspected_source_version"], "v1")
        chain = self.store.get_investigation(investigation["id"])["source_analyses"][0]
        self.assertEqual(chain["reinspection_requests"][0]["known_source_version"], "v2")
        with self.assertRaisesRegex(ValueError, "required"):
            self.store.request_source_reinspection(analysis_id, "newer_version_known")
        with self.assertRaisesRegex(ValueError, "only valid"):
            self.store.request_source_reinspection(analysis_id, "old", known_source_version="v2")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute("UPDATE source_reinspection_requests SET reason='old' WHERE id=?", (requested["id"],))
        records = self.store.export_project(project["id"])
        self.assertIn("source_reinspection_request", {item["record_type"] for item in records})
        restored = MemoryStore(Path(self.temp.name) / "source-reinspection-import.db")
        try:
            restored.import_project(records)
            restored_request = restored.get_investigation(investigation["id"])["source_analyses"][0]["reinspection_requests"][0]
            self.assertEqual(restored_request["id"], requested["id"])
        finally:
            restored.close()

    def test_research_provenance_rolls_back_invalid_claim_batch(self):
        p = self.store.create_project("research-rollback")
        investigation = self.store.create_investigation(p["id"], "Question", "Reason", "Decision", initiator="user")
        source = {"source_type":"web","stable_source_id":"page","source_version":"1",
                  "access_reason":"Research decision","analysis_method":"extract claims"}
        with self.assertRaisesRegex(ValueError, "earlier claims"):
            self.store.record_source_analysis(investigation["id"], source, [
                {"key":"bad","role":"decision","content":"Choose it","evidence_claim_keys":["missing"]}
            ])
        self.assertEqual(self.store.get_investigation(investigation["id"])["source_analyses"], [])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM events WHERE project_id=?", (p["id"],)).fetchone()[0], 0)

    def test_later_outcome_links_to_decision_and_brief_compares_results(self):
        p = self.store.create_project("decision-outcome")
        investigation = self.store.create_investigation(p["id"], "Ship the cache?", "Latency is high", "Release cache", initiator="user")
        first = self.store.record_source_analysis(investigation["id"], {
            "source_type":"benchmark","stable_source_id":"baseline","source_version":"1",
            "access_reason":"Measure latency","analysis_method":"benchmark",
        }, [
            {"key":"baseline","role":"evidence","content":"p95 latency is 800 ms","memory_status":"active"},
            {"key":"ship","role":"decision","content":"Ship the cache","expected_outcome":"p95 below 300 ms",
             "evidence_claim_keys":["baseline"],"memory_status":"active"},
        ])
        decision = first["claims"][1]
        second = self.store.record_source_analysis(investigation["id"], {
            "source_type":"benchmark","stable_source_id":"followup","source_version":"1",
            "access_reason":"Verify outcome","analysis_method":"benchmark",
        }, [
            {"key":"measured","role":"evidence","content":"p95 latency is 240 ms","memory_status":"active"},
            {"key":"result","role":"outcome","content":"Latency fell to 240 ms","outcome_effect":"confirms",
             "evidence_claim_keys":["measured"],"evidence_claim_refs":[{"source_analysis_id":first["source_analysis_id"],"claim_key":"ship"}],
             "memory_status":"active"},
        ])
        chain = self.store.get_investigation(investigation["id"])
        outcome = chain["source_analyses"][1]["claims"][1]
        self.assertEqual({item["claim_key"] for item in outcome["evidence"]}, {"measured", "ship"})
        brief = self.store.decision_context(p["id"], "cache latency", 6000)
        comparison = brief["expected_vs_observed"][0]
        self.assertEqual((comparison["expected_outcome"], comparison["observed_outcome"], comparison["effect"]),
                         ("p95 below 300 ms", "Latency fell to 240 ms", "confirms"))
        self.assertEqual(comparison["decision_citation"]["memory_id"], decision["memory_id"])
        self.assertEqual(comparison["outcome_citation"]["memory_id"], second["claims"][1]["memory_id"])

    def test_cross_analysis_claim_reference_must_stay_in_investigation(self):
        p = self.store.create_project("isolated-investigations")
        first = self.store.create_investigation(p["id"], "First?", "Reason", "Decision", initiator="user")
        second = self.store.create_investigation(p["id"], "Second?", "Reason", "Decision", initiator="user")
        prior = self.store.record_source_analysis(first["id"], {
            "source_type":"test","stable_source_id":"one","source_version":"1",
            "access_reason":"Test isolation","analysis_method":"fixture",
        }, [{"key":"fact","role":"evidence","content":"First fact"}])
        with self.assertRaisesRegex(ValueError, "existing claim in this investigation"):
            self.store.record_source_analysis(second["id"], {
                "source_type":"test","stable_source_id":"two","source_version":"1",
                "access_reason":"Test isolation","analysis_method":"fixture",
            }, [{"key":"decision","role":"decision","content":"Invalid decision",
                 "evidence_claim_refs":[{"source_analysis_id":prior["source_analysis_id"],"claim_key":"fact"}]}])
        self.assertEqual(self.store.get_investigation(second["id"])["source_analyses"], [])

    def test_topic_wiki_revision_lifecycle_staleness_and_markdown(self):
        p = self.store.create_project("topic-wiki")
        event = self.store.record_event(p["id"], "decision", "Use SQLite for durable local writes")
        decision = self.store.upsert_memory(p["id"], "Storage choice", "Use SQLite", "decision", "active",
                                            source_event_ids=[event["id"]])
        reason_event = self.store.record_event(p["id"], "fact", "Transactions prevent partial writes")
        self.store.upsert_memory(p["id"], "Storage rationale", "Transactions prevent partial writes", "fact", "active",
                                 source_event_ids=[reason_event["id"]], tags=["rationale"])
        page = self.store.create_wiki_page(p["id"], "storage", "Storage architecture", idempotency_key="page")
        self.store.set_wiki_notes(page["id"], "Keep the migration checklist here.")
        revision = self.store.generate_wiki_revision(page["id"], "storage choice", generation_metadata={"client":"test"})
        self.assertEqual(revision["status"], "proposed")
        self.assertEqual(revision["sections"]["current_position"][0]["claim"], "Use SQLite")
        self.assertTrue(revision["citations"])
        published = self.store.transition_wiki_revision(revision["id"], "published")
        self.assertEqual(published["status"], "published")
        rendered = self.store.render_wiki_revision(revision["id"])["markdown"]
        self.assertIn("# Storage architecture", rendered)
        self.assertIn("memory:" + decision["id"], rendered)
        self.assertIn("Keep the migration checklist here.", rendered)
        changed = self.store.transition(decision["id"], "superseded")
        self.assertEqual(changed["stale_wiki_revision_ids"], [revision["id"]])
        stale = self.store.get_wiki_page(page["id"])["revisions"][0]
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["stale_reason"], "cited memory became superseded")

    def test_publishing_new_wiki_revision_stales_prior_and_preserves_manual_notes(self):
        p = self.store.create_project("wiki-history")
        event = self.store.record_event(p["id"], "decision", "Keep the current API")
        decision = self.store.upsert_memory(p["id"], "API choice", "Keep the current API", "decision", "active",
                                            source_event_ids=[event["id"]])
        rationale_event = self.store.record_event(p["id"], "fact", "Clients remain compatible")
        self.store.upsert_memory(p["id"], "API rationale", "Clients remain compatible", "fact", "active",
                                 source_event_ids=[rationale_event["id"]], tags=["rationale"])
        page = self.store.create_wiki_page(p["id"], "api", "API")
        self.store.set_wiki_notes(page["id"], "Owner: platform")
        first = self.store.generate_wiki_revision(page["id"], "API choice")
        self.store.transition_wiki_revision(first["id"], "published")
        second = self.store.generate_wiki_revision(page["id"], "API choice")
        self.store.transition_wiki_revision(second["id"], "published")
        result = self.store.get_wiki_page(page["id"])
        self.assertEqual([item["status"] for item in result["revisions"]], ["stale", "published"])
        self.assertEqual(result["manual_notes"], "Owner: platform")
        self.store.upsert_memory(p["id"], "API choice", "Adopt the revised API", "decision", "active",
                                 memory_id=decision["id"])
        self.assertEqual(self.store.get_wiki_revision(second["id"])["stale_reason"], "cited memory materially updated")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute("UPDATE wiki_revisions SET sections_json='{}' WHERE id=?", (second["id"],))

    def test_wiki_revision_lint_reports_lifecycle_and_omission_findings(self):
        p = self.store.create_project("wiki-lint")
        event = self.store.record_event(p["id"], "decision", "Use SQLite for storage")
        cited = self.store.upsert_memory(p["id"], "Storage", "Use SQLite", "decision", "active",
                                         source_event_ids=[event["id"]])
        omitted_event = self.store.record_event(p["id"], "fact", "SQLite storage requires backups")
        omitted = self.store.upsert_memory(p["id"], "SQLite backups", "SQLite storage requires backups", "fact", "active",
                                           source_event_ids=[omitted_event["id"]])
        page = self.store.create_wiki_page(p["id"], "sqlite storage", "Storage")
        revision = self.store.generate_wiki_revision(page["id"], "SQLite storage")
        initial = self.store.lint_wiki_revision(revision["id"])
        self.assertTrue(initial["deterministic"])
        self.assertFalse(initial["state_changed"])
        omitted_findings = [item for item in initial["findings"] if item["code"] == "omitted_current_memory"]
        self.assertIn(omitted["id"], {item["memory_id"] for item in omitted_findings})

        self.store.transition_wiki_revision(revision["id"], "published")
        self.store.transition(cited["id"], "disputed")
        linted = self.store.lint_wiki_revision(revision["id"])
        codes = {item["code"] for item in linted["findings"]}
        self.assertIn("unresolved_dispute", codes)
        self.assertIn("stale_revision", codes)
        self.assertEqual(linted["status"], "fail")

        self.store.transition(omitted["id"], "superseded")
        audit_count = self.store.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        first = self.store.lint_wiki_revision(revision["id"])
        second = self.store.lint_wiki_revision(revision["id"])
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "fail")
        self.assertEqual(first["check_mode"], "deterministic_rules")
        self.assertTrue(first["deterministic"])
        self.assertFalse(first["model_assisted"])
        self.assertFalse(first["state_changed"])
        self.assertFalse(first["autonomous_state_changes"])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0], audit_count)

    def test_wiki_revision_lint_does_not_flag_workflow_tasks_as_omitted_claims(self):
        p = self.store.create_project("wiki-task-lint")
        decision_event = self.store.record_event(p["id"], "decision", "Use the deployed Wiki review workflow")
        self.store.upsert_memory(p["id"], "Review workflow", "Use the deployed Wiki review workflow",
                                 "decision", "active", source_event_ids=[decision_event["id"]])
        task_event = self.store.record_event(p["id"], "task", "Next: observe review friction")
        task = self.store.upsert_memory(p["id"], "Review handoff", "Next: observe review friction",
                                        "task", "active", source_event_ids=[task_event["id"]],
                                        tags=["handoff", "checkpoint"])
        page = self.store.create_wiki_page(p["id"], "wiki review workflow", "Wiki review")
        revision = self.store.generate_wiki_revision(page["id"], "Wiki review workflow")
        omitted = [item["memory_id"] for item in self.store.lint_wiki_revision(revision["id"])["findings"]
                   if item["code"] == "omitted_current_memory"]
        self.assertNotIn(task["id"], omitted)

    def test_wiki_revision_lint_bounds_omissions_to_leading_lexical_matches(self):
        p = self.store.create_project("wiki-bounded-omission")
        decision_event = self.store.record_event(p["id"], "decision", "Use SQLite storage")
        self.store.upsert_memory(p["id"], "Storage", "Use SQLite storage", "decision", "active",
                                 source_event_ids=[decision_event["id"]])
        omitted_event = self.store.record_event(p["id"], "fact", "A remote repository exists")
        omitted = self.store.upsert_memory(p["id"], "Repository", "A remote repository exists", "fact", "active",
                                           source_event_ids=[omitted_event["id"]])
        page = self.store.create_wiki_page(p["id"], "sqlite storage", "Storage")
        revision = self.store.generate_wiki_revision(page["id"], "SQLite storage")

        low_rank = dict(omitted)
        low_rank["retrieval"] = {"lexical_rank": 11}
        with patch.object(self.store, "search", return_value=[low_rank]):
            findings = self.store.lint_wiki_revision(revision["id"])["findings"]
        self.assertNotIn("omitted_current_memory", {item["code"] for item in findings})

        leading = dict(omitted)
        leading["retrieval"] = {"lexical_rank": 10}
        with patch.object(self.store, "search", return_value=[leading]):
            findings = self.store.lint_wiki_revision(revision["id"])["findings"]
        self.assertIn(omitted["id"], {item.get("memory_id") for item in findings
                                      if item["code"] == "omitted_current_memory"})

    def test_wiki_revision_generation_failure_exposes_retrieval_diagnostics(self):
        p = self.store.create_project("wiki-generation-diagnostics")
        event = self.store.record_event(p["id"], "task", "Observe deployed review friction")
        self.store.upsert_memory(p["id"], "Review task", "Observe deployed review friction", "task", "active",
                                 source_event_ids=[event["id"]])
        page = self.store.create_wiki_page(p["id"], "review friction", "Review friction")
        with self.assertRaisesRegex(ValueError, r"retrieval_gate=accepted; retrieved_items=1; .*hint="):
            self.store.generate_wiki_revision(page["id"], "Observe deployed review friction")

    def test_wiki_revision_lint_requires_unsupported_recommendations_to_be_inferences(self):
        p = self.store.create_project("wiki-recommendation-lint")
        decision_event = self.store.record_event(p["id"], "decision", "Keep the current deployment")
        support = self.store.upsert_memory(p["id"], "Deployment", "Keep the current deployment", "decision", "active",
                                           source_event_ids=[decision_event["id"]])
        recommendation_event = self.store.record_event(p["id"], "fact", "We should migrate immediately")
        recommendation = self.store.upsert_memory(
            p["id"], "Migration advice", "We should migrate immediately", "fact", "active",
            source_event_ids=[recommendation_event["id"]], tags=["rationale"])
        page = self.store.create_wiki_page(p["id"], "deployment migrate", "Deployment")
        revision = self.store.generate_wiki_revision(page["id"], "deployment migrate")

        linted = self.store.lint_wiki_revision(revision["id"])
        recommendation_findings = [item for item in linted["findings"]
                                   if item.get("memory_id") == recommendation["id"]]
        self.assertEqual({item["code"] for item in recommendation_findings},
                         {"recommendation_mislabeled_as_evidence", "unsupported_recommendation"})
        self.assertTrue(all(item["required_label"] == "inference" for item in recommendation_findings))
        self.assertTrue(linted["deterministic"])
        self.assertFalse(linted["state_changed"])

        self.store.create_relation(p["id"], support["id"], recommendation["id"], "supports")
        supported_codes = {item["code"] for item in self.store.lint_wiki_revision(revision["id"])["findings"]
                           if item.get("memory_id") == recommendation["id"]}
        self.assertEqual(supported_codes, {"recommendation_mislabeled_as_evidence"})

    def test_wiki_revision_lint_reports_source_age_only_as_reinspection_prompt(self):
        p = self.store.create_project("wiki-source-age")
        investigation = self.store.create_investigation(
            p["id"], "Which deployment policy?", "Guidance may age", "Choose a policy", [], "test")
        recorded = self.store.record_source_analysis(investigation["id"], {
            "source_type":"documentation", "stable_source_id":"deployment-policy", "source_version":"v1",
            "canonical_uri":"https://example.invalid/deployment", "retrieved_at":"2000-01-01T00:00:00+00:00",
            "access_reason":"Check deployment guidance", "analysis_method":"manual claim extraction",
        }, [
            {"key":"guidance", "role":"evidence", "content":"Staged deployment limits rollout risk", "memory_status":"active"},
            {"key":"policy", "role":"decision", "content":"Use staged deployment",
             "evidence_claim_keys":["guidance"], "memory_status":"active"},
        ])
        memory_id = recorded["claims"][1]["memory_id"]
        page = self.store.create_wiki_page(p["id"], "Use staged deployment", "Deployment")
        revision = self.store.generate_wiki_revision(page["id"], "Use staged deployment")

        findings = [item for item in self.store.lint_wiki_revision(revision["id"])["findings"]
                    if item["code"] == "source_reinspection_due"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["memory_id"], memory_id)
        self.assertEqual(findings[0]["prompt"], "reinspect_source_version")
        self.assertFalse(findings[0]["external_change_verified"])
        self.assertGreaterEqual(findings[0]["age_days"], findings[0]["threshold_days"])
        self.assertNotIn("stale_revision", {item["code"] for item in self.store.lint_wiki_revision(revision["id"])["findings"]})

    def test_review_queue_includes_latest_wiki_revision_lint_and_explicit_routes(self):
        p = self.store.create_project("wiki-review-queue")
        event = self.store.record_event(p["id"], "decision", "Use SQLite storage")
        self.store.upsert_memory(p["id"], "Storage", "Use SQLite storage", "decision", "active",
                                 source_event_ids=[event["id"]])
        page = self.store.create_wiki_page(p["id"], "SQLite storage", "Storage")
        first = self.store.generate_wiki_revision(page["id"], "SQLite storage")
        second = self.store.generate_wiki_revision(page["id"], "SQLite storage")

        queued = [item for item in self.store.review_queue(p["id"])
                  if item["review_kind"] == "wiki_revision"]
        self.assertEqual([item["id"] for item in queued], [second["id"]])
        self.assertNotEqual(queued[0]["id"], first["id"])
        self.assertEqual(queued[0]["lint"]["contract_version"], "topic-wiki-lint/v1")
        self.assertEqual(queued[0]["available_actions"], [
            {"action":"approve", "tool":"wiki_revision_transition", "arguments":{"status":"published"}},
            {"action":"reject", "tool":"wiki_revision_transition", "arguments":{"status":"rejected"}},
        ])

        self.store.transition_wiki_revision(second["id"], "published")
        self.assertEqual([item for item in self.store.review_queue(p["id"])
                          if item["review_kind"] == "wiki_revision"], [])

    def test_review_queue_prioritizes_actionable_wiki_revision_over_old_memory_candidates(self):
        p = self.store.create_project("wiki-review-priority")
        old_event = self.store.record_event(p["id"], "fact", "Old candidate")
        self.store.upsert_memory(p["id"], "Old candidate", "Old candidate", "fact", "proposed",
                                 source_event_ids=[old_event["id"]])
        decision_event = self.store.record_event(p["id"], "decision", "Use SQLite")
        self.store.upsert_memory(p["id"], "Storage", "Use SQLite", "decision", "active",
                                 source_event_ids=[decision_event["id"]])
        page = self.store.create_wiki_page(p["id"], "storage", "Storage")
        revision = self.store.generate_wiki_revision(page["id"], "storage decision")
        queue = self.store.review_queue(p["id"])
        self.assertEqual(queue[0]["id"], revision["id"])
        self.assertEqual(queue[0]["review_kind"], "wiki_revision")
        self.assertLess(queue[0]["queue_priority"], queue[1]["queue_priority"])

    def test_wiki_lint_allows_terminal_memories_only_in_history_or_alternatives(self):
        p = self.store.create_project("wiki-terminal-history")
        old_event = self.store.record_event(p["id"], "decision", "Use files")
        old = self.store.upsert_memory(p["id"], "Old storage", "Use files", "decision", "active",
                                       source_event_ids=[old_event["id"]])
        new_event = self.store.record_event(p["id"], "decision", "Use SQLite")
        self.store.upsert_memory(p["id"], "Current storage", "Use SQLite", "decision", "active",
                                 source_event_ids=[new_event["id"]])
        self.store.transition(old["id"], "superseded")
        page = self.store.create_wiki_page(p["id"], "storage decision", "Storage")
        revision = self.store.generate_wiki_revision(page["id"], "storage decision history")
        terminal = [item for item in self.store.lint_wiki_revision(revision["id"])["findings"]
                    if item["code"] == "terminal_memory" and item["memory_id"] == old["id"]]
        self.assertEqual(terminal, [])

    def test_topic_wiki_export_import_round_trip(self):
        p = self.store.create_project("wiki-export")
        event = self.store.record_event(p["id"], "decision", "Choose option A")
        self.store.upsert_memory(p["id"], "Choice", "Choose option A", "decision", "active", source_event_ids=[event["id"]])
        reason = self.store.record_event(p["id"], "fact", "Option A is supported")
        self.store.upsert_memory(p["id"], "Why", "Option A is supported", "fact", "active",
                                 source_event_ids=[reason["id"]], tags=["rationale"])
        page = self.store.create_wiki_page(p["id"], "choice", "Choice")
        revision = self.store.generate_wiki_revision(page["id"], "option choice")
        records = self.store.export_project(p["id"])
        other = MemoryStore(Path(self.temp.name) / "wiki-import.db")
        try:
            other.import_project(records)
            restored = other.get_wiki_page(page["id"])
            self.assertEqual(restored["revisions"][0]["id"], revision["id"])
            self.assertEqual(restored["revisions"][0]["citations"], revision["citations"])
        finally:
            other.close()

    def test_wiki_browse_indexes_pages_and_exposes_reverse_citation_backlinks(self):
        p = self.store.create_project("wiki-navigation")
        shared_event = self.store.record_event(p["id"], "constraint", "Shared deployment requires an audit trail")
        shared = self.store.upsert_memory(p["id"], "Shared deployment constraint",
                                          "Shared deployment requires an audit trail", "constraint", "active",
                                          source_event_ids=[shared_event["id"]])
        decision_event = self.store.record_event(p["id"], "decision", "Shared deployment uses staged rollout")
        self.store.upsert_memory(p["id"], "Shared deployment rollout", "Shared deployment uses staged rollout",
                                 "decision", "active", source_event_ids=[decision_event["id"]])
        pages = [self.store.create_wiki_page(p["id"], topic, title) for topic,title in
                 (("deployment approvals","Approvals"),("deployment rollout","Rollout"),("operations","Operations"))]
        for page in pages[:2]:
            revision = self.store.generate_wiki_revision(page["id"], "shared deployment")
            self.store.transition_wiki_revision(revision["id"], "published")

        first = self.store.browse_wiki(p["id"], limit=2)
        self.assertEqual([item["topic"] for item in first["topic_index"]],
                         ["deployment approvals","deployment rollout"])
        self.assertTrue(first["has_more"]); self.assertEqual(first["next_offset"], 2)
        second = self.store.browse_wiki(p["id"], limit=2, offset=first["next_offset"])
        self.assertEqual([item["topic"] for item in second["pages"]], ["operations"])
        self.assertEqual(second["pages"][0]["reader_state"], "no_current_revision")
        self.assertFalse(second["pages"][0]["renderable"])
        self.assertEqual(second["renderable_page_count"], 0)
        self.assertEqual(second["unrenderable_page_count"], 1)
        self.assertFalse(second["has_more"])

        statements = []
        self.store.conn.set_trace_callback(statements.append)
        selected = self.store.browse_wiki(p["id"], page_id=pages[0]["id"])["selected"]
        self.store.conn.set_trace_callback(None)
        shared_links = next(item for item in selected["backlinks"] if item["memory_id"] == shared["id"])
        self.assertEqual([item["page_id"] for item in shared_links["pages"]], [pages[1]["id"]])
        self.assertFalse(shared_links["has_more"])
        self.assertFalse(self.store.browse_wiki(p["id"])["search_index_duplicated"])
        read_statements = [item for item in statements if item.lstrip().upper().startswith(("SELECT", "WITH"))]
        self.assertLessEqual(len(read_statements), 9)

    def test_wiki_markdown_export_has_stable_metadata_and_navigation(self):
        p = self.store.create_project("wiki-markdown-export")
        event = self.store.record_event(p["id"], "constraint", "Deployments require approval")
        self.store.upsert_memory(p["id"], "Approval", "Deployments require approval", "constraint", "active",
                                 source_event_ids=[event["id"]])
        pages = [self.store.create_wiki_page(p["id"], topic, title) for topic,title in
                 (("approval flow","Approvals"),("rollout flow","Rollout"),("unused","No revision"))]
        revisions = []
        for page in pages[:2]:
            revision = self.store.generate_wiki_revision(page["id"], "deployment approval")
            revisions.append(self.store.transition_wiki_revision(revision["id"], "published"))

        exported = self.store.export_wiki_markdown(p["id"])
        self.assertEqual(exported["contract_version"], "topic-wiki-export/v1")
        self.assertEqual(exported["page_count"], 2)
        self.assertEqual(exported["source_page_count"], 3)
        self.assertEqual(exported["skipped_page_count"], 1)
        self.assertEqual(exported, self.store.export_wiki_markdown(p["id"]))
        self.assertFalse(exported["markdown_writable_authority"])
        self.assertEqual(exported["authoritative_source"], "sqlite")
        first,second = exported["documents"]
        self.assertEqual(first["path"], f"pages/{pages[0]['id']}.md")
        self.assertIn(f"page_id: {pages[0]['id']}", first["markdown"])
        self.assertIn(f"revision_id: {revisions[0]['id']}", first["markdown"])
        self.assertIn("[Wiki index](../index.md)", first["markdown"])
        self.assertIn(f"[Next](../pages/{pages[1]['id']}.md)", first["markdown"])
        self.assertIn(f"[Previous](../pages/{pages[0]['id']}.md)", second["markdown"])
        self.assertIn(f"[Rollout](../pages/{pages[1]['id']}.md)", first["markdown"])
        self.assertIn(f"[Approvals](pages/{pages[0]['id']}.md)", exported["index"]["markdown"])

        bounded = self.store.export_wiki_markdown(p["id"], limit=1)
        self.assertTrue(bounded["has_more"]); self.assertEqual(bounded["next_offset"], 1)

    def test_research_provenance_export_import_round_trip(self):
        p = self.store.create_project("research-export")
        investigation = self.store.create_investigation(p["id"], "Question", "Reason", "Decision", initiator="user")
        self.store.record_source_analysis(investigation["id"], {
            "source_type":"paper","stable_source_id":"paper-1","content_fingerprint":"sha256:abc",
            "access_reason":"Compare options","analysis_method":"structured reading"
        }, [{"key":"evidence","role":"evidence","content":"Observed result"}])
        records = self.store.export_project(p["id"])
        self.assertIn("source_analysis", {record["record_type"] for record in records})
        other = MemoryStore(Path(self.temp.name) / "research-import.db")
        try:
            other.import_project(records)
            restored = other.get_investigation(investigation["id"])
            self.assertEqual(restored["source_analyses"][0]["stable_source_id"], "paper-1")
            self.assertEqual(restored["source_analyses"][0]["claims"][0]["role"], "evidence")
        finally:
            other.close()

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

    def test_project_identity_resolves_unique_registered_name_to_another_path(self):
        first_path = Path(self.temp.name) / "first" / "context-memory"
        second_path = Path(self.temp.name) / "second" / "context-memory"
        first_path.mkdir(parents=True); second_path.mkdir(parents=True)
        first = self.store.resolve_project(str(first_path))
        second = self.store.resolve_project(str(second_path))
        self.assertEqual(second["project"]["id"], first["project"]["id"])
        self.assertEqual(second["matched_by"], "name")
        aliases = self.store.list_project_aliases(first["project"]["id"])
        self.assertEqual(len([alias for alias in aliases if alias["kind"] == "path"]), 2)

    def test_context_falls_back_to_cross_project_and_always_merges_global(self):
        official = self.store.create_project("official")
        empty = self.store.create_project("empty")
        shared = self.store.create_project("shared")
        checkpoint = self.store.upsert_memory(official["id"], "Implementation checkpoint",
                                              "Cross project discovery is the next implementation checkpoint", "task", "active")
        global_memory = self.store.upsert_memory(shared["id"], "Global preference",
                                                "Cross project replies use Korean", "preference", "active", visibility="global")
        result = self.store.get_context(empty["id"], "Cross project", 2000)
        self.assertTrue(result["project_discovery"]["used"])
        self.assertEqual(result["project_discovery"]["selected_project_id"], official["id"])
        self.assertEqual(result["project_discovery"]["selection_reason"], "single_confident_candidate")
        self.assertEqual({item["memory_id"] for item in result["items"]}, {checkpoint["id"], global_memory["id"]})
        self.assertEqual(set(result["project_discovery"]["project_ids"]), {official["id"], shared["id"]})

    def test_context_keeps_project_boundary_when_local_results_exist(self):
        local = self.store.create_project("local-boundary")
        other = self.store.create_project("other-boundary")
        own = self.store.upsert_memory(local["id"], "Local checkpoint", "Use SQLite checkpoint", "task", "active")
        self.store.upsert_memory(other["id"], "Other checkpoint", "Use SQLite checkpoint", "task", "active")
        result = self.store.get_context(local["id"], "SQLite checkpoint", 2000)
        self.assertFalse(result["project_discovery"]["used"])
        self.assertFalse(result["project_discovery"]["ambiguous"])
        self.assertEqual(result["project_discovery"]["project_ids"], [])
        self.assertEqual([item["memory_id"] for item in result["items"]], [own["id"]])

    def test_cross_project_discovery_searches_whole_db_and_aggregates_projects(self):
        hinted = self.store.create_project("hinted-empty", "context-memory")
        official = self.store.create_project("official-memory", "context-memory")
        unrelated = self.store.create_project("asset-manager", "asset-manager")
        session = self.store.start_session(official["id"], "codex", external_id="whole-db")
        expected = self.store.upsert_memory(official["id"], "Next checkpoint", "Implement project discovery discovery", "task", "active")
        self.store.upsert_memory(unrelated["id"], "Next checkpoint", "Implement project discovery", "task", "active")
        result = self.store.get_context(hinted["id"], "project discovery", 2000)
        self.assertFalse(result["project_discovery"]["ambiguous"])
        self.assertEqual(result["project_discovery"]["selected_project_id"], official["id"])
        self.assertEqual(result["project_discovery"]["selection_reason"], "dominant_candidate")
        self.assertEqual([item["memory_id"] for item in result["items"]], [expected["id"]])
        candidates = result["project_discovery"]["candidates"]
        self.assertEqual({candidate["id"] for candidate in candidates}, {official["id"], unrelated["id"]})
        official_candidate = next(candidate for candidate in candidates if candidate["id"] == official["id"])
        self.assertEqual(official_candidate["latest_checkpoint"]["id"], expected["id"])
        self.assertEqual(official_candidate["recent_activity_at"], session["started_at"])
        self.assertGreater(official_candidate["relevance"], 0)
        self.assertEqual(official_candidate["identity_prior"], .15)
        self.assertGreater(official_candidate["confidence"], candidates[1]["confidence"])

    def test_memory_search_discovery_is_not_limited_by_matching_project_name(self):
        hinted = self.store.create_project("whole-db-hint", "context-memory")
        unrelated = self.store.create_project("whole-db-target", "asset-manager")
        expected = self.store.upsert_memory(unrelated["id"], "Rare deployment clue", "quasar zebrafish rollout", "fact", "active")
        results = self.store.search(hinted["id"], "quasar zebrafish", discover_projects=True)
        self.assertEqual([memory["id"] for memory in results], [expected["id"]])

    def test_ambiguous_project_discovery_returns_candidates_without_mixing_memories(self):
        hinted = self.store.create_project("ambiguous-hint", "context-memory")
        first = self.store.create_project("ambiguous-first", "context-memory")
        second = self.store.create_project("ambiguous-second", "context-memory")
        self.store.upsert_memory(first["id"], "Checkpoint", "Implement registry discovery", "task", "active")
        self.store.upsert_memory(second["id"], "Checkpoint", "Implement registry discovery", "task", "active")
        result = self.store.get_context(hinted["id"], "registry discovery", 2000)
        self.assertTrue(result["project_discovery"]["ambiguous"])
        self.assertEqual(result["items"], [])
        self.assertEqual({p["id"] for p in result["project_discovery"]["candidates"]}, {first["id"], second["id"]})

    def test_shared_path_prior_selects_matching_project_without_prefiltering(self):
        hinted = self.store.create_project("path-prior-hint", "checkout")
        matching = self.store.create_project("path-prior-match", "different-name")
        competing = self.store.create_project("path-prior-competitor", "checkout")
        shared_path = str(Path(self.temp.name) / "shared-checkout")
        self.store.set_project_alias(hinted["id"], "path", shared_path)
        self.store.set_project_alias(matching["id"], "path", shared_path)
        expected = self.store.upsert_memory(matching["id"], "Checkpoint", "alpha release checkpoint", "task", "active")
        self.store.upsert_memory(competing["id"], "Checkpoint", "alpha release checkpoint", "task", "active")
        result = self.store.get_context(hinted["id"], "alpha release checkpoint", 2000)
        self.assertEqual(result["project_discovery"]["selected_project_id"], matching["id"])
        self.assertEqual([item["memory_id"] for item in result["items"]], [expected["id"]])
        winner = result["project_discovery"]["candidates"][0]
        self.assertEqual(winner["identity_prior"], .35)
        self.assertIn("shared_path", winner["confidence_reasons"])

    def test_low_confidence_discovery_returns_candidates_without_memory(self):
        hinted = self.store.create_project("low-confidence-hint")
        target = self.store.create_project("low-confidence-target")
        self.store.upsert_memory(target["id"], "Weak clue", "quasar", "fact", "active",
                                 confidence=0, importance=0)
        result = self.store.get_context(hinted["id"], "unrelated quasar tokens", 2000)
        self.assertTrue(result["project_discovery"]["used"])
        self.assertIsNone(result["project_discovery"]["selected_project_id"])
        self.assertEqual(result["project_discovery"]["selection_reason"], "low_confidence")
        self.assertEqual(result["items"], [])

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
        self.assertFalse(self.store.search_health(p["id"])["ok"], "direct SQL changes leave the rebuildable embedding stale")
        self.store.rebuild_fts(p["id"])
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

    def test_semantic_provider_can_add_candidate_when_fts_has_unrelated_hit(self):
        class SemanticFixture:
            dimensions = 2
            name = "semantic-fixture"
            vector_only_threshold = .8
            supplements_lexical_results = True

            def embed(self, texts):
                return [[1.0, 0.0] if "iphone" in text.casefold() or "disconnected" in text.casefold()
                        else [0.0, 1.0] for text in texts]

        self.store.close()
        self.store = MemoryStore(Path(self.temp.name) / "data" / "memory.db", SemanticFixture())
        p = self.store.create_project("semantic-gate")
        relevant = self.store.upsert_memory(p["id"], "Mobile sync", "Queue edits while disconnected", "fact", "active")
        self.store.upsert_memory(p["id"], "Work log", "Track completed work", "fact", "active")
        results = self.store.search(p["id"], "work offline on iphone", 5)
        self.assertIn(relevant["id"], [item["id"] for item in results])

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
        self.assertEqual(self.store.review_queue(p["id"])[0]["review_kind"], "memory_candidate")
        self.assertEqual(self.store.review_queue(p["id"])[0]["available_actions"],
                         ["approve", "reject", "supersede", "dispute"])
        self.assertTrue(ended["review"]["conflicts"])
        self.assertEqual(self.store._row("SELECT event_id FROM memory_sources WHERE memory_id=?", (candidate["id"],))["event_id"], event["id"])

    def test_craft_style_session_advises_non_promotable_kind_and_extracts_only_task(self):
        p = self.store.create_project("craft-kind-contract")
        session = self.store.start_session(p["id"], "craft-agent", external_id="craft-session")
        task = self.store.record_event(p["id"], "task", "Ship the Craft guide", session_id=session["id"], idempotency_key="craft-task")
        todo = self.store.record_event(p["id"], "todo", "Old clients may have used todo", session_id=session["id"], idempotency_key="craft-todo")
        replay = self.store.record_event(p["id"], "todo", "Old clients may have used todo", session_id=session["id"], idempotency_key="craft-todo")
        self.assertTrue(task["promotion"]["eligible"])
        self.assertFalse(todo["promotion"]["eligible"])
        self.assertIn("not automatically", todo["promotion"]["warning"])
        self.assertEqual(replay["promotion"], todo["promotion"])
        ended = self.store.end_session(session["id"])
        self.assertEqual([candidate["type"] for candidate in ended["review"]["created"]], ["task"])
        candidate = ended["review"]["created"][0]
        source = self.store._row("SELECT event_id FROM memory_sources WHERE memory_id=?", (candidate["id"],))
        self.assertEqual(source["event_id"], task["id"])

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

    def test_search_batches_candidate_usage_and_source_queries(self):
        p = self.store.create_project("batched-search")
        memories = []
        for index in range(8):
            event = self.store.record_event(p["id"], "fact", f"shared retrieval evidence {index}")
            memories.append(self.store.upsert_memory(
                p["id"], f"Shared result {index}", "shared retrieval content", "fact", "active",
                source_event_ids=[event["id"]]))
        self.store.record_memory_feedback(memories[0]["id"], "helpful")

        selects = []
        self.store.conn.set_trace_callback(
            lambda statement: selects.append(statement) if statement.lstrip().upper().startswith("SELECT") else None)
        try:
            results = self.store.search(p["id"], "shared retrieval", limit=8)
        finally:
            self.store.conn.set_trace_callback(None)

        self.assertEqual(len(results), 8)
        usage_by_id = {result["id"]: result["usage"] for result in results}
        self.assertEqual(usage_by_id[memories[0]["id"]]["helpful_count"], 1)
        self.assertTrue(all(len(result["sources"]) == 1 for result in results))
        self.assertEqual(sum("FROM memory_usage" in statement for statement in selects), 1)
        self.assertEqual(sum("FROM memory_sources" in statement for statement in selects), 1)

    def test_search_uses_strict_lexical_pass_when_it_fills_the_limit(self):
        p = self.store.create_project("strict-lexical")
        exact = self.store.upsert_memory(p["id"], "Checkout retry", "Prevent duplicate charges", "decision", "active")
        self.store.upsert_memory(p["id"], "Checkout only", "Unrelated checkout note", "fact", "active")
        matches = []
        self.store.conn.set_trace_callback(
            lambda statement: matches.append(statement) if "memories_fts MATCH" in statement else None)
        try:
            results = self.store.search(p["id"], "checkout duplicate", limit=1)
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertEqual(results[0]["id"], exact["id"])
        self.assertEqual(results[0]["retrieval"]["lexical_strategy"], "strict")
        self.assertEqual(len(matches), 1)

    def test_search_falls_back_to_broad_lexical_query_and_keeps_aliases_grouped(self):
        p = self.store.create_project("lexical-fallback")
        self.store.set_search_aliases(p["id"], "db", ["database"])
        exact = self.store.upsert_memory(p["id"], "Database choice", "Use sqlite persistence", "decision", "active")
        partial = self.store.upsert_memory(p["id"], "Database backup", "Nightly archive", "procedure", "active")
        results = self.store.search(p["id"], "db sqlite", limit=2)
        self.assertEqual({item["id"] for item in results}, {exact["id"], partial["id"]})
        self.assertTrue(all(item["retrieval"]["lexical_strategy"] == "broad_fallback" for item in results))

    def test_local_hash_reranks_only_the_bounded_lexical_candidates(self):
        p = self.store.create_project("bounded-local-hash")
        expected = self.store.upsert_memory(
            p["id"], "Checkout retry", "Prevent duplicate charges", "decision", "active")
        for index in range(30):
            self.store.upsert_memory(
                p["id"], f"Unrelated note {index}", f"garden archive material {index}", "fact", "active")

        embedding_selects = []
        self.store.conn.set_trace_callback(
            lambda statement: embedding_selects.append(statement)
            if "FROM memory_embeddings" in statement else None)
        try:
            result = self.store.search(p["id"], "checkout duplicate", limit=1)[0]
        finally:
            self.store.conn.set_trace_callback(None)

        self.assertEqual(result["id"], expected["id"])
        scan = result["retrieval"]["semantic_scan"]
        self.assertEqual(scan["mode"], "lexical_rerank")
        self.assertEqual(scan["evaluated"], 1)
        self.assertEqual(scan["candidate_limit"], 1)
        self.assertFalse(scan["truncated"])
        self.assertEqual(len(embedding_selects), 1)
        self.assertIn("m.id IN", embedding_selects[0])

    def test_local_hash_vector_fallback_exposes_candidate_and_time_limits(self):
        p = self.store.create_project("bounded-vector-fallback")
        expected = self.store.upsert_memory(
            p["id"], "검색 성능", "개인화된 기억을 빠르게 검색합니다", "decision", "active")

        result = self.store.search(p["id"], "개인화된기억 빠르게검색합니다", limit=1)[0]

        self.assertEqual(result["id"], expected["id"])
        scan = result["retrieval"]["semantic_scan"]
        self.assertEqual(scan["mode"], "vector_fallback")
        self.assertEqual(scan["candidate_limit"], 1000)
        self.assertEqual(scan["time_limit_ms"], 25)
        self.assertEqual(scan["evaluated"], 1)
        self.assertFalse(scan["truncated"])

    def test_cross_project_vector_fallback_scans_only_identity_candidates(self):
        hinted = self.store.create_project("empty-hint")
        target = self.store.create_project("context-memory")
        unrelated = self.store.create_project("asset-manager")
        expected = self.store.upsert_memory(
            target["id"], "검색 성능", "개인화된 기억을 빠르게 검색합니다", "decision", "active")
        self.store.upsert_memory(
            unrelated["id"], "검색 성능", "개인화된 기억을 빠르게 검색합니다", "decision", "active")

        results = self.store.search(
            hinted["id"], "context-memory 개인화된기억 빠르게검색합니다", limit=1,
            discover_projects=True)

        self.assertEqual(results[0]["id"], expected["id"])
        scan = results[0]["retrieval"]["semantic_scan"]
        self.assertEqual(scan["mode"], "vector_fallback")
        self.assertEqual(scan["project_candidate_limit"], 12)
        self.assertEqual(scan["project_candidate_ids"], [target["id"]])
        self.assertEqual(scan["evaluated"], 1)

    def test_cross_project_vector_fallback_skips_unplausible_projects(self):
        hinted = self.store.create_project("empty-hint")
        unrelated = self.store.create_project("asset-manager")
        self.store.upsert_memory(
            unrelated["id"], "검색 성능", "개인화된 기억을 빠르게 검색합니다", "decision", "active")
        embedding_selects = []
        self.store.conn.set_trace_callback(
            lambda statement: embedding_selects.append(statement)
            if "FROM memory_embeddings" in statement else None)
        try:
            results = self.store.search(
                hinted["id"], "unknown-project 개인화된기억 빠르게검색합니다", limit=1,
                discover_projects=True)
        finally:
            self.store.conn.set_trace_callback(None)

        self.assertEqual(results, [])
        self.assertEqual(len(embedding_selects), 1)
        self.assertIn("m.visibility='global'", embedding_selects[0])

    def test_context_rejects_weak_vector_only_result_and_reports_gate_evidence(self):
        p = self.store.create_project("negative-result-gate")
        self.store.upsert_memory(p["id"], "Unrelated local note", "The local project tracks garden irrigation", "fact", "active")
        result = self.store.get_context(p["id"], "Which Falcon deployment window is approved?", 2000,
                                        discover_projects=False, response_format="compact")
        self.assertEqual(result["items"], [])
        gate = result["retrieval_gate"]
        self.assertEqual(gate["status"], "no_confident_match")
        self.assertIn(gate["reason"], {"no_candidates", "weak_vector_only_similarity"})
        self.assertIsNone(gate["components"]["lexical_rank"])
        self.assertEqual(gate["components"]["query_coverage"], 0.0)
        if gate["components"]["semantic_similarity"] is not None:
            self.assertLess(gate["components"]["semantic_similarity"], gate["thresholds"]["vector_only_similarity"])

    def test_context_accepts_lexical_match_and_reports_agreement_components(self):
        p = self.store.create_project("positive-result-gate")
        expected = self.store.upsert_memory(p["id"], "Falcon deployment", "Approve the Falcon west window", "decision", "active")
        result = self.store.get_context(p["id"], "Falcon approved window", 2000,
                                        discover_projects=False, response_format="compact")
        self.assertEqual(result["items"][0]["memory_id"], expected["id"])
        self.assertEqual(result["retrieval_gate"]["status"], "accepted")
        self.assertEqual(result["retrieval_gate"]["reason"], "lexical_match")
        self.assertIsNotNone(result["retrieval_gate"]["components"]["lexical_rank"])

    def test_vector_only_gate_requires_top_result_separation(self):
        def candidate(score, similarity):
            return {"retrieval":{"score":score, "lexical_rank":None, "query_coverage":0.0,
                                 "semantic_similarity":similarity}}
        rejected = self.store._retrieval_gate([candidate(.018, .55), candidate(.017, .53)])
        self.assertEqual(rejected["reason"], "weak_vector_only_separation")
        accepted = self.store._retrieval_gate([candidate(.018, .55), candidate(.017, .40)])
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["reason"], "strong_vector_only_match")

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

    def test_audit_chain_exports_deterministically_and_verifies_offline(self):
        p = self.store.create_project("audit-offline")
        for index in range(105): self.store.record_event(p["id"], "fact", f"audit {index}")
        self.store.set_policy(p["id"], audit_keep_entries=100)
        self.store.maintain(p["id"], True)
        first = self.store.export_audit_chain(p["id"])
        self.assertEqual(first, self.store.export_audit_chain(p["id"]))
        verified = self.store.verify_audit_chain(first, first["head_digest"])
        self.assertTrue(verified["ok"]); self.assertTrue(verified["anchored"])
        tampered = json.loads(json.dumps(first)); tampered["checkpoints"][0]["digest"] = "0" * 64
        self.assertFalse(self.store.verify_audit_chain(tampered, first["head_digest"])["ok"])
        reordered = json.loads(json.dumps(first)); reordered["audit_entries"].reverse()
        self.assertFalse(self.store.verify_audit_chain(reordered)["ok"])

    def test_scheduled_maintenance_runs_once_when_due(self):
        p = self.store.create_project("scheduled")
        self.assertEqual(self.store.maintain_scheduled(p["id"])["reason"], "disabled")
        self.store.set_policy(p["id"], maintenance_interval_seconds=300)
        first = self.store.maintain_scheduled(p["id"])
        self.assertTrue(first["ran"])
        second = self.store.maintain_scheduled(p["id"])
        self.assertFalse(second["ran"]); self.assertEqual(second["reason"], "not_due")
        status = self.store.maintenance_status(p["id"])
        self.assertIsNotNone(status["schedule"]["last_completed_at"])

    def test_encrypted_backup_requires_optional_crypto_or_round_trips(self):
        p = self.store.create_project("encrypted-backup")
        event = self.store.record_event(p["id"], "fact", "encrypted evidence")
        envelope = Path(self.temp.name) / "backup.enc"
        try:
            result = self.store.backup_to(envelope, "correct horse battery staple")
        except RuntimeError as exc:
            self.assertIn("context-memory-mcp[crypto]", str(exc))
            self.assertFalse(envelope.exists())
            return
        from context_memory.backup_crypto import decrypt_file
        restored_path = Path(self.temp.name) / "restored.db"
        decrypt_file(envelope, restored_path, "correct horse battery staple")
        restored = MemoryStore(restored_path)
        try: self.assertEqual(restored.get_source(event["id"])["content"], "encrypted evidence")
        finally: restored.close()
        with self.assertRaises(ValueError): decrypt_file(envelope, Path(self.temp.name) / "wrong.db", "wrong")


if __name__ == "__main__": unittest.main()
