import http.client
import json
import socket
import threading
import time
import unittest

from context_memory.hosted_api import HostedAPIAdapter
from context_memory.hosted_authorization import HostedSession
from context_memory.hosted_http import HostedHTTPServer
from context_memory.hosted_repository import HostedRepositoryGateway
from context_memory.hosted_transport import (
    HostedCursorCodec,
    HostedTransportPolicy,
)


class FakeRepository:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False

    def search(self, tenant_id, project_id, query):
        if self.block:
            self.started.set()
            self.release.wait(timeout=2)
        return [{"content": f"{tenant_id}:{project_id}:{query}"}]

    def export_project(self, tenant_id, project_id):
        return {"events": []}

    def poll_events(self, tenant_id, project_id, cursor):
        return {"events": [], "next_cursor": cursor or 0}

    def backup_tenant(self, tenant_id):
        return b"{}"


class FakeAdministrationGateway:
    def create_project(
        self, session, tenant_id, project_id, request_id
    ) -> None:
        return None


class FakeIdempotencyStore:
    def claim(self, tenant_id, operation, key, request):
        raise AssertionError("mutation route not used")


class RecordingAdapter(HostedAPIAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancelled = threading.Event()

    def cancel(self, request_id):
        result = super().cancel(request_id)
        if result:
            self.cancelled.set()
        return result


def session():
    return HostedSession(
        actor_id="user-a",
        tenant_id="tenant-a",
        session_id="session-a",
        roles=frozenset({"project_reader"}),
        project_ids=frozenset({"project-a"}),
    )


class HostedHTTPServerTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakeRepository()
        policy = HostedTransportPolicy(
            max_body_bytes=128,
            request_timeout_seconds=1,
            allowed_origins=("https://app.example",),
            trusted_proxy_cidrs=("127.0.0.0/8",),
        )
        self.adapter = RecordingAdapter(
            HostedRepositoryGateway(self.repository),
            FakeAdministrationGateway(),
            policy,
            HostedCursorCodec(
                b"listener-cursor-key-at-least-thirty-two-bytes"
            ),
            FakeIdempotencyStore(),
        )

        def resolve(headers, tenant_id):
            if (
                headers.get("authorization") == "Bearer verified"
                and tenant_id == "tenant-a"
            ):
                return session()
            return None

        self.server = HostedHTTPServer(
            ("127.0.0.1", 0),
            self.adapter,
            resolve,
            read_timeout_seconds=0.1,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self):
        self.repository.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, method, path, body=b"", headers=None):
        connection = http.client.HTTPConnection(
            self.host, self.port, timeout=2
        )
        values = {
            "Authorization": "Bearer verified",
            "Content-Type": "application/json",
            "X-Forwarded-Proto": "https",
            "X-Request-ID": "network-request",
        }
        if headers:
            values.update(headers)
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), raw)
        connection.close()
        return result

    def test_real_socket_routes_through_proxy_tls_and_verified_session(self):
        body = json.dumps({"query": "decision"}).encode()
        status, headers, raw = self._request(
            "POST",
            "/v1/tenants/tenant-a/projects/project-a/search",
            body,
            {"Origin": "https://app.example"},
        )
        self.assertEqual(status, 200)
        value = json.loads(raw)
        self.assertEqual(
            value["result"][0]["content"],
            "tenant-a:project-a:decision",
        )
        self.assertEqual(headers["X-Request-ID"], "network-request")
        self.assertEqual(
            headers["Access-Control-Allow-Origin"],
            "https://app.example",
        )

        denied = self._request(
            "POST",
            "/v1/tenants/tenant-a/projects/project-a/search",
            body,
            {"Authorization": "Bearer invalid"},
        )
        self.assertEqual(denied[0], 403)
        self.assertEqual(
            json.loads(denied[2])["error"]["code"], "access_denied"
        )

    def test_oversized_and_malformed_bodies_are_stable(self):
        connection = http.client.HTTPConnection(
            self.host, self.port, timeout=2
        )
        connection.putrequest(
            "POST", "/v1/tenants/tenant-a/projects/project-a/search"
        )
        connection.putheader("Content-Length", "129")
        connection.putheader("X-Forwarded-Proto", "https")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        self.assertEqual(
            json.loads(response.read())["error"]["code"], "body_too_large"
        )
        connection.close()

        malformed = self._request(
            "POST",
            "/v1/tenants/tenant-a/projects/project-a/search",
            b"{bad-json",
        )
        self.assertEqual(malformed[0], 400)
        self.assertEqual(
            json.loads(malformed[2])["error"]["code"], "invalid_request"
        )

    def test_cors_preflight_is_exact_and_bounded(self):
        allowed = self._request(
            "OPTIONS",
            "/v1/tenants/tenant-a/projects/project-a/search",
            headers={
                "Origin": "https://app.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "content-type, x-request-id"
                ),
            },
        )
        self.assertEqual(allowed[0], 204)
        self.assertEqual(allowed[1]["Access-Control-Allow-Methods"], "POST")
        denied = self._request(
            "OPTIONS",
            "/v1/tenants/tenant-a/projects/project-a/search",
            headers={
                "Origin": "https://app.example.evil",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(denied[0], 403)

    def test_slow_body_read_times_out_with_stable_response(self):
        client = socket.create_connection((self.host, self.port), timeout=2)
        request = (
            b"POST /v1/tenants/tenant-a/projects/project-a/search HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 20\r\n"
            b"X-Forwarded-Proto: https\r\n\r\n"
            b"{"
        )
        client.sendall(request)
        time.sleep(0.2)
        response = client.recv(4096)
        client.close()
        self.assertIn(b" 408 ", response)

    def test_disconnect_cancels_active_adapter_and_shutdown_is_clean(self):
        self.repository.block = True
        body = b'{"query":"wait"}'
        client = socket.create_connection((self.host, self.port), timeout=2)
        request = (
            b"POST /v1/tenants/tenant-a/projects/project-a/search HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer verified\r\n"
            b"X-Forwarded-Proto: https\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        client.sendall(request)
        self.assertTrue(self.repository.started.wait(timeout=1))
        client.shutdown(socket.SHUT_RDWR)
        client.close()
        self.assertTrue(self.adapter.cancelled.wait(timeout=1))
        self.repository.release.set()

        self.server.shutdown()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
