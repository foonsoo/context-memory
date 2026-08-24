import tempfile
import unittest
from pathlib import Path

from context_memory.store import MemoryStore


class CheckpointRepositoryTests(unittest.TestCase):
    def test_observes_checkpoint_policy_state_behind_facade(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.db")
            try:
                project = store.create_project("checkpoint-repository")
                session = store.start_session(project["id"])
                store.record_event(project["id"], "fact", "work")

                self.assertEqual(
                    store.checkpoints.session_start(session["id"])["project_id"],
                    project["id"],
                )
                self.assertEqual(store.checkpoints.event_cursor(project["id"]), 1)
                self.assertEqual(
                    store.checkpoints.durable_events_after(project["id"], 0), 1
                )
                self.assertIsNone(store.checkpoints.latest(project["id"]))
            finally:
                store.close()
