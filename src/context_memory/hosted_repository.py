from __future__ import annotations

from typing import Protocol

from .hosted_authorization import (
    HostedAction,
    HostedResource,
    HostedSession,
    authorize_hosted_action,
)
from .hosted_limits import HostedRateLimiter


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


class HostedRepositoryRateLimitError(RuntimeError):
    """Stable throttling result without repository-state disclosure."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("hosted repository rate limit exceeded")
        self.retry_after_seconds = retry_after_seconds


class HostedRepositoryGateway:
    """Authorize exact tenant-scoped repository calls."""

    def __init__(
        self,
        repository: TenantConstrainedRepository,
        rate_limiter: HostedRateLimiter | None = None,
    ) -> None:
        self.repository = repository
        self.rate_limiter = rate_limiter

    def search(
        self,
        session: HostedSession | None,
        tenant_id: str,
        project_id: str,
        query: str,
    ) -> object:
        self._consume(session, HostedAction.SEARCH)
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
        self._consume(session, HostedAction.EXPORT)
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
        self._consume(session, HostedAction.EVENT_POLL)
        self._require(
            session,
            HostedResource(tenant_id, project_id),
            HostedAction.EVENT_POLL,
        )
        return self.repository.poll_events(tenant_id, project_id, cursor)

    def backup_tenant(
        self, session: HostedSession | None, tenant_id: str
    ) -> object:
        self._consume(session, HostedAction.BACKUP)
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

    def _consume(
        self, session: HostedSession | None, action: HostedAction
    ) -> None:
        if self.rate_limiter is None:
            return
        if session is None or not session.tenant_id or not session.actor_id:
            return
        decision = self.rate_limiter.consume(
            session.tenant_id, session.actor_id, action.value
        )
        if not decision.allowed:
            raise HostedRepositoryRateLimitError(decision.retry_after_seconds)
