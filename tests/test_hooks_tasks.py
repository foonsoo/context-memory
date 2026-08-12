import tempfile
import unittest
from pathlib import Path

from context_memory.hooks import checkpoint_from_hook
from context_memory.store import MemoryStore
from context_memory.tasks import checkpoint_task


class HookAndTaskAdaptersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.db")
        self.project = self.store.create_project("adapters")
        self.session = self.store.start_session(self.project["id"], "codex", external_id="hook")

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_hook_uses_checkpoint_policy_and_same_core_operation(self):
        self.store.set_policy(self.project["id"], checkpoint_event_count=1)
        self.store.record_event(self.project["id"], "decision", "Use lifecycle adapter", session_id=self.session["id"])
        result = checkpoint_from_hook(self.store, self.project["id"], self.session["id"], {
            "context_memory": {"goal": "Ship adapters", "next_step": "Run tests"}
        })
        self.assertEqual(result["mode"], "interim")
        self.assertFalse(result["claims"]["completion"])
        self.assertIsNone(self.store.conn.execute("SELECT ended_at FROM sessions WHERE id=?", (self.session["id"],)).fetchone()[0])

    def test_hook_does_nothing_when_policy_does_not_trigger(self):
        self.assertIsNone(checkpoint_from_hook(
            self.store, self.project["id"], self.session["id"],
            {"context_memory": {"goal": "No material change"}},
        ))

    def test_tasks_adapter_publishes_status_and_returns_core_result(self):
        statuses = []
        result = checkpoint_task(self.store, {
            "project_id": self.project["id"], "mode": "interim", "reason": "manual",
            "goal": "Expose task adapter", "idempotency_key": "task-adapter",
            "session_id": self.session["id"], "next_step": "Integrate in host",
        }, lambda status, detail: statuses.append((status, detail)))
        self.assertEqual([item[0] for item in statuses], ["working", "completed"])
        self.assertEqual(statuses[-1][1]["checkpoint_id"], result["checkpoint_id"])


if __name__ == "__main__": unittest.main()
