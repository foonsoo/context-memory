import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from context_memory.hosted_content import HostedContentStore
from context_memory.hosted_governance import (
    HostedGovernancePolicy,
    HostedGovernanceStore,
    sensitive_data_warnings,
)
from context_memory.hosted_identity import HostedIdentityStore

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


class FailingIdentity:
    def __init__(self, identity):
        self.identity = identity
        self.fail_once = True

    def __getattr__(self, name):
        return getattr(self.identity, name)

    def erase_project(self, tenant_id, project_id):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("injected identity failure")
        return self.identity.erase_project(tenant_id, project_id)


class HostedGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.now = NOW
        self.content = HostedContentStore(
            self.root / "content.db", clock=lambda: self.now
        )
        self.identity = HostedIdentityStore(
            self.root / "identity.db", clock=lambda: self.now
        )
        for tenant_id in ("tenant-a", "tenant-b"):
            self.content.provision_tenant(tenant_id)
            self.content.provision_project(tenant_id, "project-a")
            self.identity.provision_tenant(tenant_id)
            self.identity.provision_actor(tenant_id, "user-a")
            self.identity.provision_project(tenant_id, "project-a")
            self.identity.assign_role(tenant_id, "user-a", "project_reader")
            self.identity.grant_project(tenant_id, "user-a", "project-a")
        self.policy = HostedGovernancePolicy(
            collection_purpose=(
                "Preserve user-selected decision evidence and context."
            ),
            event_retention_days=30,
            backup_retention_days=7,
            storage_region="kr-seoul-1",
            storage_class="encrypted-regional",
            incident_contact="security@example.invalid",
        )
        self.governance = HostedGovernanceStore(
            self.root / "governance.db",
            self.content,
            self.identity,
            self.policy,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.governance.close()
        self.identity.close()
        self.content.close()
        self.tempdir.cleanup()

    def test_policy_summary_declares_purpose_region_retention_and_incident(
        self,
    ):
        summary = self.governance.policy_summary()
        self.assertEqual(summary["event_retention_days"], 30)
        self.assertEqual(summary["backup_retention_days"], 7)
        self.assertEqual(summary["storage_region"], "kr-seoul-1")
        self.assertIn("decision evidence", summary["collection_purpose"])
        self.assertEqual(
            summary["sensitive_data_detection"],
            "best_effort_warning_only",
        )
        self.assertTrue(summary["incident_contact"])
        self.assertTrue(summary["incident_runbook"])

    def test_retention_deletes_only_expired_tenant_events(self):
        self.content.record_event("tenant-a", "project-a", "fact", "old alpha")
        self.content.record_event(
            "tenant-b", "project-a", "fact", "old foreign"
        )
        self.now += timedelta(days=31)
        self.content.record_event(
            "tenant-a", "project-a", "fact", "current alpha"
        )
        result = self.governance.apply_event_retention("tenant-a")
        self.assertEqual(result["deleted_events"], 1)
        exported = self.governance.export_project("tenant-a", "project-a")
        self.assertEqual(
            [event["content"] for event in exported["events"]],
            ["current alpha"],
        )
        foreign = self.governance.export_project("tenant-b", "project-a")
        self.assertEqual(len(foreign["events"]), 1)

    def test_sensitive_detection_warns_without_blocking_or_echoing(self):
        content = "Contact person@example.test and password=hunter2"
        result = self.governance.record_event(
            "tenant-a", "project-a", "fact", content
        )
        self.assertTrue(result["recorded"])
        codes = {
            warning["code"] for warning in result["sensitive_data_warnings"]
        }
        self.assertEqual(codes, {"email_address", "credential_assignment"})
        serialized = json.dumps(result["sensitive_data_warnings"])
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("person@example.test", serialized)
        self.assertEqual(sensitive_data_warnings("ordinary text"), [])

    def test_user_exports_are_tenant_scoped(self):
        self.content.record_event("tenant-a", "project-a", "fact", "alpha")
        project = self.governance.export_project("tenant-a", "project-a")
        actor = self.governance.export_actor("tenant-a", "user-a")
        self.assertEqual(project["events"][0]["content"], "alpha")
        self.assertEqual(actor["roles"], ["project_reader"])
        self.assertEqual(actor["project_ids"], ["project-a"])
        self.assertNotIn(
            "tenant-b", json.dumps({"project": project, "actor": actor})
        )

    def test_backup_expiry_is_explicit_and_external_deletion_confirmed(self):
        self.governance.register_backup(
            "tenant-a",
            "backup-old",
            "tenant-a/backups/old",
            self.now - timedelta(days=8),
        )
        self.governance.register_backup(
            "tenant-a", "backup-current", "tenant-a/backups/current"
        )
        expired = self.governance.expired_backups()
        self.assertEqual(
            [item["backup_id"] for item in expired], ["backup-old"]
        )
        self.assertTrue(
            self.governance.mark_backup_deleted("tenant-a", "backup-old")
        )
        self.assertEqual(self.governance.expired_backups(), [])

    def test_project_erasure_resumes_after_cross_store_failure(self):
        self.content.record_event("tenant-a", "project-a", "fact", "erase me")
        self.governance.identity = FailingIdentity(self.identity)
        requested = self.governance.request_erasure(
            "tenant-a", "project", "project-a", "erase-project-a"
        )
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.governance.execute_erasure(requested["operation_id"])
        self.assertEqual(
            self.content.export_project("tenant-a", "project-a")["events"],
            [],
        )
        row = self.governance.connection.execute(
            "SELECT content_done, identity_done FROM hosted_erasure_journal"
        ).fetchone()
        self.assertEqual(tuple(row), (1, 0))

        completed = self.governance.execute_erasure(requested["operation_id"])
        self.assertEqual(completed["status"], "complete")
        actor = self.identity.export_actor("tenant-a", "user-a")
        self.assertEqual(actor["project_ids"], [])
        foreign = self.content.export_project("tenant-b", "project-a")
        self.assertEqual(foreign["project_id"], "project-a")

    def test_actor_erasure_removes_identity_data_not_shared_content(self):
        self.identity.issue_session(
            "tenant-a",
            "session-private",
            "user-a",
            self.now + timedelta(hours=1),
        )
        self.content.record_event(
            "tenant-a", "project-a", "fact", "shared project content"
        )
        request = self.governance.request_erasure(
            "tenant-a", "actor", "user-a", "opaque-request-actor"
        )

        result = self.governance.execute_erasure(request["operation_id"])

        self.assertEqual(result["status"], "complete")
        self.assertFalse(
            self.identity.export_actor("tenant-a", "user-a")["exists"]
        )
        project = self.content.export_project("tenant-a", "project-a")
        self.assertEqual(
            project["events"][0]["content"], "shared project content"
        )

    def test_tenant_erasure_removes_raw_identifiers_and_backup_registry(self):
        self.content.record_event(
            "tenant-a", "project-a", "fact", "tenant private content"
        )
        self.governance.register_backup(
            "tenant-a", "backup-a", "tenant-a/backups/a"
        )
        request = self.governance.request_erasure(
            "tenant-a", "tenant", None, "erase-tenant-a"
        )
        pending = self.governance.execute_erasure(request["operation_id"])
        self.assertEqual(pending["status"], "awaiting_backup_deletion")
        self.assertEqual(pending["backups"][0]["backup_id"], "backup-a")
        self.assertTrue(
            self.governance.mark_backup_deleted("tenant-a", "backup-a")
        )
        self.governance.execute_erasure(request["operation_id"])
        repeated = self.governance.request_erasure(
            "tenant-a", "tenant", None, "erase-tenant-a"
        )
        self.assertEqual(repeated["status"], "complete")
        self.assertEqual(
            self.content.export_project("tenant-a", "project-a")["events"],
            [],
        )
        self.assertFalse(
            self.identity.export_actor("tenant-a", "user-a")["exists"]
        )
        self.assertEqual(
            self.governance.connection.execute(
                """
                SELECT COUNT(*) FROM hosted_backup_registry
                WHERE tenant_id = ?
                """,
                ("tenant-a",),
            ).fetchone()[0],
            0,
        )
        receipt = dict(
            self.governance.connection.execute(
                "SELECT * FROM hosted_erasure_receipts"
            ).fetchone()
        )
        self.assertNotIn("tenant-a", json.dumps(receipt))
        foreign = self.identity.export_actor("tenant-b", "user-a")
        self.assertTrue(foreign["exists"])
