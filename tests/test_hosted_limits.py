import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from context_memory.hosted_administration import (
    HostedAdministrationGateway,
    HostedAdministrationRateLimitError,
)
from context_memory.hosted_authorization import HostedSession
from context_memory.hosted_identity import HostedIdentityStore
from context_memory.hosted_limits import (
    HostedRateLimiter,
    HostedRateLimitPolicy,
)
from context_memory.hosted_repository import (
    HostedRepositoryGateway,
    HostedRepositoryRateLimitError,
)


class RecordingRepository:
    def __init__(self):
        self.calls = []

    def search(self, tenant_id, project_id, query):
        self.calls.append((tenant_id, project_id, query))
        return []

    def export_project(self, tenant_id, project_id):
        raise AssertionError("not used")

    def poll_events(self, tenant_id, project_id, cursor):
        raise AssertionError("not used")

    def backup_tenant(self, tenant_id):
        raise AssertionError("not used")


def session(actor_id="actor-a", tenant_id="tenant-a"):
    return HostedSession(
        actor_id=actor_id,
        tenant_id=tenant_id,
        session_id=f"session-{actor_id}",
        roles=frozenset({"project_reader"}),
        project_ids=frozenset({"project-a"}),
    )


class HostedRateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 8, 31, tzinfo=timezone.utc)
        self.path = Path(self.tempdir.name) / "limits.db"
        self.limiter = HostedRateLimiter(
            self.path,
            HostedRateLimitPolicy(requests_per_window=2, window_seconds=60),
            lambda: self.now,
        )

    def tearDown(self):
        self.limiter.close()
        self.tempdir.cleanup()

    def test_limit_persists_and_resets_at_window_boundary(self):
        self.assertTrue(
            self.limiter.consume("tenant-a", "actor-a", "search").allowed
        )
        self.assertTrue(
            self.limiter.consume("tenant-a", "actor-a", "search").allowed
        )
        denied = self.limiter.consume("tenant-a", "actor-a", "search")
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.retry_after_seconds, 60)

        self.limiter.close()
        self.limiter = HostedRateLimiter(
            self.path,
            HostedRateLimitPolicy(requests_per_window=2, window_seconds=60),
            lambda: self.now,
        )
        self.assertFalse(
            self.limiter.consume("tenant-a", "actor-a", "search").allowed
        )
        self.now += timedelta(seconds=60)
        self.assertTrue(
            self.limiter.consume("tenant-a", "actor-a", "search").allowed
        )

    def test_tenant_actor_and_action_have_independent_buckets(self):
        for _ in range(2):
            self.limiter.consume("tenant-a", "actor-a", "search")
        self.assertTrue(
            self.limiter.consume("tenant-b", "actor-a", "search").allowed
        )
        self.assertTrue(
            self.limiter.consume("tenant-a", "actor-b", "search").allowed
        )
        self.assertTrue(
            self.limiter.consume("tenant-a", "actor-a", "export").allowed
        )

    def test_gateway_never_calls_storage_after_limit(self):
        repository = RecordingRepository()
        gateway = HostedRepositoryGateway(repository, self.limiter)
        caller = session()
        gateway.search(caller, "tenant-a", "project-a", "first")
        gateway.search(caller, "tenant-a", "project-a", "second")
        with self.assertRaises(HostedRepositoryRateLimitError) as raised:
            gateway.search(caller, "tenant-a", "project-a", "third")
        self.assertEqual(raised.exception.retry_after_seconds, 60)
        self.assertEqual(len(repository.calls), 2)

    def test_denied_repository_probes_are_also_bounded(self):
        repository = RecordingRepository()
        gateway = HostedRepositoryGateway(repository, self.limiter)
        caller = session()
        for _ in range(2):
            with self.assertRaises(PermissionError):
                gateway.search(caller, "tenant-b", "project-a", "probe")
        with self.assertRaises(HostedRepositoryRateLimitError):
            gateway.search(caller, "tenant-b", "project-a", "probe")
        self.assertEqual(repository.calls, [])

    def test_administration_limits_are_audited(self):
        identity = HostedIdentityStore(
            Path(self.tempdir.name) / "identity.db", lambda: self.now
        )
        try:
            identity.provision_tenant("tenant-a")
            gateway = HostedAdministrationGateway(identity, self.limiter)
            admin = session()
            admin = HostedSession(
                actor_id=admin.actor_id,
                tenant_id=admin.tenant_id,
                session_id=admin.session_id,
                roles=frozenset({"tenant_admin"}),
                project_ids=frozenset(),
            )
            gateway.create_project(
                admin, "tenant-a", "project-a", "admin-first"
            )
            gateway.create_project(
                admin, "tenant-a", "project-b", "admin-second"
            )
            with self.assertRaises(HostedAdministrationRateLimitError):
                gateway.create_project(
                    admin, "tenant-a", "project-c", "admin-limited"
                )
            audit = identity.list_security_audit("admin-limited")
            self.assertEqual(audit[0]["reason"], "rate_limited")
        finally:
            identity.close()
