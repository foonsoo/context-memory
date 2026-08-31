import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from context_memory.hosted_administration import HostedAdministrationGateway
from context_memory.hosted_api import (
    HostedAPIAdapter,
    HostedAPIRequest,
)
from context_memory.hosted_authorization import HostedSession
from context_memory.hosted_content import HostedContentStore
from context_memory.hosted_identity import HostedIdentityStore
from context_memory.hosted_repository import HostedRepositoryGateway
from context_memory.hosted_transport import (
    HostedCursorCodec,
    HostedIdempotencyStore,
    HostedRequestContext,
    HostedTransportPolicy,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def user_session(**overrides):
    values = {
        "actor_id": "user-a",
        "tenant_id": "tenant-a",
        "session_id": "session-a",
        "roles": frozenset(
            {
                "project_reader",
                "project_exporter",
                "tenant_backup_operator",
            }
        ),
        "project_ids": frozenset({"project-a"}),
    }
    values.update(overrides)
    return HostedSession(**values)


class HostedAPIAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.content = HostedContentStore(root / "content.db")
        self.content.provision_tenant("tenant-a")
        self.content.provision_tenant("tenant-b")
        self.content.provision_project("tenant-a", "project-a")
        self.content.provision_project("tenant-b", "project-a")
        self.content.record_event(
            "tenant-a", "project-a", "fact", "alpha decision"
        )
        self.content.record_event(
            "tenant-b", "project-a", "fact", "foreign decision"
        )
        self.identity = HostedIdentityStore(root / "identity.db", lambda: NOW)
        self.identity.provision_tenant("tenant-a")
        self.identity.provision_tenant("tenant-b")
        self.identity.provision_actor("tenant-a", "admin-a")
        self.idempotency = HostedIdempotencyStore(
            root / "idempotency.db", clock=lambda: NOW
        )
        self.policy = HostedTransportPolicy(
            max_body_bytes=256,
            request_timeout_seconds=5,
            allowed_origins=("https://app.example",),
            trusted_proxy_cidrs=("10.0.0.0/8",),
        )
        self.cursors = HostedCursorCodec(
            b"hosted-api-cursor-key-at-least-thirty-two-bytes",
            clock=lambda: NOW,
        )
        self.adapter = self._adapter(self.content)

    def tearDown(self):
        self.idempotency.close()
        self.identity.close()
        self.content.close()
        self.tempdir.cleanup()

    def _adapter(self, repository, context_factory=HostedRequestContext):
        return HostedAPIAdapter(
            HostedRepositoryGateway(repository),
            HostedAdministrationGateway(self.identity),
            self.policy,
            self.cursors,
            self.idempotency,
            context_factory,
        )

    def test_disk_full_is_stable_and_does_not_disclose_database_path(self):
        class FullRepository:
            def search(self, tenant_id, project_id, query):
                raise sqlite3.OperationalError(
                    "database or disk is full: /private/tenant-a.db"
                )

        adapter = self._adapter(FullRepository())
        response = adapter.handle(
            self._request("search", {"query": "decision"})
        )
        self.assertEqual(response.status, 507)
        self.assertEqual(response.body["error"]["code"], "storage_exhausted")
        self.assertNotIn("/private", json.dumps(response.body))

    @staticmethod
    def _request(
        operation,
        body,
        *,
        request_id="request-1",
        tenant_id="tenant-a",
        project_id="project-a",
        session=None,
        headers=None,
        secure=True,
        peer_ip="203.0.113.10",
    ):
        encoded = (
            body
            if isinstance(body, bytes)
            else json.dumps(body, separators=(",", ":")).encode()
        )
        return HostedAPIRequest(
            request_id=request_id,
            operation=operation,
            tenant_id=tenant_id,
            project_id=project_id,
            body=encoded,
            content_length=len(encoded),
            peer_ip=peer_ip,
            connection_secure=secure,
            session=session or user_session(),
            headers=headers,
        )

    def test_search_applies_tls_proxy_cors_version_and_authorization(self):
        request = self._request(
            "search",
            {"query": "decision"},
            headers={
                "Origin": "https://app.example",
                "X-Context-Memory-API-Version": "v1",
                "X-Forwarded-For": "198.51.100.4",
                "X-Forwarded-Proto": "https",
            },
            secure=False,
            peer_ip="10.0.0.4",
        )
        response = self.adapter.handle(request)
        self.assertEqual(response.status, 200)
        results = response.body["result"]
        self.assertEqual(
            [item["content"] for item in results], ["alpha decision"]
        )
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"],
            "https://app.example",
        )
        self.assertEqual(response.headers["X-Request-ID"], "request-1")

    def test_foreign_tenant_and_malformed_requests_have_stable_errors(self):
        denied = self._request(
            "search",
            {"query": "decision"},
            tenant_id="tenant-b",
        )
        response = self.adapter.handle(denied)
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "access_denied")
        self.assertNotIn("tenant", response.body["error"]["message"])

        malformed = self._request("search", b"{not-json")
        response = self.adapter.handle(malformed)
        self.assertEqual(response.status, 400)
        self.assertEqual(response.body["error"]["code"], "invalid_request")

        oversized = self._request("search", b"x" * 257)
        response = self.adapter.handle(oversized)
        self.assertEqual(response.status, 413)
        self.assertEqual(response.body["error"]["code"], "body_too_large")

    def test_event_poll_cursor_is_opaque_and_tenant_bound(self):
        first = self.adapter.handle(self._request("event_poll", {}))
        self.assertEqual(first.status, 200)
        cursor = first.body["result"]["next_cursor"]
        self.assertIsInstance(cursor, str)
        second = self.adapter.handle(
            self._request(
                "event_poll",
                {"cursor": cursor},
                request_id="request-2",
            )
        )
        self.assertEqual(second.status, 200)
        self.assertEqual(second.body["result"]["events"], [])
        foreign = self.adapter.handle(
            self._request(
                "event_poll",
                {"cursor": cursor},
                request_id="request-3",
                tenant_id="tenant-b",
                session=user_session(tenant_id="tenant-b"),
            )
        )
        self.assertEqual(foreign.status, 400)
        self.assertEqual(foreign.body["error"]["code"], "invalid_cursor")

    def test_project_create_is_idempotent_and_failed_claim_is_retriable(self):
        admin = user_session(
            actor_id="admin-a",
            roles=frozenset({"tenant_admin"}),
            project_ids=frozenset(),
        )
        headers = {"Idempotency-Key": "create-project-b"}
        request = self._request(
            "project_create",
            {"project_id": "project-b"},
            project_id=None,
            session=admin,
            headers=headers,
        )
        first = self.adapter.handle(request)
        replay = self.adapter.handle(
            self._request(
                "project_create",
                {"project_id": "project-b"},
                request_id="request-2",
                project_id=None,
                session=admin,
                headers=headers,
            )
        )
        self.assertEqual(first.status, 200)
        self.assertEqual(replay.body["result"], first.body["result"])

        denied = self._request(
            "project_create",
            {"project_id": "project-c"},
            request_id="request-denied",
            project_id=None,
            session=user_session(),
            headers={"Idempotency-Key": "retry-project-c"},
        )
        self.assertEqual(self.adapter.handle(denied).status, 403)
        retry = self._request(
            "project_create",
            {"project_id": "project-c"},
            request_id="request-retry",
            project_id=None,
            session=admin,
            headers={"Idempotency-Key": "retry-project-c"},
        )
        self.assertEqual(self.adapter.handle(retry).status, 200)

    def test_deadline_and_disconnect_cancel_after_bounded_repository_call(
        self,
    ):
        now = NOW

        class DeadlineRepository:
            def search(self, tenant_id, project_id, query):
                nonlocal now
                now += timedelta(seconds=5)
                return []

        deadline_adapter = self._adapter(
            DeadlineRepository(),
            lambda request_id, timeout: HostedRequestContext(
                request_id, timeout, lambda: now
            ),
        )
        deadline = deadline_adapter.handle(
            self._request("search", {"query": "slow"})
        )
        self.assertEqual(deadline.status, 504)
        self.assertEqual(deadline.body["error"]["code"], "deadline_exceeded")

        started = threading.Event()
        release = threading.Event()

        class BlockingRepository:
            def search(self, tenant_id, project_id, query):
                started.set()
                release.wait(timeout=2)
                return []

        cancelled_adapter = self._adapter(BlockingRepository())
        responses = []
        thread = threading.Thread(
            target=lambda: responses.append(
                cancelled_adapter.handle(
                    self._request("search", {"query": "cancel"})
                )
            )
        )
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        self.assertTrue(cancelled_adapter.cancel("request-1"))
        release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(responses[0].status, 499)
        self.assertEqual(
            responses[0].body["error"]["code"], "request_cancelled"
        )
