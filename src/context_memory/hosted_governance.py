from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HostedGovernancePolicy:
    collection_purpose: str
    event_retention_days: int = 365
    backup_retention_days: int = 30
    storage_region: str = "operator-selected"
    storage_class: str = "encrypted-managed-storage"
    incident_contact: str = "operator-security-contact"
    incident_runbook: str = "docs/HOSTED_GOVERNANCE.md#incident-response"

    def __post_init__(self) -> None:
        if not self.collection_purpose.strip():
            raise ValueError("collection purpose is required")
        if self.event_retention_days < 1 or self.backup_retention_days < 1:
            raise ValueError("retention periods must be positive")
        for value in (
            self.storage_region,
            self.storage_class,
            self.incident_contact,
            self.incident_runbook,
        ):
            if not value.strip():
                raise ValueError("governance policy fields cannot be empty")


class GovernanceContentStore(Protocol):
    def record_event(
        self, tenant_id: str, project_id: str, kind: str, content: str
    ) -> dict[str, object]: ...

    def export_project(
        self, tenant_id: str, project_id: str
    ) -> dict[str, object]: ...

    def purge_events_before(self, tenant_id: str, cutoff: datetime) -> int: ...

    def erase_project(self, tenant_id: str, project_id: str) -> bool: ...

    def erase_tenant(self, tenant_id: str) -> bool: ...


class GovernanceIdentityStore(Protocol):
    def export_actor(
        self, tenant_id: str, actor_id: str
    ) -> dict[str, object]: ...

    def erase_actor(self, tenant_id: str, actor_id: str) -> bool: ...

    def erase_project(self, tenant_id: str, project_id: str) -> bool: ...

    def erase_tenant(self, tenant_id: str) -> bool: ...


SENSITIVE_PATTERNS = (
    (
        "private_key_material",
        "secret",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "credential_assignment",
        "secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*\S+"
        ),
    ),
    (
        "email_address",
        "personal_data",
        re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b"),
    ),
    (
        "korean_resident_number_shape",
        "personal_data",
        re.compile(r"\b\d{6}-[1-4]\d{6}\b"),
    ),
)


def sensitive_data_warnings(content: str) -> list[dict[str, object]]:
    """Return best-effort warnings without echoing matched content."""
    warnings = []
    for code, category, pattern in SENSITIVE_PATTERNS:
        count = len(pattern.findall(content))
        if count:
            warnings.append(
                {
                    "code": code,
                    "category": category,
                    "count": count,
                    "blocking": False,
                    "detection_is_complete": False,
                }
            )
    return warnings


class HostedGovernanceStore:
    """Coordinate retention, backup expiry, and resumable erasure."""

    def __init__(
        self,
        path: str | Path,
        content: GovernanceContentStore,
        identity: GovernanceIdentityStore,
        policy: HostedGovernancePolicy,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.content = content
        self.identity = identity
        self.policy = policy
        self.clock = clock
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS hosted_erasure_journal(
              operation_id TEXT PRIMARY KEY,
              idempotency_key TEXT NOT NULL UNIQUE,
              request_hash TEXT NOT NULL,
              tenant_id TEXT NOT NULL,
              erasure_kind TEXT NOT NULL,
              target_id TEXT,
              content_done INTEGER NOT NULL DEFAULT 0,
              identity_done INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hosted_erasure_receipts(
              operation_id TEXT PRIMARY KEY,
              idempotency_key_hash TEXT NOT NULL UNIQUE,
              request_hash TEXT NOT NULL,
              erasure_kind TEXT NOT NULL,
              subject_hash TEXT NOT NULL,
              completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hosted_backup_registry(
              tenant_id TEXT NOT NULL,
              backup_id TEXT NOT NULL,
              object_key TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              deleted_at TEXT,
              PRIMARY KEY(tenant_id, backup_id)
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def policy_summary(self) -> dict[str, object]:
        return {
            "collection_purpose": self.policy.collection_purpose,
            "event_retention_days": self.policy.event_retention_days,
            "backup_retention_days": self.policy.backup_retention_days,
            "storage_region": self.policy.storage_region,
            "storage_class": self.policy.storage_class,
            "incident_contact": self.policy.incident_contact,
            "incident_runbook": self.policy.incident_runbook,
            "sensitive_data_detection": "best_effort_warning_only",
        }

    def record_event(
        self,
        tenant_id: str,
        project_id: str,
        kind: str,
        content: str,
    ) -> dict[str, object]:
        event = self.content.record_event(tenant_id, project_id, kind, content)
        return {
            "event": event,
            "sensitive_data_warnings": sensitive_data_warnings(content),
            "recorded": True,
        }

    def export_project(
        self, tenant_id: str, project_id: str
    ) -> dict[str, object]:
        return self.content.export_project(tenant_id, project_id)

    def export_actor(self, tenant_id: str, actor_id: str) -> dict[str, object]:
        return self.identity.export_actor(tenant_id, actor_id)

    def apply_event_retention(self, tenant_id: str) -> dict[str, object]:
        cutoff = self.clock() - timedelta(
            days=self.policy.event_retention_days
        )
        return {
            "tenant_id": tenant_id,
            "cutoff": cutoff.isoformat(),
            "deleted_events": self.content.purge_events_before(
                tenant_id, cutoff
            ),
        }

    def register_backup(
        self,
        tenant_id: str,
        backup_id: str,
        object_key: str,
        created_at: datetime | None = None,
    ) -> dict[str, object]:
        created = created_at or self.clock()
        expires = created + timedelta(days=self.policy.backup_retention_days)
        self.connection.execute(
            """
            INSERT INTO hosted_backup_registry(
              tenant_id, backup_id, object_key, created_at, expires_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                backup_id,
                object_key,
                created.isoformat(),
                expires.isoformat(),
            ),
        )
        return {
            "tenant_id": tenant_id,
            "backup_id": backup_id,
            "expires_at": expires.isoformat(),
        }

    def expired_backups(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT tenant_id, backup_id, object_key, expires_at
            FROM hosted_backup_registry
            WHERE deleted_at IS NULL AND expires_at <= ?
            ORDER BY tenant_id, backup_id
            """,
            (self.clock().isoformat(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_backup_deleted(self, tenant_id: str, backup_id: str) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE hosted_backup_registry SET deleted_at = ?
            WHERE tenant_id = ? AND backup_id = ? AND deleted_at IS NULL
            """,
            (self.clock().isoformat(), tenant_id, backup_id),
        )
        return cursor.rowcount == 1

    def request_erasure(
        self,
        tenant_id: str,
        erasure_kind: str,
        target_id: str | None,
        idempotency_key: str,
    ) -> dict[str, object]:
        if erasure_kind not in {"actor", "project", "tenant"}:
            raise ValueError("erasure kind must be actor, project, or tenant")
        if erasure_kind != "tenant" and not target_id:
            raise ValueError("target_id is required")
        if erasure_kind == "tenant" and target_id is not None:
            raise ValueError("tenant erasure does not accept target_id")
        if not idempotency_key:
            raise ValueError("idempotency key is required")
        request_hash = self._digest(
            {"tenant_id": tenant_id, "kind": erasure_kind, "target": target_id}
        )
        idempotency_key_hash = self._digest(idempotency_key)
        receipt = self.connection.execute(
            """
            SELECT operation_id, request_hash FROM hosted_erasure_receipts
            WHERE idempotency_key_hash = ?
            """,
            (idempotency_key_hash,),
        ).fetchone()
        if receipt:
            if receipt["request_hash"] != request_hash:
                raise ValueError("idempotency key reused for another erasure")
            return {
                "operation_id": receipt["operation_id"],
                "status": "complete",
            }
        row = self.connection.execute(
            """
            SELECT operation_id, request_hash FROM hosted_erasure_journal
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row:
            if row["request_hash"] != request_hash:
                raise ValueError("idempotency key reused for another erasure")
            return {"operation_id": row["operation_id"], "status": "pending"}
        operation_id = str(uuid.uuid4())
        now = self.clock().isoformat()
        self.connection.execute(
            """
            INSERT INTO hosted_erasure_journal(
              operation_id, idempotency_key, request_hash, tenant_id,
              erasure_kind, target_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                idempotency_key,
                request_hash,
                tenant_id,
                erasure_kind,
                target_id,
                now,
                now,
            ),
        )
        return {"operation_id": operation_id, "status": "pending"}

    def execute_erasure(self, operation_id: str) -> dict[str, object]:
        row = self.connection.execute(
            """
            SELECT * FROM hosted_erasure_journal WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            receipt = self.connection.execute(
                """
                SELECT operation_id FROM hosted_erasure_receipts
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if receipt:
                return {"operation_id": operation_id, "status": "complete"}
            raise KeyError("unknown erasure operation")
        tenant_id = row["tenant_id"]
        kind = row["erasure_kind"]
        target = row["target_id"]
        if not row["content_done"]:
            if kind == "project":
                self.content.erase_project(tenant_id, target)
            elif kind == "tenant":
                self.content.erase_tenant(tenant_id)
            self._mark(operation_id, "content_done")
        if not row["identity_done"]:
            if kind == "actor":
                self.identity.erase_actor(tenant_id, target)
            elif kind == "project":
                self.identity.erase_project(tenant_id, target)
            else:
                self.identity.erase_tenant(tenant_id)
            self._mark(operation_id, "identity_done")
        if kind == "tenant":
            pending_backups = self.connection.execute(
                """
                SELECT backup_id, object_key FROM hosted_backup_registry
                WHERE tenant_id = ? AND deleted_at IS NULL
                ORDER BY backup_id
                """,
                (tenant_id,),
            ).fetchall()
            if pending_backups:
                return {
                    "operation_id": operation_id,
                    "status": "awaiting_backup_deletion",
                    "backups": [dict(item) for item in pending_backups],
                }
            self.connection.execute(
                "DELETE FROM hosted_backup_registry WHERE tenant_id = ?",
                (tenant_id,),
            )
        self._complete_erasure(row)
        return {"operation_id": operation_id, "status": "complete"}

    def _mark(self, operation_id: str, column: str) -> None:
        if column not in {"content_done", "identity_done"}:
            raise ValueError("invalid erasure stage")
        self.connection.execute(
            f"UPDATE hosted_erasure_journal SET {column} = 1, updated_at = ? "
            "WHERE operation_id = ?",
            (self.clock().isoformat(), operation_id),
        )

    def _complete_erasure(self, row: sqlite3.Row) -> None:
        subject_hash = self._digest(
            {
                "tenant_id": row["tenant_id"],
                "kind": row["erasure_kind"],
                "target": row["target_id"],
            }
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                INSERT INTO hosted_erasure_receipts(
                  operation_id, idempotency_key_hash, request_hash,
                  erasure_kind,
                  subject_hash, completed_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    row["operation_id"],
                    self._digest(row["idempotency_key"]),
                    row["request_hash"],
                    row["erasure_kind"],
                    subject_hash,
                    self.clock().isoformat(),
                ),
            )
            self.connection.execute(
                "DELETE FROM hosted_erasure_journal WHERE operation_id = ?",
                (row["operation_id"],),
            )
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()

    @staticmethod
    def _digest(value: object) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()
