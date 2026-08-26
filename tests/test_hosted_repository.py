import unittest

from context_memory.hosted_authorization import HostedSession
from context_memory.hosted_repository import (
    HostedRepositoryDeniedError,
    HostedRepositoryGateway,
)


class RecordingRepository:
    def __init__(self):
        self.calls = []

    def search(self, tenant_id, project_id, query):
        self.calls.append(("search", tenant_id, project_id, query))
        return ["search-result"]

    def export_project(self, tenant_id, project_id):
        self.calls.append(("export", tenant_id, project_id))
        return b"export"

    def poll_events(self, tenant_id, project_id, cursor):
        self.calls.append(("poll", tenant_id, project_id, cursor))
        return {"events": []}

    def backup_tenant(self, tenant_id):
        self.calls.append(("backup", tenant_id))
        return b"backup"


def session(**overrides):
    values = {
        "actor_id": "user-a",
        "tenant_id": "tenant-a",
        "session_id": "session-a",
        "roles": frozenset(
            {"project_reader", "project_exporter", "tenant_backup_operator"}
        ),
        "project_ids": frozenset({"project-a"}),
    }
    values.update(overrides)
    return HostedSession(**values)


class HostedRepositoryGatewayTests(unittest.TestCase):
    def setUp(self):
        self.repository = RecordingRepository()
        self.gateway = HostedRepositoryGateway(self.repository)

    def test_forwards_exact_tenant_and_project_after_authorization(self):
        caller = session()
        self.assertEqual(
            self.gateway.search(
                caller, "tenant-a", "project-a", "current decision"
            ),
            ["search-result"],
        )
        self.assertEqual(
            self.gateway.export_project(caller, "tenant-a", "project-a"),
            b"export",
        )
        self.assertEqual(
            self.gateway.poll_events(
                caller, "tenant-a", "project-a", cursor=12
            ),
            {"events": []},
        )
        self.assertEqual(
            self.repository.calls,
            [
                ("search", "tenant-a", "project-a", "current decision"),
                ("export", "tenant-a", "project-a"),
                ("poll", "tenant-a", "project-a", 12),
            ],
        )

    def test_denial_never_calls_repository(self):
        attempts = (
            lambda: self.gateway.search(
                session(), "tenant-b", "project-a", "query"
            ),
            lambda: self.gateway.export_project(
                session(project_ids=frozenset()), "tenant-a", "project-a"
            ),
            lambda: self.gateway.poll_events(
                session(active=False), "tenant-a", "project-a"
            ),
            lambda: self.gateway.backup_tenant(session(), "tenant-b"),
        )
        expected = (
            "tenant_mismatch",
            "project_not_granted",
            "session_inactive",
            "tenant_mismatch",
        )
        for attempt, reason in zip(attempts, expected, strict=True):
            with self.subTest(reason=reason):
                with self.assertRaises(HostedRepositoryDeniedError) as raised:
                    attempt()
                self.assertEqual(raised.exception.reason, reason)
                self.assertEqual(
                    str(raised.exception),
                    "hosted repository access denied",
                )
        self.assertEqual(self.repository.calls, [])

    def test_backup_forwards_tenant_only(self):
        result = self.gateway.backup_tenant(session(), "tenant-a")
        self.assertEqual(result, b"backup")
        self.assertEqual(self.repository.calls, [("backup", "tenant-a")])

    def test_role_denial_does_not_probe_repository(self):
        caller = session(roles=frozenset({"project_reader"}))
        with self.assertRaises(HostedRepositoryDeniedError) as raised:
            self.gateway.export_project(caller, "tenant-a", "project-a")
        self.assertEqual(raised.exception.reason, "permission_denied")
        self.assertEqual(self.repository.calls, [])
