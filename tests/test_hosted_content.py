import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from context_memory.hosted_authorization import HostedSession
from context_memory.hosted_content import HostedContentStore
from context_memory.hosted_repository import (
    HostedRepositoryDeniedError,
    HostedRepositoryGateway,
)


class HostedContentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = HostedContentStore(
            Path(self.tempdir.name) / "hosted-content.db"
        )
        for tenant_id in ("tenant-a", "tenant-b"):
            self.store.provision_tenant(tenant_id)
            self.store.provision_project(tenant_id, "shared-project-id")

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_same_project_id_is_isolated_by_tenant_for_every_read(self):
        first = self.store.record_event(
            "tenant-a", "shared-project-id", "decision", "alpha secret"
        )
        second = self.store.record_event(
            "tenant-b", "shared-project-id", "decision", "beta secret"
        )
        self.assertEqual(first["event_seq"], 1)
        self.assertEqual(second["event_seq"], 1)

        alpha_search = self.store.search(
            "tenant-a", "shared-project-id", "secret"
        )
        self.assertEqual(
            [row["content"] for row in alpha_search], ["alpha secret"]
        )
        alpha_poll = self.store.poll_events(
            "tenant-a", "shared-project-id", None
        )
        self.assertEqual(
            [row["content"] for row in alpha_poll["events"]],
            ["alpha secret"],
        )
        alpha_export = self.store.export_project(
            "tenant-a", "shared-project-id"
        )
        self.assertEqual(
            [row["content"] for row in alpha_export["events"]],
            ["alpha secret"],
        )
        alpha_backup = json.loads(self.store.backup_tenant("tenant-a"))
        self.assertNotIn("beta secret", json.dumps(alpha_backup))

    def test_project_foreign_key_requires_matching_tenant_root(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_event(
                "tenant-a", "missing-project", "fact", "not written"
            )

    def test_cursor_is_tenant_and_project_local(self):
        self.store.record_event(
            "tenant-a", "shared-project-id", "fact", "first"
        )
        self.store.record_event(
            "tenant-a", "shared-project-id", "fact", "second"
        )
        self.store.record_event(
            "tenant-b", "shared-project-id", "fact", "foreign"
        )
        result = self.store.poll_events(
            "tenant-a", "shared-project-id", cursor=1
        )
        self.assertEqual(
            [event["content"] for event in result["events"]], ["second"]
        )
        self.assertEqual(result["next_cursor"], 2)

    def test_gateway_denial_cannot_reach_foreign_tenant_content(self):
        self.store.record_event(
            "tenant-b", "shared-project-id", "fact", "foreign secret"
        )
        gateway = HostedRepositoryGateway(self.store)
        caller = HostedSession(
            actor_id="user-a",
            tenant_id="tenant-a",
            session_id="session-a",
            roles=frozenset({"project_reader"}),
            project_ids=frozenset({"shared-project-id"}),
        )
        with self.assertRaises(HostedRepositoryDeniedError):
            gateway.search(
                caller, "tenant-b", "shared-project-id", "secret"
            )
        self.assertEqual(
            gateway.search(
                caller, "tenant-a", "shared-project-id", "secret"
            ),
            [],
        )
