from __future__ import annotations

from typing import Protocol

from .hosted_authorization import (
    HostedAction,
    HostedResource,
    HostedSession,
    authorize_hosted_action,
)


class TenantConstrainedRepository(Protocol):
    """Hosted content operations that require an explicit tenant key."""

    def search(
        self, tenant_id: str, project_id: str, query: str
    ) -> object: ...

    def export_project(self, tenant_id: str, project_id: str) -> object: ...

    def poll_events(
        self, tenant_id: str, project_id: str, cursor: int | None
    ) -> object: ...

    def backup_tenant(self, tenant_id: str) -> object: ...


class HostedRepositoryDeniedError(PermissionError):
    """Stable denial that reveals no resource existence."""

    def __init__(self, reason: str) -> None:
        super().__init__("hosted repository access denied")
        self.reason = reason


class HostedRepositoryGateway:
    """Authorize exact tenant-scoped repository calls."""

    def __init__(self, repository: TenantConstrainedRepository) -> None:
        self.repository = repository

    def search(
        self,
        session: HostedSession | None,
        tenant_id: str,
        project_id: str,
        query: str,
    ) -> object:
        self._require(
            session,
            HostedResource(tenant_id, project_id),
            HostedAction.SEARCH,
        )
        return self.repository.search(tenant_id, project_id, query)

    def export_project(
        self,
        session: HostedSession | None,
        tenant_id: str,
        project_id: str,
    ) -> object:
        self._require(
            session,
            HostedResource(tenant_id, project_id),
            HostedAction.EXPORT,
        )
        return self.repository.export_project(tenant_id, project_id)

    def poll_events(
        self,
        session: HostedSession | None,
        tenant_id: str,
        project_id: str,
        cursor: int | None = None,
    ) -> object:
        self._require(
            session,
            HostedResource(tenant_id, project_id),
            HostedAction.EVENT_POLL,
        )
        return self.repository.poll_events(tenant_id, project_id, cursor)

    def backup_tenant(
        self, session: HostedSession | None, tenant_id: str
    ) -> object:
        self._require(
            session,
            HostedResource(tenant_id),
            HostedAction.BACKUP,
        )
        return self.repository.backup_tenant(tenant_id)

    @staticmethod
    def _require(
        session: HostedSession | None,
        resource: HostedResource,
        action: HostedAction,
    ) -> None:
        decision = authorize_hosted_action(session, resource, action)
        if not decision.allowed:
            raise HostedRepositoryDeniedError(decision.reason)
