from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Mapping

from .hosted_administration import (
    HostedAdministrationDeniedError,
    HostedAdministrationGateway,
    HostedAdministrationRateLimitError,
)
from .hosted_authorization import HostedSession
from .hosted_operations import (
    HostedStorageExhaustedError,
    translate_storage_error,
)
from .hosted_repository import (
    HostedRepositoryDeniedError,
    HostedRepositoryGateway,
    HostedRepositoryRateLimitError,
)
from .hosted_transport import (
    HostedAPIError,
    HostedAPIErrorCode,
    HostedCursorCodec,
    HostedIdempotencyStore,
    HostedRequestContext,
    HostedTransportPolicy,
)


@dataclass(frozen=True)
class HostedAPIRequest:
    request_id: str
    operation: str
    tenant_id: str
    body: bytes
    content_length: int
    peer_ip: str
    connection_secure: bool
    session: HostedSession | None
    project_id: str | None = None
    headers: Mapping[str, str] | None = None


@dataclass(frozen=True)
class HostedAPIResponse:
    status: int
    body: dict[str, object]
    headers: dict[str, str]


class HostedAPIAdapter:
    """Apply hosted transport policy before P7-4 gateways."""

    def __init__(
        self,
        repository: HostedRepositoryGateway,
        administration: HostedAdministrationGateway,
        policy: HostedTransportPolicy,
        cursors: HostedCursorCodec,
        idempotency: HostedIdempotencyStore,
        context_factory: Callable[
            [str, int], HostedRequestContext
        ] = HostedRequestContext,
    ) -> None:
        self.repository = repository
        self.administration = administration
        self.policy = policy
        self.cursors = cursors
        self.idempotency = idempotency
        self.context_factory = context_factory
        self._contexts: dict[str, HostedRequestContext] = {}
        self._context_lock = Lock()

    def cancel(self, request_id: str) -> bool:
        with self._context_lock:
            context = self._contexts.get(request_id)
        if context is None:
            return False
        context.cancel()
        return True

    def handle(self, request: HostedAPIRequest) -> HostedAPIResponse:
        context: HostedRequestContext | None = None
        version = self.policy.supported_api_versions[0]
        origin: str | None = None
        try:
            self._validate_request_identity(request)
            self.policy.validate_body_length(request.content_length)
            if request.content_length != len(request.body):
                raise self._invalid(
                    "declared content length does not match body"
                )
            headers = self._normalized_headers(request.headers)
            resolved = self.policy.resolve_request(
                peer_ip=request.peer_ip,
                connection_secure=request.connection_secure,
                api_version=headers.get("x-context-memory-api-version"),
                origin=headers.get("origin"),
                forwarded_for=headers.get("x-forwarded-for"),
                forwarded_proto=headers.get("x-forwarded-proto"),
            )
            version = resolved.api_version
            origin = resolved.cors_origin
            context = self.context_factory(
                request.request_id,
                self.policy.request_timeout_seconds,
            )
            with self._context_lock:
                if request.request_id in self._contexts:
                    raise self._invalid("request ID is already active")
                self._contexts[request.request_id] = context
            context.check()
            body = self._decode_body(request.body)
            result = self._dispatch(request, headers, body, context)
            context.check()
            return HostedAPIResponse(
                200,
                {
                    "api_version": version,
                    "request_id": request.request_id,
                    "result": result,
                },
                self._response_headers(request.request_id, version, origin),
            )
        except HostedAPIError as exc:
            return HostedAPIResponse(
                exc.status,
                exc.envelope(request.request_id or "unassigned"),
                self._response_headers(
                    request.request_id or "unassigned", version, origin
                ),
            )
        except (
            HostedRepositoryDeniedError,
            HostedAdministrationDeniedError,
        ):
            return self._error_response(
                request,
                HostedAPIErrorCode.ACCESS_DENIED,
                403,
                "access denied",
                version,
                origin,
            )
        except (
            HostedRepositoryRateLimitError,
            HostedAdministrationRateLimitError,
        ) as exc:
            return self._error_response(
                request,
                HostedAPIErrorCode.RATE_LIMITED,
                429,
                "request rate limit exceeded",
                version,
                origin,
                exc.retry_after_seconds,
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return self._error_response(
                request,
                HostedAPIErrorCode.INVALID_REQUEST,
                400,
                "request is malformed",
                version,
                origin,
            )
        except (OSError, sqlite3.OperationalError) as exc:
            translated = translate_storage_error(exc)
            if isinstance(translated, HostedStorageExhaustedError):
                return self._error_response(
                    request,
                    HostedAPIErrorCode.STORAGE_EXHAUSTED,
                    507,
                    "hosted storage capacity is exhausted",
                    version,
                    origin,
                )
            return self._error_response(
                request,
                HostedAPIErrorCode.INTERNAL_ERROR,
                500,
                "internal server error",
                version,
                origin,
            )
        except Exception:
            return self._error_response(
                request,
                HostedAPIErrorCode.INTERNAL_ERROR,
                500,
                "internal server error",
                version,
                origin,
            )
        finally:
            if context is not None:
                with self._context_lock:
                    if self._contexts.get(request.request_id) is context:
                        del self._contexts[request.request_id]

    def _dispatch(
        self,
        request: HostedAPIRequest,
        headers: dict[str, str],
        body: dict[str, Any],
        context: HostedRequestContext,
    ) -> object:
        context.check()
        if request.operation == "search":
            project_id = self._project(request)
            query = body.get("query")
            if not isinstance(query, str) or not query.strip():
                raise self._invalid("search query is required")
            return self.repository.search(
                request.session, request.tenant_id, project_id, query
            )
        if request.operation == "export":
            self._require_empty(body)
            return self.repository.export_project(
                request.session,
                request.tenant_id,
                self._project(request),
            )
        if request.operation == "event_poll":
            self._require_keys(body, {"cursor"})
            cursor_value = body.get("cursor")
            cursor = None
            if cursor_value is not None:
                if not isinstance(cursor_value, str):
                    raise self._invalid("cursor must be a string")
                cursor = self.cursors.decode(
                    cursor_value, request.tenant_id, "event_poll"
                )
            result = self.repository.poll_events(
                request.session,
                request.tenant_id,
                self._project(request),
                cursor,
            )
            if not isinstance(result, dict):
                raise TypeError("event poll result must be an object")
            response = dict(result)
            next_cursor = response.pop("next_cursor", None)
            if next_cursor is not None:
                response["next_cursor"] = self.cursors.encode(
                    request.tenant_id, "event_poll", int(next_cursor)
                )
            return response
        if request.operation == "backup":
            self._require_empty(body)
            value = self.repository.backup_tenant(
                request.session, request.tenant_id
            )
            if not isinstance(value, bytes):
                raise TypeError("backup result must be bytes")
            return {"encoding": "utf-8", "payload": value.decode()}
        if request.operation == "project_create":
            self._require_keys(body, {"project_id"})
            project_id = body.get("project_id")
            if not isinstance(project_id, str) or not project_id:
                raise self._invalid("project_id is required")
            key = headers.get("idempotency-key")
            if not key:
                raise self._invalid("Idempotency-Key is required")
            operation = "project_create"
            claim = self.idempotency.claim(
                request.tenant_id, operation, key, body
            )
            if claim.state == "replay":
                return claim.response
            try:
                context.check()
                self.administration.create_project(
                    request.session,
                    request.tenant_id,
                    project_id,
                    request.request_id,
                )
                result = {"created": True, "project_id": project_id}
                self.idempotency.complete(
                    request.tenant_id, operation, key, body, result
                )
            except Exception:
                self.idempotency.abandon(
                    request.tenant_id, operation, key, body
                )
                raise
            return result
        raise self._invalid("unknown hosted API operation")

    @staticmethod
    def _decode_body(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        value = json.loads(body)
        if not isinstance(value, dict):
            raise TypeError("request body must be an object")
        return value

    @staticmethod
    def _normalized_headers(
        headers: Mapping[str, str] | None,
    ) -> dict[str, str]:
        if headers is None:
            return {}
        return {
            str(name).lower(): str(value) for name, value in headers.items()
        }

    @staticmethod
    def _project(request: HostedAPIRequest) -> str:
        if not request.project_id:
            raise HostedAPIAdapter._invalid("project_id is required")
        return request.project_id

    @staticmethod
    def _require_empty(body: dict[str, Any]) -> None:
        if body:
            raise HostedAPIAdapter._invalid("request body must be empty")

    @staticmethod
    def _require_keys(body: dict[str, Any], allowed: set[str]) -> None:
        if extra := sorted(set(body) - allowed):
            raise HostedAPIAdapter._invalid(
                f"unknown request fields: {', '.join(extra)}"
            )

    @staticmethod
    def _validate_request_identity(request: HostedAPIRequest) -> None:
        if not request.request_id:
            raise HostedAPIAdapter._invalid("request_id is required")
        if not request.tenant_id:
            raise HostedAPIAdapter._invalid("tenant_id is required")

    @staticmethod
    def _invalid(message: str) -> HostedAPIError:
        return HostedAPIError(HostedAPIErrorCode.INVALID_REQUEST, 400, message)

    @staticmethod
    def _response_headers(
        request_id: str, version: str, origin: str | None
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Context-Memory-API-Version": version,
            "X-Request-ID": request_id,
        }
        if origin is not None:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        return headers

    @staticmethod
    def _error_response(
        request: HostedAPIRequest,
        code: HostedAPIErrorCode,
        status: int,
        message: str,
        version: str,
        origin: str | None,
        retry_after_seconds: int | None = None,
    ) -> HostedAPIResponse:
        error = HostedAPIError(code, status, message, retry_after_seconds)
        return HostedAPIResponse(
            status,
            error.envelope(request.request_id or "unassigned"),
            HostedAPIAdapter._response_headers(
                request.request_id or "unassigned", version, origin
            ),
        )
