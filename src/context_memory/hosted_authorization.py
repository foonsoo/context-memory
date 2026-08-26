from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import AbstractSet


class HostedAction(StrEnum):
    SEARCH = "search"
    EXPORT = "export"
    EVENT_POLL = "event_poll"
    BACKUP = "backup"


ROLE_PERMISSIONS = MappingProxyType(
    {
        "project_reader": frozenset(
            {HostedAction.SEARCH, HostedAction.EVENT_POLL}
        ),
        "project_exporter": frozenset({HostedAction.EXPORT}),
        "tenant_backup_operator": frozenset({HostedAction.BACKUP}),
        "tenant_admin": frozenset(HostedAction),
        "service_reader": frozenset(
            {HostedAction.SEARCH, HostedAction.EVENT_POLL}
        ),
    }
)


@dataclass(frozen=True)
class HostedSession:
    """Identity state supplied by a future hosted auth boundary."""

    actor_id: str
    tenant_id: str
    session_id: str
    roles: frozenset[str]
    project_ids: frozenset[str]
    active: bool = True


@dataclass(frozen=True)
class HostedResource:
    tenant_id: str
    project_id: str | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str


def authorize_hosted_action(
    session: HostedSession | None,
    resource: HostedResource,
    action: HostedAction,
) -> AuthorizationDecision:
    """Apply the hosted deny-by-default tenant and project policy."""
    if session is None:
        return AuthorizationDecision(False, "authentication_required")
    if not session.active:
        return AuthorizationDecision(False, "session_inactive")
    if not session.actor_id or not session.tenant_id or not session.session_id:
        return AuthorizationDecision(False, "invalid_identity_context")
    if session.tenant_id != resource.tenant_id:
        return AuthorizationDecision(False, "tenant_mismatch")

    permissions = _permissions_for(session.roles)
    if action not in permissions:
        return AuthorizationDecision(False, "permission_denied")

    if action is HostedAction.BACKUP:
        if resource.project_id is not None:
            return AuthorizationDecision(False, "tenant_resource_required")
        return AuthorizationDecision(True, "authorized")

    if resource.project_id is None:
        return AuthorizationDecision(False, "project_required")
    if resource.project_id not in session.project_ids:
        return AuthorizationDecision(False, "project_not_granted")
    return AuthorizationDecision(True, "authorized")


def _permissions_for(roles: AbstractSet[str]) -> frozenset[HostedAction]:
    permissions: set[HostedAction] = set()
    for role in roles:
        permissions.update(ROLE_PERMISSIONS.get(role, ()))
    return frozenset(permissions)
