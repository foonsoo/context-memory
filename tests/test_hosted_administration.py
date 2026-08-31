import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from context_memory.hosted_administration import (
    HostedAdministrationDeniedError,
    HostedAdministrationGateway,
)
from context_memory.hosted_authorization import HostedSession
from context_memory.hosted_identity import HostedIdentityStore

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def session(**overrides):
    values = {
        "actor_id": "admin-a",
        "tenant_id": "tenant-a",
        "session_id": "admin-session",
        "roles": frozenset({"tenant_admin"}),
        "project_ids": frozenset(),
    }
    values.update(overrides)
    return HostedSession(**values)


class HostedAdministrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = HostedIdentityStore(
            Path(self.tempdir.name) / "identity.db", lambda: NOW
        )
        for tenant_id in ("tenant-a", "tenant-b"):
            self.store.provision_tenant(tenant_id)
        self.store.provision_actor("tenant-a", "admin-a")
        self.store.provision_actor("tenant-a", "user-a")
        self.gateway = HostedAdministrationGateway(self.store)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_tenant_admin_can_create_and_grant_with_audit(self):
        self.gateway.create_project(
            session(), "tenant-a", "project-a", "request-create"
        )
        self.gateway.grant_project(
            session(),
            "tenant-a",
            "user-a",
            "project-a",
            "request-grant",
        )
        self.store.issue_session(
            "tenant-a",
            "user-session",
            "user-a",
            NOW + timedelta(hours=1),
        )
        loaded = self.store.load_session("tenant-a", "user-session")
        self.assertEqual(loaded.project_ids, frozenset({"project-a"}))
        audit = self.store.list_security_audit("request-grant")
        self.assertEqual(
            [entry["decision"] for entry in audit],
            ["attempted", "allowed"],
        )
        self.assertEqual(audit[-1]["action"], "grant_admin")

    def test_denial_is_audited_without_mutating_foreign_tenant(self):
        with self.assertRaises(HostedAdministrationDeniedError) as raised:
            self.gateway.create_project(
                session(), "tenant-b", "project-b", "request-denied"
            )
        self.assertEqual(raised.exception.reason, "tenant_mismatch")
        audit = self.store.list_security_audit("request-denied")
        self.assertEqual(audit[0]["tenant_id"], "tenant-a")
        self.assertEqual(audit[0]["decision"], "denied")
        self.assertEqual(audit[0]["reason"], "tenant_mismatch")
        projects = self.store.conn.execute(
            "SELECT * FROM hosted_projects WHERE tenant_id = 'tenant-b'"
        ).fetchall()
        self.assertEqual(projects, [])

    def test_service_role_requires_separate_security_admin(self):
        with self.assertRaises(HostedAdministrationDeniedError):
            self.gateway.assign_service_role(
                session(),
                "tenant-a",
                "user-a",
                "service_reader",
                "request-role-denied",
            )
        security_admin = session(roles=frozenset({"tenant_security_admin"}))
        self.gateway.assign_service_role(
            security_admin,
            "tenant-a",
            "user-a",
            "service_reader",
            "request-role-allowed",
        )
        roles = self.store.conn.execute(
            """
            SELECT role FROM hosted_role_assignments
            WHERE tenant_id = ? AND actor_id = ?
            """,
            ("tenant-a", "user-a"),
        ).fetchall()
        self.assertEqual([row["role"] for row in roles], ["service_reader"])
        self.assertTrue(
            self.gateway.revoke_role(
                security_admin,
                "tenant-a",
                "user-a",
                "service_reader",
                "request-role-revoke",
            )
        )

    def test_session_and_grant_lifecycle_are_administered(self):
        self.gateway.create_project(
            session(), "tenant-a", "project-a", "request-lifecycle-create"
        )
        self.gateway.grant_project(
            session(),
            "tenant-a",
            "user-a",
            "project-a",
            "request-lifecycle-grant",
        )
        self.gateway.assign_role(
            session(),
            "tenant-a",
            "user-a",
            "project_reader",
            "request-lifecycle-role",
        )
        self.gateway.issue_session(
            session(),
            "tenant-a",
            "user-session",
            "user-a",
            NOW + timedelta(hours=1),
            "request-lifecycle-session",
        )
        self.assertTrue(
            self.store.load_session("tenant-a", "user-session").active
        )
        self.assertTrue(
            self.gateway.revoke_session(
                session(),
                "tenant-a",
                "user-session",
                "request-lifecycle-session-revoke",
            )
        )
        self.assertTrue(
            self.gateway.revoke_project(
                session(),
                "tenant-a",
                "user-a",
                "project-a",
                "request-lifecycle-grant-revoke",
            )
        )

    def test_store_failure_is_audited_without_sensitive_payload(self):
        with self.assertRaises(Exception):
            self.gateway.grant_project(
                session(),
                "tenant-a",
                "user-a",
                "missing-project",
                "request-failed",
            )
        audit = self.store.list_security_audit("request-failed")
        self.assertEqual(
            [entry["decision"] for entry in audit],
            ["attempted", "failed"],
        )
        self.assertEqual(audit[-1]["reason"], "store_error")
        self.assertNotIn("content", audit[0])
        self.assertNotIn("credential", audit[0])

    def test_actor_deletion_removes_sessions_roles_and_grants(self):
        self.gateway.create_project(
            session(), "tenant-a", "project-a", "request-create-delete"
        )
        self.gateway.grant_project(
            session(),
            "tenant-a",
            "user-a",
            "project-a",
            "request-grant-delete",
        )
        self.store.assign_role("tenant-a", "user-a", "project_reader")
        self.store.issue_session(
            "tenant-a",
            "user-session",
            "user-a",
            NOW + timedelta(hours=1),
        )
        self.assertTrue(
            self.gateway.delete_actor(
                session(),
                "tenant-a",
                "user-a",
                "request-delete",
            )
        )
        self.assertIsNone(self.store.load_session("tenant-a", "user-session"))
