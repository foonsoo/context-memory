import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from context_memory.hosted_transport import (
    HostedAPIError,
    HostedAPIErrorCode,
    HostedCursorCodec,
    HostedIdempotencyStore,
    HostedRequestContext,
    HostedTransportPolicy,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


class HostedTransportPolicyTests(unittest.TestCase):
    def test_trusted_proxy_tls_cors_and_version_are_exact(self):
        policy = HostedTransportPolicy(
            allowed_origins=("https://app.example",),
            trusted_proxy_cidrs=("10.0.0.0/8",),
            supported_api_versions=("v1", "v2"),
        )
        resolved = policy.resolve_request(
            peer_ip="10.0.0.4",
            connection_secure=False,
            forwarded_for="203.0.113.8",
            forwarded_proto="https",
            api_version="v2",
            origin="https://app.example",
        )
        self.assertEqual(resolved.client_ip, "203.0.113.8")
        self.assertEqual(resolved.scheme, "https")
        self.assertEqual(resolved.api_version, "v2")

        with self.assertRaises(HostedAPIError) as origin_error:
            policy.resolve_request(
                peer_ip="10.0.0.4",
                connection_secure=False,
                forwarded_proto="https",
                api_version="v1",
                origin="https://app.example.evil",
            )
        self.assertEqual(
            origin_error.exception.code,
            HostedAPIErrorCode.CORS_ORIGIN_DENIED,
        )
        with self.assertRaises(HostedAPIError) as version_error:
            policy.resolve_request(
                peer_ip="10.0.0.4",
                connection_secure=False,
                forwarded_proto="https",
                api_version="v3",
                origin=None,
            )
        self.assertEqual(
            version_error.exception.code,
            HostedAPIErrorCode.UNSUPPORTED_API_VERSION,
        )

    def test_untrusted_forwarded_headers_cannot_assert_https(self):
        policy = HostedTransportPolicy(trusted_proxy_cidrs=("10.0.0.0/8",))
        with self.assertRaises(HostedAPIError) as raised:
            policy.resolve_request(
                peer_ip="198.51.100.9",
                connection_secure=False,
                forwarded_for="203.0.113.8",
                forwarded_proto="https",
                api_version="v1",
                origin=None,
            )
        self.assertEqual(
            raised.exception.code, HostedAPIErrorCode.INSECURE_TRANSPORT
        )

    def test_body_limit_and_error_envelope_are_stable(self):
        policy = HostedTransportPolicy(max_body_bytes=10)
        policy.validate_body_length(10)
        with self.assertRaises(HostedAPIError) as raised:
            policy.validate_body_length(11)
        error = raised.exception
        self.assertEqual(error.status, 413)
        self.assertEqual(
            error.envelope("request-1"),
            {
                "error": {
                    "code": "body_too_large",
                    "message": "request body exceeds the configured limit",
                    "request_id": "request-1",
                }
            },
        )


class HostedRequestContextTests(unittest.TestCase):
    def test_deadline_and_cancellation_are_cooperative(self):
        now = NOW
        context = HostedRequestContext("request-1", 5, lambda: now)
        context.check()
        now += timedelta(seconds=5)
        with self.assertRaises(HostedAPIError) as deadline:
            context.check()
        self.assertEqual(
            deadline.exception.code, HostedAPIErrorCode.DEADLINE_EXCEEDED
        )

        active = HostedRequestContext("request-2", 5, lambda: NOW)
        active.cancel()
        with self.assertRaises(HostedAPIError) as cancelled:
            active.check()
        self.assertEqual(
            cancelled.exception.code, HostedAPIErrorCode.REQUEST_CANCELLED
        )


class HostedCursorCodecTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.codec = HostedCursorCodec(
            b"cursor-signing-key-that-is-at-least-32-bytes",
            ttl_seconds=60,
            clock=lambda: self.now,
        )

    def test_cursor_is_tenant_route_bound_tamper_proof_and_expiring(self):
        cursor = self.codec.encode("tenant-a", "events", 25)
        self.assertEqual(self.codec.decode(cursor, "tenant-a", "events"), 25)
        for tenant_id, route, value in (
            ("tenant-b", "events", cursor),
            ("tenant-a", "search", cursor),
            ("tenant-a", "events", cursor[:-1] + "A"),
        ):
            with self.subTest(tenant_id=tenant_id, route=route):
                with self.assertRaises(HostedAPIError) as raised:
                    self.codec.decode(value, tenant_id, route)
                self.assertEqual(
                    raised.exception.code,
                    HostedAPIErrorCode.INVALID_CURSOR,
                )
        self.now += timedelta(seconds=60)
        with self.assertRaises(HostedAPIError):
            self.codec.decode(cursor, "tenant-a", "events")


class HostedIdempotencyStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.now = NOW
        self.path = Path(self.tempdir.name) / "idempotency.db"
        self.store = HostedIdempotencyStore(
            self.path, retention_seconds=60, clock=lambda: self.now
        )

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_claim_replay_conflict_and_restart_retention(self):
        request = {"project_id": "project-a", "value": 1}
        claim = self.store.claim("tenant-a", "write", "key-1", request)
        self.assertEqual(claim.state, "new")
        with self.assertRaises(HostedAPIError) as in_progress:
            self.store.claim("tenant-a", "write", "key-1", request)
        self.assertEqual(
            in_progress.exception.code,
            HostedAPIErrorCode.IDEMPOTENCY_IN_PROGRESS,
        )
        response = {"created": True, "id": "result-1"}
        self.store.complete("tenant-a", "write", "key-1", request, response)
        self.store.close()
        self.store = HostedIdempotencyStore(
            self.path, retention_seconds=60, clock=lambda: self.now
        )
        replay = self.store.claim("tenant-a", "write", "key-1", request)
        self.assertEqual(replay.state, "replay")
        self.assertEqual(replay.response, response)
        with self.assertRaises(HostedAPIError) as conflict:
            self.store.claim("tenant-a", "write", "key-1", {"value": 2})
        self.assertEqual(
            conflict.exception.code,
            HostedAPIErrorCode.IDEMPOTENCY_CONFLICT,
        )

    def test_retention_expiry_allows_a_new_claim(self):
        request = {"value": 1}
        self.store.claim("tenant-a", "write", "key-1", request)
        self.store.complete(
            "tenant-a", "write", "key-1", request, {"ok": True}
        )
        self.now += timedelta(seconds=60)
        claim = self.store.claim("tenant-a", "write", "key-1", {"value": 2})
        self.assertEqual(claim.state, "new")

    def test_idempotency_scope_is_tenant_and_operation_local(self):
        request = {"value": 1}
        self.store.claim("tenant-a", "write", "same-key", request)
        self.assertEqual(
            self.store.claim("tenant-b", "write", "same-key", request).state,
            "new",
        )
        self.assertEqual(
            self.store.claim("tenant-a", "other", "same-key", request).state,
            "new",
        )
