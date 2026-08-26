import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from context_memory.hosted_authorization import (
    HostedAction,
    HostedResource,
    authorize_hosted_action,
)
from context_memory.hosted_identity import HostedIdentityStore


class HostedIdentityStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 8, 26, tzinfo=timezone.utc)
        self.store = HostedIdentityStore(
            Path(self.tempdir.name) / "hosted.db", lambda: self.now
        )
        self.store.provision_tenant("tenant-a")
        self.store.provision_actor("tenant-a", "user-a")
        self.store.provision_project("tenant-a", "project-a")
        self.store.assign_role("tenant-a", "user-a", "project_reader")
        self.store.grant_project("tenant-a", "user-a", "project-a")

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def issue(self, session_id="session-a", lifetime=timedelta(hours=1)):
        self.store.issue_session(
            "tenant-a", session_id, "user-a", self.now + lifetime
        )

    def test_loads_current_roles_and_grants_for_each_request(self):
        self.issue()
        loaded = self.store.load_session("tenant-a", "session-a")
        decision = authorize_hosted_action(
            loaded,
            HostedResource("tenant-a", "project-a"),
            HostedAction.SEARCH,
        )
        self.assertTrue(decision.allowed)

        self.store.conn.execute(
            """
            DELETE FROM hosted_project_grants
            WHERE tenant_id = 'tenant-a' AND actor_id = 'user-a'
            """
        )
        reloaded = self.store.load_session("tenant-a", "session-a")
        denied = authorize_hosted_action(
            reloaded,
            HostedResource("tenant-a", "project-a"),
            HostedAction.SEARCH,
        )
        self.assertEqual(denied.reason, "project_not_granted")

    def test_revocation_is_enforced_on_the_next_load(self):
        self.issue()
        loaded = self.store.load_session("tenant-a", "session-a")
        self.assertTrue(loaded.active)
        self.assertTrue(self.store.revoke_session("tenant-a", "session-a"))
        self.assertFalse(
            self.store.load_session("tenant-a", "session-a").active
        )
        self.assertFalse(self.store.revoke_session("tenant-a", "session-a"))

    def test_expired_session_is_inactive(self):
        self.issue(lifetime=timedelta(seconds=-1))
        loaded = self.store.load_session("tenant-a", "session-a")
        self.assertFalse(loaded.active)

    def test_unknown_session_does_not_reveal_another_tenant(self):
        self.issue()
        self.store.provision_tenant("tenant-b")
        self.assertIsNone(self.store.load_session("tenant-b", "session-a"))

    def test_foreign_keys_prevent_cross_tenant_grants(self):
        self.store.provision_tenant("tenant-b")
        self.store.provision_project("tenant-b", "project-b")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.grant_project("tenant-a", "user-a", "project-b")
