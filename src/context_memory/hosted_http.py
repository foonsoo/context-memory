from __future__ import annotations

import json
import queue
import re
import select
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping
from urllib.parse import unquote, urlsplit

from .hosted_api import HostedAPIAdapter, HostedAPIRequest, HostedAPIResponse
from .hosted_authorization import HostedSession
from .hosted_operations import HostedOperationsMonitor
from .hosted_transport import HostedAPIError, HostedAPIErrorCode

SessionResolver = Callable[[Mapping[str, str], str], HostedSession | None]


class HostedHTTPServer(ThreadingHTTPServer):
    """Dependency-injected HTTP edge for the hosted API adapter."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        adapter: HostedAPIAdapter,
        session_resolver: SessionResolver,
        *,
        read_timeout_seconds: float = 5.0,
        connection_secure: bool = False,
        operations: HostedOperationsMonitor | None = None,
    ) -> None:
        if read_timeout_seconds <= 0:
            raise ValueError("read timeout must be positive")
        self.adapter = adapter
        self.session_resolver = session_resolver
        self.read_timeout_seconds = read_timeout_seconds
        self.connection_secure = connection_secure
        self.operations = operations or HostedOperationsMonitor()
        super().__init__(server_address, HostedHTTPRequestHandler)

    def handle_error(
        self, request: object, client_address: tuple[str, int]
    ) -> None:
        # BaseServer prints raw tracebacks to stderr. Hosted listeners
        # record a bounded class without paths, peers, or payloads.
        self.operations.record_listener_error()


class HostedHTTPRequestHandler(BaseHTTPRequestHandler):
    server: HostedHTTPServer
    protocol_version = "HTTP/1.1"

    ROUTES = (
        (
            "POST",
            re.compile(r"^/v1/tenants/([^/]+)/projects/([^/]+)/search$"),
            "search",
        ),
        (
            "POST",
            re.compile(r"^/v1/tenants/([^/]+)/projects/([^/]+)/events:poll$"),
            "event_poll",
        ),
        (
            "POST",
            re.compile(r"^/v1/tenants/([^/]+)/projects/([^/]+)/export$"),
            "export",
        ),
        (
            "POST",
            re.compile(r"^/v1/tenants/([^/]+)/backup$"),
            "backup",
        ),
        (
            "PUT",
            re.compile(r"^/v1/tenants/([^/]+)/projects$"),
            "project_create",
        ),
    )

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.read_timeout_seconds)

    def do_POST(self) -> None:
        self._handle_request("POST")

    def do_PUT(self) -> None:
        self._handle_request("PUT")

    def do_GET(self) -> None:
        request_id = self._request_id()
        trace_id = self._trace_id()
        self._response_trace_id = trace_id
        if self.path == "/healthz":
            self._write(
                HostedAPIResponse(
                    200,
                    self.server.operations.liveness(),
                    {"X-Request-ID": request_id},
                )
            )
            return
        if self.path == "/readyz":
            result = self.server.operations.readiness()
            status = 200 if result["status"] == "ready" else 503
            self._write(
                HostedAPIResponse(
                    status,
                    {"status": result["status"]},
                    {"X-Request-ID": request_id},
                )
            )
            return
        self._write_error(
            HostedAPIError(
                HostedAPIErrorCode.INVALID_REQUEST,
                404,
                "hosted API route was not found",
            ),
            request_id,
        )

    def do_OPTIONS(self) -> None:
        request_id = self._request_id()
        try:
            _, _, operation = self._route(
                self.headers.get("Access-Control-Request-Method", ""),
                self.path,
            )
            headers = self._headers()
            resolved = self.server.adapter.policy.resolve_request(
                peer_ip=self.client_address[0],
                connection_secure=self.server.connection_secure,
                api_version=headers.get("x-context-memory-api-version"),
                origin=headers.get("origin"),
                forwarded_for=headers.get("x-forwarded-for"),
                forwarded_proto=headers.get("x-forwarded-proto"),
            )
            requested_headers = headers.get(
                "access-control-request-headers", ""
            )
            allowed = {
                "content-type",
                "idempotency-key",
                "x-context-memory-api-version",
                "x-request-id",
                "x-trace-id",
            }
            values = {
                value.strip().lower()
                for value in requested_headers.split(",")
                if value.strip()
            }
            if not values <= allowed:
                raise self._invalid("preflight requested unsupported headers")
            response_headers = self.server.adapter._response_headers(
                request_id,
                resolved.api_version,
                resolved.cors_origin,
            )
            response_headers.update(
                {
                    "Access-Control-Allow-Methods": (
                        "PUT" if operation == "project_create" else "POST"
                    ),
                    "Access-Control-Allow-Headers": ", ".join(sorted(allowed)),
                    "Access-Control-Max-Age": "600",
                }
            )
            self._write(HostedAPIResponse(204, {}, response_headers))
        except HostedAPIError as exc:
            self._write(
                HostedAPIResponse(
                    exc.status,
                    exc.envelope(request_id),
                    self.server.adapter._response_headers(
                        request_id,
                        self.server.adapter.policy.supported_api_versions[0],
                        None,
                    ),
                )
            )
        except (ValueError, UnicodeError):
            self._write_error(
                self._invalid("preflight request is malformed"), request_id
            )
        except Exception:
            self._write_error(
                HostedAPIError(
                    HostedAPIErrorCode.INTERNAL_ERROR,
                    500,
                    "internal server error",
                ),
                request_id,
            )

    def _handle_request(self, method: str) -> None:
        request_id = self._request_id()
        trace_id = self._trace_id()
        self._response_trace_id = trace_id
        operation = "unmatched"
        started: float | None = None
        self._last_response_status = 500
        try:
            tenant_id, project_id, operation = self._route(method, self.path)
            started = self.server.operations.request_started(
                request_id, trace_id, operation
            )
            length = self._content_length()
            self.server.adapter.policy.validate_body_length(length)
            body = self.rfile.read(length)
            if len(body) != length:
                raise self._invalid("request body ended before Content-Length")
            headers = self._headers()
            session = self.server.session_resolver(headers, tenant_id)
            request = HostedAPIRequest(
                request_id=request_id,
                operation=operation,
                tenant_id=tenant_id,
                project_id=project_id,
                body=body,
                content_length=length,
                peer_ip=self.client_address[0],
                connection_secure=self.server.connection_secure,
                session=session,
                headers=headers,
            )
            self._write(self._run_adapter(request))
        except socket.timeout:
            self.close_connection = True
            error = HostedAPIError(
                HostedAPIErrorCode.DEADLINE_EXCEEDED,
                408,
                "request body read deadline exceeded",
            )
            self._write_error(error, request_id)
        except HostedAPIError as exc:
            self._write_error(exc, request_id)
        except (ValueError, UnicodeError):
            self._write_error(
                self._invalid("request path or headers are malformed"),
                request_id,
            )
        except Exception:
            self._write_error(
                HostedAPIError(
                    HostedAPIErrorCode.INTERNAL_ERROR,
                    500,
                    "internal server error",
                ),
                request_id,
            )
        finally:
            if started is None:
                started = self.server.operations.request_started(
                    request_id, trace_id, operation
                )
            self.server.operations.request_finished(
                request_id,
                trace_id,
                operation,
                self._last_response_status,
                started,
            )

    def _run_adapter(self, request: HostedAPIRequest) -> HostedAPIResponse:
        results: queue.Queue[HostedAPIResponse] = queue.Queue(maxsize=1)
        worker = threading.Thread(
            target=lambda: results.put(self.server.adapter.handle(request)),
            daemon=True,
        )
        worker.start()
        disconnected = False
        while worker.is_alive():
            worker.join(timeout=0.01)
            if not disconnected and self._client_disconnected():
                disconnected = True
                self.server.adapter.cancel(request.request_id)
        return results.get_nowait()

    def _client_disconnected(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _route(
        self, method: str, raw_path: str
    ) -> tuple[str, str | None, str]:
        parsed = urlsplit(raw_path)
        if parsed.query or parsed.fragment:
            raise self._invalid("query strings are not supported")
        for expected_method, pattern, operation in self.ROUTES:
            match = pattern.fullmatch(parsed.path)
            if match and method == expected_method:
                values = tuple(
                    self._path_value(value) for value in match.groups()
                )
                return (
                    values[0],
                    values[1] if len(values) > 1 else None,
                    operation,
                )
        raise HostedAPIError(
            HostedAPIErrorCode.INVALID_REQUEST,
            404,
            "hosted API route was not found",
        )

    @staticmethod
    def _path_value(value: str) -> str:
        decoded = unquote(value)
        if not decoded or "/" in decoded or decoded in {".", ".."}:
            raise ValueError("invalid path segment")
        return decoded

    def _content_length(self) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None or not raw.isascii() or not raw.isdigit():
            raise self._invalid("valid Content-Length is required")
        return int(raw)

    def _request_id(self) -> str:
        value = self.headers.get("X-Request-ID")
        return self._correlation_id(value)

    def _trace_id(self) -> str:
        return self._correlation_id(self.headers.get("X-Trace-ID"))

    @staticmethod
    def _correlation_id(value: str | None) -> str:
        if (
            value
            and len(value) <= 128
            and value.isascii()
            and all(
                character.isalnum() or character in "-_."
                for character in value
            )
        ):
            return value
        return str(uuid.uuid4())

    def _headers(self) -> dict[str, str]:
        return {name.lower(): value for name, value in self.headers.items()}

    @staticmethod
    def _invalid(message: str) -> HostedAPIError:
        return HostedAPIError(HostedAPIErrorCode.INVALID_REQUEST, 400, message)

    def _write_error(self, error: HostedAPIError, request_id: str) -> None:
        self._write(
            HostedAPIResponse(
                error.status,
                error.envelope(request_id),
                self.server.adapter._response_headers(
                    request_id,
                    self.server.adapter.policy.supported_api_versions[0],
                    None,
                ),
            )
        )

    def _write(self, response: HostedAPIResponse) -> None:
        self._last_response_status = response.status
        body = (
            b""
            if response.status == 204
            else json.dumps(
                response.body, ensure_ascii=False, separators=(",", ":")
            ).encode()
        )
        try:
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            trace_id = getattr(self, "_response_trace_id", None)
            if trace_id:
                self.send_header("X-Trace-ID", trace_id)
                if "Access-Control-Allow-Origin" in response.headers:
                    self.send_header(
                        "Access-Control-Expose-Headers",
                        "X-Request-ID, X-Trace-ID",
                    )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return

    def log_message(self, fmt: str, *args: object) -> None:
        return
