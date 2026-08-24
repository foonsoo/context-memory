import tempfile
import unittest
from pathlib import Path

from context_memory.store import MemoryStore


class MaintenanceRepositoryTests(unittest.TestCase):
    def test_policy_and_audit_reads_live_behind_facade(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.db")
            try:
                project = store.create_project("maintenance-repository")
                store.record_event(project["id"], "fact", "Evidence")

                self.assertEqual(
                    store.maintenance.get_policy(project["id"])["project_id"],
                    project["id"],
                )
                self.assertEqual(
                    len(store.maintenance.audit_entries(project["id"])), 2
                )
                self.assertEqual(
                    store.maintenance.audit_checkpoints(project["id"]), []
                )
            finally:
                store.close()
