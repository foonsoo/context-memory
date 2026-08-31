import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from context_memory.hosted_authorization import HostedSession
from context_memory.hosted_content import (
    HostedContentStore,
    HostedQuotaExceededError,
    HostedQuotaPolicy,
)
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
            gateway.search(caller, "tenant-b", "shared-project-id", "secret")
        self.assertEqual(
            gateway.search(caller, "tenant-a", "shared-project-id", "secret"),
            [],
        )

    def test_gateway_to_store_denies_foreign_tenant_for_every_action(self):
        self.store.record_event(
            "tenant-a", "shared-project-id", "fact", "alpha"
        )
        self.store.record_event(
            "tenant-b", "shared-project-id", "fact", "foreign"
        )
        gateway = HostedRepositoryGateway(self.store)
        caller = HostedSession(
            actor_id="user-a",
            tenant_id="tenant-a",
            session_id="session-a",
            roles=frozenset(
                {
                    "project_reader",
                    "project_exporter",
                    "tenant_backup_operator",
                }
            ),
            project_ids=frozenset({"shared-project-id"}),
        )
        attempts = (
            lambda: gateway.search(
                caller, "tenant-b", "shared-project-id", "foreign"
            ),
            lambda: gateway.export_project(
                caller, "tenant-b", "shared-project-id"
            ),
            lambda: gateway.poll_events(
                caller, "tenant-b", "shared-project-id"
            ),
            lambda: gateway.backup_tenant(caller, "tenant-b"),
        )
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                with self.assertRaises(HostedRepositoryDeniedError) as raised:
                    attempt()
                self.assertEqual(raised.exception.reason, "tenant_mismatch")
        authorized = gateway.export_project(
            caller, "tenant-a", "shared-project-id"
        )
        self.assertEqual(
            [event["content"] for event in authorized["events"]], ["alpha"]
        )
        tenant_backup = gateway.backup_tenant(caller, "tenant-a").decode()
        self.assertNotIn("foreign", tenant_backup)


class HostedContentQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = HostedContentStore(
            Path(self.tempdir.name) / "quota.db",
            HostedQuotaPolicy(
                max_projects_per_tenant=1,
                max_events_per_project=2,
                max_event_bytes=6,
                max_tenant_bytes=10,
            ),
        )
        self.store.provision_tenant("tenant-a")
        self.store.provision_tenant("tenant-b")
        self.store.provision_project("tenant-a", "project-a")
        self.store.provision_project("tenant-b", "project-b")

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_project_and_event_quotas_are_tenant_local(self):
        with self.assertRaises(HostedQuotaExceededError) as project_error:
            self.store.provision_project("tenant-a", "project-extra")
        self.assertEqual(
            project_error.exception.reason, "tenant_project_limit"
        )
        self.store.record_event("tenant-a", "project-a", "fact", "12345")
        self.store.record_event("tenant-a", "project-a", "fact", "67890")
        with self.assertRaises(HostedQuotaExceededError) as event_error:
            self.store.record_event("tenant-a", "project-a", "fact", "x")
        self.assertEqual(event_error.exception.reason, "project_event_limit")
        event = self.store.record_event(
            "tenant-b", "project-b", "fact", "other"
        )
        self.assertEqual(event["event_seq"], 1)

    def test_event_and_tenant_byte_limits_use_utf8_bytes(self):
        with self.assertRaises(HostedQuotaExceededError) as event_error:
            self.store.record_event("tenant-a", "project-a", "fact", "한글a")
        self.assertEqual(event_error.exception.reason, "event_byte_limit")
        self.store.record_event("tenant-a", "project-a", "fact", "123456")
        with self.assertRaises(HostedQuotaExceededError) as tenant_error:
            self.store.record_event("tenant-a", "project-a", "fact", "12345")
        self.assertEqual(tenant_error.exception.reason, "tenant_byte_limit")
