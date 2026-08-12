import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from context_memory.store import MemoryStore


class CheckpointResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "state" / "memory.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_interim_checkpoint_survives_restart_and_retry(self):
        first = MemoryStore(self.database)
        project = first.create_project("restart-checkpoint")
        session = first.start_session(project["id"], "test", external_id="restart")
        evidence = first.record_event(project["id"], "decision", "Persist recovery state", session_id=session["id"])
        arguments = dict(
            project_id=project["id"], mode="interim", reason="material_change",
            goal="Survive restart", idempotency_key="restart-checkpoint-1",
            session_id=session["id"], completed=["Stored evidence"],
            next_step="Resume after restart", source_event_cursor=evidence["event_seq"],
        )
        created = first.create_checkpoint(**arguments)
        first.close()

        restored = MemoryStore(self.database)
        try:
            self.assertEqual(restored.create_checkpoint(**arguments), created)
            source = restored.get_source(created["checkpoint_id"])
            payload = json.loads(source["metadata_json"])["checkpoint"]
            self.assertEqual(payload["next_step"], "Resume after restart")
            self.assertEqual(payload["claims"], {"completion": False, "verification": False})
            self.assertIsNone(restored._row("SELECT ended_at FROM sessions WHERE id=?", (session["id"],))["ended_at"])
            evaluated = restored.evaluate_checkpoint(
                project["id"], context_usage=.9, session_id=session["id"],
                goal="Survive restart", completed=["Stored evidence"], next_step="Resume after restart",
            )
            self.assertFalse(evaluated["should_checkpoint"])
            self.assertEqual(evaluated["suppression"], "unchanged_recovery_state")
        finally:
            restored.close()

    def test_concurrent_clients_share_one_idempotent_checkpoint(self):
        setup = MemoryStore(self.database)
        project = setup.create_project("concurrent-checkpoint")
        session = setup.start_session(project["id"], "test", external_id="concurrent")
        setup.record_event(project["id"], "decision", "Deduplicate concurrent writers", session_id=session["id"])
        setup.close()
        barrier = threading.Barrier(2)

        def create() -> dict:
            store = MemoryStore(self.database)
            try:
                barrier.wait(timeout=5)
                return store.create_checkpoint(
                    project["id"], "interim", "manual", "Concurrent recovery",
                    "shared-concurrent-key", session_id=session["id"], next_step="Continue once",
                )
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(results[0], results[1])
        verify = MemoryStore(self.database)
        try:
            self.assertEqual(verify.conn.execute(
                "SELECT count(*) FROM events WHERE project_id=? AND kind='checkpoint'", (project["id"],)
            ).fetchone()[0], 1)
            self.assertEqual(verify.conn.execute(
                "SELECT count(*) FROM idempotency_keys WHERE operation='create_checkpoint' AND key='shared-concurrent-key'"
            ).fetchone()[0], 1)
        finally:
            verify.close()

    def test_missing_usage_uses_elapsed_event_and_age_fallbacks(self):
        store = MemoryStore(self.database)
        try:
            project = store.create_project("missing-usage")
            session = store.start_session(project["id"], "test", external_id="elapsed")
            store.set_policy(project["id"], checkpoint_elapsed_seconds=60, checkpoint_event_count=2,
                             checkpoint_max_age_seconds=60, checkpoint_cooldown_seconds=0)
            old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
            store.conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (old, session["id"]))
            elapsed = store.evaluate_checkpoint(project["id"], session_id=session["id"], goal="Fallback")
            self.assertIsNone(elapsed["signals"]["context_usage"])
            self.assertEqual(elapsed["trigger"], "elapsed")
            created = store.create_checkpoint(
                project["id"], "interim", "elapsed", "Fallback", elapsed["suggested_idempotency_key"],
                session_id=session["id"],
            )
            store.conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), session["id"]))
            store.record_event(project["id"], "fact", "one", session_id=session["id"])
            self.assertFalse(store.evaluate_checkpoint(project["id"], session_id=session["id"], goal="Changed")["should_checkpoint"])
            store.record_event(project["id"], "fact", "two", session_id=session["id"])
            events = store.evaluate_checkpoint(project["id"], session_id=session["id"], goal="Changed")
            self.assertEqual(events["trigger"], "event_count")
            store.set_policy(project["id"], checkpoint_event_count=100)
            checkpoint_time = datetime.fromisoformat(created["created_at"])
            class FutureDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return checkpoint_time + timedelta(seconds=61)
            with patch("context_memory.store.datetime", FutureDateTime):
                aged = store.evaluate_checkpoint(project["id"], goal="Changed")
            self.assertEqual(aged["trigger"], "checkpoint_age")
            self.assertGreaterEqual(aged["signals"]["checkpoint_age_seconds"], 60)
        finally:
            store.close()

    def test_threshold_boundaries_and_cooldown_persist_across_restart(self):
        store = MemoryStore(self.database)
        project = store.create_project("threshold-boundaries")
        store.set_policy(project["id"], checkpoint_soft_usage=.6, checkpoint_hard_usage=.75,
                         checkpoint_cooldown_seconds=600, checkpoint_hysteresis=.05)
        store.record_event(project["id"], "fact", "material change")
        below = store.evaluate_checkpoint(project["id"], context_usage=.5999, goal="Boundary")
        self.assertFalse(below["should_checkpoint"])
        soft = store.evaluate_checkpoint(project["id"], context_usage=.6, goal="Boundary")
        self.assertEqual(soft["trigger"], "soft_context_usage_after_material_change")
        store.create_checkpoint(project["id"], "interim", "context_budget", "Boundary",
                                soft["suggested_idempotency_key"], context_usage=.6)
        store.record_event(project["id"], "fact", "later material change")
        store.close()

        restored = MemoryStore(self.database)
        try:
            hard = restored.evaluate_checkpoint(project["id"], context_usage=.75, goal="Boundary changed")
            self.assertFalse(hard["should_checkpoint"])
            self.assertEqual(hard["suppression"], "cooldown")
            self.assertEqual(hard["thresholds"]["checkpoint_hard_usage"], .75)
        finally:
            restored.close()

    def test_final_checkpoint_rolls_back_all_provenance_on_invalid_evidence(self):
        store = MemoryStore(self.database)
        try:
            project = store.create_project("provenance-rollback")
            other = store.create_project("other-project")
            session = store.start_session(project["id"], "test", external_id="rollback")
            foreign = store.record_event(other["id"], "deployment", "Foreign evidence")
            before = store.conn.execute("SELECT count(*) FROM events WHERE project_id=?", (project["id"],)).fetchone()[0]
            with self.assertRaisesRegex(ValueError, "invalid verified event"):
                store.create_checkpoint(
                    project["id"], "final", "completed", "Atomic final", "invalid-provenance",
                    session_id=session["id"], verified_event_ids=[foreign["id"]],
                    handoff_title="Must not exist", handoff_content="Must roll back",
                )
            self.assertEqual(store.conn.execute(
                "SELECT count(*) FROM events WHERE project_id=?", (project["id"],)
            ).fetchone()[0], before)
            self.assertEqual(store.conn.execute(
                "SELECT count(*) FROM memories WHERE project_id=?", (project["id"],)
            ).fetchone()[0], 0)
            self.assertIsNone(store._row("SELECT ended_at FROM sessions WHERE id=?", (session["id"],))["ended_at"])
            self.assertEqual(store.conn.execute(
                "SELECT count(*) FROM idempotency_keys WHERE operation='create_checkpoint' AND key='invalid-provenance'"
            ).fetchone()[0], 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
