from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import wraps
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class HostedAPIErrorCode(StrEnum):
    ACCESS_DENIED = "access_denied"
    BODY_TOO_LARGE = "body_too_large"
    CORS_ORIGIN_DENIED = "cors_origin_denied"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    IDEMPOTENCY_IN_PROGRESS = "idempotency_in_progress"
    INSECURE_TRANSPORT = "insecure_transport"
    INTERNAL_ERROR = "internal_error"
    INVALID_REQUEST = "invalid_request"
    INVALID_CURSOR = "invalid_cursor"
    REQUEST_CANCELLED = "request_cancelled"
    RATE_LIMITED = "rate_limited"
    STORAGE_EXHAUSTED = "storage_exhausted"
    UNSUPPORTED_API_VERSION = "unsupported_api_version"


class HostedAPIError(RuntimeError):
    def __init__(
        self,
        code: HostedAPIErrorCode,
        status: int,
        message: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after_seconds = retry_after_seconds

    def envelope(self, request_id: str) -> dict[str, object]:
        error: dict[str, object] = {
            "code": self.code.value,
            "message": str(self),
            "request_id": request_id,
        }
        if self.retry_after_seconds is not None:
            error["retry_after_seconds"] = self.retry_after_seconds
        return {"error": error}


@dataclass(frozen=True)
class HostedTransportPolicy:
    max_body_bytes: int = 1_048_576
    request_timeout_seconds: int = 30
    cursor_ttl_seconds: int = 900
    idempotency_retention_seconds: int = 86_400
    supported_api_versions: tuple[str, ...] = ("v1",)
    allowed_origins: tuple[str, ...] = ()
    trusted_proxy_cidrs: tuple[str, ...] = ()
    require_https: bool = True

    def __post_init__(self) -> None:
        positive = (
            self.max_body_bytes,
            self.request_timeout_seconds,
            self.cursor_ttl_seconds,
            self.idempotency_retention_seconds,
        )
        if any(value < 1 for value in positive):
            raise ValueError("transport limits must be positive")
        if not self.supported_api_versions:
            raise ValueError("at least one API version is required")
        for cidr in self.trusted_proxy_cidrs:
            ipaddress.ip_network(cidr)

    def validate_body_length(self, content_length: int) -> None:
        if content_length < 0 or content_length > self.max_body_bytes:
            raise HostedAPIError(
                HostedAPIErrorCode.BODY_TOO_LARGE,
                413,
                "request body exceeds the configured limit",
            )

    def resolve_request(
        self,
        *,
        peer_ip: str,
        connection_secure: bool,
        api_version: str | None,
        origin: str | None,
        forwarded_for: str | None = None,
        forwarded_proto: str | None = None,
    ) -> ResolvedHostedRequest:
        version = api_version or self.supported_api_versions[0]
        if version not in self.supported_api_versions:
            raise HostedAPIError(
                HostedAPIErrorCode.UNSUPPORTED_API_VERSION,
                400,
                "unsupported API version",
            )
        if origin is not None and origin not in self.allowed_origins:
            raise HostedAPIError(
                HostedAPIErrorCode.CORS_ORIGIN_DENIED,
                403,
                "request origin is not allowed",
            )

        client_ip = str(ipaddress.ip_address(peer_ip))
        scheme = "https" if connection_secure else "http"
        if self._is_trusted_proxy(client_ip):
            if forwarded_for:
                candidate = forwarded_for.split(",", 1)[0].strip()
                client_ip = str(ipaddress.ip_address(candidate))
            if forwarded_proto:
                if forwarded_proto not in {"http", "https"}:
                    raise HostedAPIError(
                        HostedAPIErrorCode.INSECURE_TRANSPORT,
                        400,
                        "forwarded transport scheme is invalid",
                    )
                scheme = forwarded_proto
        if self.require_https and scheme != "https":
            raise HostedAPIError(
                HostedAPIErrorCode.INSECURE_TRANSPORT,
                400,
                "HTTPS is required",
            )
        return ResolvedHostedRequest(
            client_ip=client_ip,
            scheme=scheme,
            api_version=version,
            cors_origin=origin,
        )

    def _is_trusted_proxy(self, peer_ip: str) -> bool:
        address = ipaddress.ip_address(peer_ip)
        return any(
            address in ipaddress.ip_network(cidr)
            for cidr in self.trusted_proxy_cidrs
        )


@dataclass(frozen=True)
class ResolvedHostedRequest:
    client_ip: str
    scheme: str
    api_version: str
    cors_origin: str | None


class HostedRequestContext:
    """Cooperative request deadline and cancellation state."""

    def __init__(
        self,
        request_id: str,
        timeout_seconds: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not request_id:
            raise ValueError("request_id is required")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        self.request_id = request_id
        self.clock = clock
        self.deadline = clock() + timedelta(seconds=timeout_seconds)
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def check(self) -> None:
        if self._cancelled.is_set():
            raise HostedAPIError(
                HostedAPIErrorCode.REQUEST_CANCELLED,
                499,
                "request was cancelled",
            )
        if self.clock() >= self.deadline:
            raise HostedAPIError(
                HostedAPIErrorCode.DEADLINE_EXCEEDED,
                504,
                "request deadline exceeded",
            )


class HostedCursorCodec:
    """Issue tenant- and route-bound opaque pagination cursors."""

    def __init__(
        self,
        signing_key: bytes,
        ttl_seconds: int = 900,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("cursor signing key must be at least 32 bytes")
        if ttl_seconds < 1:
            raise ValueError("cursor TTL must be positive")
        self.signing_key = signing_key
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    def encode(self, tenant_id: str, route: str, offset: int) -> str:
        if offset < 0:
            raise ValueError("cursor offset cannot be negative")
        payload = {
            "exp": int(self.clock().timestamp()) + self.ttl_seconds,
            "offset": offset,
            "route": route,
            "tenant_id": tenant_id,
            "version": 1,
        }
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode()
        signature = hmac.new(self.signing_key, raw, hashlib.sha256).digest()
        return self._encode(raw + signature)

    def decode(self, cursor: str, tenant_id: str, route: str) -> int:
        try:
            packed = self._decode(cursor)
            raw, signature = packed[:-32], packed[-32:]
            expected = hmac.new(self.signing_key, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(raw)
            if (
                payload.get("version") != 1
                or payload.get("tenant_id") != tenant_id
                or payload.get("route") != route
                or not isinstance(payload.get("offset"), int)
                or payload["offset"] < 0
                or not isinstance(payload.get("exp"), int)
                or payload["exp"] <= int(self.clock().timestamp())
            ):
                raise ValueError
            return payload["offset"]
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise HostedAPIError(
                HostedAPIErrorCode.INVALID_CURSOR,
                400,
                "pagination cursor is invalid or expired",
            ) from None

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    @staticmethod
    def _decode(value: str) -> bytes:
        if not value or not value.isascii():
            raise ValueError
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)


@dataclass(frozen=True)
class HostedIdempotencyClaim:
    state: str
    response: Any = None


class HostedIdempotencyStore:
    """Persist bounded API replay results across process restarts."""

    def __init__(
        self,
        path: str | Path,
        retention_seconds: int = 86_400,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if retention_seconds < 1:
            raise ValueError("idempotency retention must be positive")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.retention_seconds = retention_seconds
        self.clock = clock
        self._lock = RLock()
        self.connection = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS hosted_idempotency(
              tenant_id TEXT NOT NULL,
              operation TEXT NOT NULL,
              key TEXT NOT NULL,
              request_hash TEXT NOT NULL,
              state TEXT NOT NULL,
              response_json TEXT,
              expires_at TEXT NOT NULL,
              PRIMARY KEY(tenant_id, operation, key)
            )
            """
        )

    @_serialized
    def close(self) -> None:
        self.connection.close()

    @_serialized
    def claim(
        self,
        tenant_id: str,
        operation: str,
        key: str,
        request: Any,
    ) -> HostedIdempotencyClaim:
        if not key:
            raise ValueError("idempotency key is required")
        request_hash = self._request_hash(request)
        now = self.clock()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "DELETE FROM hosted_idempotency WHERE expires_at <= ?",
                (now.isoformat(),),
            )
            row = self.connection.execute(
                """
                SELECT request_hash, state, response_json
                FROM hosted_idempotency
                WHERE tenant_id = ? AND operation = ? AND key = ?
                """,
                (tenant_id, operation, key),
            ).fetchone()
            if row is None:
                expires_at = now + timedelta(seconds=self.retention_seconds)
                self.connection.execute(
                    """
                    INSERT INTO hosted_idempotency(
                      tenant_id, operation, key, request_hash, state,
                      response_json, expires_at
                    ) VALUES(?, ?, ?, ?, 'pending', NULL, ?)
                    """,
                    (
                        tenant_id,
                        operation,
                        key,
                        request_hash,
                        expires_at.isoformat(),
                    ),
                )
                self.connection.commit()
                return HostedIdempotencyClaim("new")
            self.connection.rollback()
        except Exception:
            self.connection.rollback()
            raise
        if row["request_hash"] != request_hash:
            raise HostedAPIError(
                HostedAPIErrorCode.IDEMPOTENCY_CONFLICT,
                409,
                "idempotency key was reused with a different request",
            )
        if row["state"] == "pending":
            raise HostedAPIError(
                HostedAPIErrorCode.IDEMPOTENCY_IN_PROGRESS,
                409,
                "an identical request is still in progress",
                retry_after_seconds=1,
            )
        return HostedIdempotencyClaim(
            "replay", json.loads(row["response_json"])
        )

    @_serialized
    def complete(
        self,
        tenant_id: str,
        operation: str,
        key: str,
        request: Any,
        response: Any,
    ) -> None:
        request_hash = self._request_hash(request)
        response_json = json.dumps(
            response, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        cursor = self.connection.execute(
            """
            UPDATE hosted_idempotency
            SET state = 'complete', response_json = ?
            WHERE tenant_id = ? AND operation = ? AND key = ?
              AND request_hash = ? AND state = 'pending'
            """,
            (
                response_json,
                tenant_id,
                operation,
                key,
                request_hash,
            ),
        )
        if cursor.rowcount != 1:
            raise HostedAPIError(
                HostedAPIErrorCode.IDEMPOTENCY_CONFLICT,
                409,
                "idempotency claim is missing or already completed",
            )

    @_serialized
    def abandon(
        self,
        tenant_id: str,
        operation: str,
        key: str,
        request: Any,
    ) -> bool:
        cursor = self.connection.execute(
            """
            DELETE FROM hosted_idempotency
            WHERE tenant_id = ? AND operation = ? AND key = ?
              AND request_hash = ? AND state = 'pending'
            """,
            (
                tenant_id,
                operation,
                key,
                self._request_hash(request),
            ),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _request_hash(request: Any) -> str:
        raw = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(raw).hexdigest()
