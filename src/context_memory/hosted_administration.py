from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import AbstractSet, Protocol

from .hosted_authorization import HostedSession
from .hosted_limits import HostedRateLimiter


class HostedAdminAction(StrEnum):
    PROJECT_CREATE = "project_create"
    GRANT_ADMIN = "grant_admin"
    SESSION_ADMIN = "session_admin"
    ACTOR_DELETE = "actor_delete"
    SERVICE_ROLE_ADMIN = "service_role_admin"


ADMIN_ROLE_PERMISSIONS = MappingProxyType(
    {
        "tenant_admin": frozenset(
            {
                HostedAdminAction.PROJECT_CREATE,
                HostedAdminAction.GRANT_ADMIN,
                HostedAdminAction.SESSION_ADMIN,
                HostedAdminAction.ACTOR_DELETE,
            }
        ),
        "tenant_security_admin": frozenset(
            {HostedAdminAction.SERVICE_ROLE_ADMIN}
        ),
    }
)


@dataclass(frozen=True)
class AdminDecision:
    allowed: bool
    reason: str


def authorize_admin_action(
    session: HostedSession | None,
    tenant_id: str,
    action: HostedAdminAction,
) -> AdminDecision:
    """Authorize a tenant administration action explicitly."""
    if session is None:
        return AdminDecision(False, "authentication_required")
    if not session.active:
        return AdminDecision(False, "session_inactive")
    if not session.actor_id or not session.tenant_id or not session.session_id:
        return AdminDecision(False, "invalid_identity_context")
    if session.tenant_id != tenant_id:
        return AdminDecision(False, "tenant_mismatch")
    if action not in _admin_permissions_for(session.roles):
        return AdminDecision(False, "permission_denied")
    return AdminDecision(True, "authorized")


def _admin_permissions_for(
    roles: AbstractSet[str],
) -> frozenset[HostedAdminAction]:
    permissions: set[HostedAdminAction] = set()
    for role in roles:
        permissions.update(ADMIN_ROLE_PERMISSIONS.get(role, ()))
    return frozenset(permissions)


class HostedAdministrationStore(Protocol):
    def provision_project(self, tenant_id: str, project_id: str) -> None: ...

    def grant_project(
        self, tenant_id: str, actor_id: str, project_id: str
    ) -> None: ...

    def revoke_project_grant(
        self, tenant_id: str, actor_id: str, project_id: str
    ) -> bool: ...

    def assign_role(
        self, tenant_id: str, actor_id: str, role: str
    ) -> None: ...

    def revoke_role(
        self, tenant_id: str, actor_id: str, role: str
    ) -> bool: ...

    def revoke_session(self, tenant_id: str, session_id: str) -> bool: ...

    def issue_session(
        self,
        tenant_id: str,
        session_id: str,
        actor_id: str,
        expires_at: datetime,
    ) -> None: ...

    def delete_actor(self, tenant_id: str, actor_id: str) -> bool: ...

    def record_security_audit(
        self,
        *,
        tenant_id: str | None,
        actor_id: str | None,
        session_id: str | None,
        action: str,
        decision: str,
        reason: str,
        request_id: str,
        target_type: str,
        target_id: str,
    ) -> None: ...


class HostedAdministrationDeniedError(PermissionError):
    def __init__(self, reason: str) -> None:
        super().__init__("hosted administration access denied")
        self.reason = reason


class HostedAdministrationRateLimitError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("hosted administration rate limit exceeded")
        self.retry_after_seconds = retry_after_seconds


class HostedAdministrationGateway:
    """Authorize and audit privileged identity mutations."""

    def __init__(
        self,
        store: HostedAdministrationStore,
        rate_limiter: HostedRateLimiter | None = None,
    ) -> None:
        self.store = store
        self.rate_limiter = rate_limiter

    def create_project(
        self,
        session: HostedSession | None,
        tenant_id: str,
        project_id: str,
        request_id: str,
    ) -> None:
        self._perform(
            session,
            tenant_id,
            HostedAdminAction.PROJECT_CREATE,
            request_id,
            "project",
            project_id,
            lambda: self.store.provision_project(tenant_id, project_id),
        )

    def grant_project(
        self,
        session: HostedSession | None,
        tenant_id: str,
        actor_id: str,
        project_id: str,
        request_id: str,
    ) -> None:
        self._perform(
            session,
            tenant_id,
            HostedAdminAction.GRANT_ADMIN,
            request_id,
            "project_grant",
            f"{actor_id}:{project_id}",
            lambda: self.store.grant_project(tenant_id, actor_id, project_id),
        )

    def revoke_project(
        self,
        session: HostedSession | None,
        tenant_id: str,
        actor_id: str,
        project_id: str,
        request_id: str,
    ) -> bool:
        result = self._perform(
            session,
            tenant_id,
            HostedAdminAction.GRANT_ADMIN,
            request_id,
            "project_grant",
            f"{actor_id}:{project_id}",
            lambda: self.store.revoke_project_grant(
                tenant_id, actor_id, project_id
            ),
        )
        return bool(result)

    def assign_role(
        self,
        session: HostedSession | None,
        tenant_id: str,
        actor_id: str,
        role: str,
        request_id: str,
    ) -> None:
        allowed_roles = {
            "project_reader",
            "project_exporter",
            "tenant_backup_operator",
        }
        if role not in allowed_roles:
            raise ValueError("unsupported administrable role")
        self._perform(
            session,
            tenant_id,
            HostedAdminAction.GRANT_ADMIN,
            request_id,
            "role_assignment",
            f"{actor_id}:{role}",
            lambda: self.store.assign_role(tenant_id, actor_id, role),
        )

    def revoke_role(
        self,
        session: HostedSession | None,
        tenant_id: str,
        actor_id: str,
        role: str,
        request_id: str,
    ) -> bool:
        if role in {"tenant_admin", "tenant_security_admin"}:
            raise ValueError(
                "privileged bootstrap role cannot be revoked here"
            )
        action = (
            HostedAdminAction.SERVICE_ROLE_ADMIN
            if role == "service_reader"
            else HostedAdminAction.GRANT_ADMIN
        )
        result = self._perform(
            session,
            tenant_id,
            action,
            request_id,
            "role_assignment",
            f"{actor_id}:{role}",
            lambda: self.store.revoke_role(tenant_id, actor_id, role),
        )
        return bool(result)

    def assign_service_role(
        self,
        session: HostedSession | None,
        tenant_id: str,
        actor_id: str,
        role: str,
        request_id: str,
    ) -> None:
        if role != "service_reader":
            raise ValueError("unsupported service role")
        self._perform(
            session,
            tenant_id,
            HostedAdminAction.SERVICE_ROLE_ADMIN,
            request_id,
            "role_assignment",
            f"{actor_id}:{role}",
            lambda: self.store.assign_role(tenant_id, actor_id, role),
        )

    def revoke_session(
        self,
        session: HostedSession | None,
        tenant_id: str,
        target_session_id: str,
        request_id: str,
    ) -> bool:
        result = self._perform(
            session,
            tenant_id,
            HostedAdminAction.SESSION_ADMIN,
            request_id,
            "session",
            target_session_id,
            lambda: self.store.revoke_session(tenant_id, target_session_id),
        )
        return bool(result)

    def issue_session(
        self,
        session: HostedSession | None,
        tenant_id: str,
        target_session_id: str,
        actor_id: str,
        expires_at: datetime,
        request_id: str,
    ) -> None:
        self._perform(
            session,
            tenant_id,
            HostedAdminAction.SESSION_ADMIN,
            request_id,
            "session",
            target_session_id,
            lambda: self.store.issue_session(
                tenant_id, target_session_id, actor_id, expires_at
            ),
        )

    def delete_actor(
        self,
        session: HostedSession | None,
        tenant_id: str,
        actor_id: str,
        request_id: str,
    ) -> bool:
        result = self._perform(
            session,
            tenant_id,
            HostedAdminAction.ACTOR_DELETE,
            request_id,
            "actor",
            actor_id,
            lambda: self.store.delete_actor(tenant_id, actor_id),
        )
        return bool(result)

    def _perform(
        self,
        session: HostedSession | None,
        tenant_id: str,
        action: HostedAdminAction,
        request_id: str,
        target_type: str,
        target_id: str,
        operation,
    ):
        if not request_id:
            raise ValueError("request_id is required")
        if (
            self.rate_limiter is not None
            and session is not None
            and session.tenant_id
            and session.actor_id
        ):
            rate = self.rate_limiter.consume(
                session.tenant_id, session.actor_id, action.value
            )
            if not rate.allowed:
                self.store.record_security_audit(
                    tenant_id=session.tenant_id,
                    actor_id=session.actor_id,
                    session_id=session.session_id,
                    action=action.value,
                    decision="denied",
                    reason="rate_limited",
                    request_id=request_id,
                    target_type=target_type,
                    target_id=target_id,
                )
                raise HostedAdministrationRateLimitError(
                    rate.retry_after_seconds
                )
        decision = authorize_admin_action(session, tenant_id, action)
        audit_tenant = session.tenant_id if session else None
        audit = {
            "tenant_id": audit_tenant,
            "actor_id": session.actor_id if session else None,
            "session_id": session.session_id if session else None,
            "action": action.value,
            "decision": "allowed" if decision.allowed else "denied",
            "reason": decision.reason,
            "request_id": request_id,
            "target_type": target_type,
            "target_id": target_id,
        }
        if not decision.allowed:
            self.store.record_security_audit(**audit)
            raise HostedAdministrationDeniedError(decision.reason)
        self.store.record_security_audit(
            **{**audit, "decision": "attempted", "reason": "authorized"}
        )
        try:
            result = operation()
        except Exception:
            self.store.record_security_audit(
                **{**audit, "decision": "failed", "reason": "store_error"}
            )
            raise
        self.store.record_security_audit(**audit)
        return result
