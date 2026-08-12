import json
import sqlite3
import subprocess
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
