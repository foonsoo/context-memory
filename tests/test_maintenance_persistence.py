import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

                with patch.object(
                    store.maintenance,
                    "maintain",
                    wraps=store.maintenance.maintain,
                ) as maintain:
                    plan = store.maintain(project["id"])
                maintain.assert_called_once_with(project["id"], False)
                self.assertFalse(plan["apply"])

                with patch.object(
                    store.maintenance,
                    "status",
                    wraps=store.maintenance.status,
                ) as status:
                    result = store.maintenance_status(project["id"])
                status.assert_called_once_with(project["id"])
                self.assertEqual(result["counts"]["events"], 1)

                scheduled = store.maintain_scheduled(project["id"])
                self.assertEqual(scheduled["reason"], "disabled")

                with patch.object(
                    store.maintenance,
                    "set_policy",
                    wraps=store.maintenance.set_policy,
                ) as set_policy:
                    policy = store.set_policy(
                        project["id"], max_context_items=12
                    )
                self.assertEqual(set_policy.call_count, 1)
                self.assertEqual(policy["max_context_items"], 12)

                health = store.search_health(project["id"])
                self.assertTrue(health["ok"])
            finally:
                store.close()
