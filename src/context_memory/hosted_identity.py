from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .hosted_authorization import HostedSession


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HostedIdentityStore:
    """Persist identity state for a future hosted service."""

    def __init__(
        self,
        db_path: str | Path,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hosted_tenants(
              tenant_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS hosted_actors(
              tenant_id TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id, actor_id),
              FOREIGN KEY(tenant_id) REFERENCES hosted_tenants(tenant_id)
            );
            CREATE TABLE IF NOT EXISTS hosted_projects(
              tenant_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id, project_id),
              FOREIGN KEY(tenant_id) REFERENCES hosted_tenants(tenant_id)
            );
            CREATE TABLE IF NOT EXISTS hosted_sessions(
              tenant_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              revoked_at TEXT,
              PRIMARY KEY(tenant_id, session_id),
              FOREIGN KEY(tenant_id, actor_id)
                REFERENCES hosted_actors(tenant_id, actor_id)
            );
            CREATE TABLE IF NOT EXISTS hosted_role_assignments(
              tenant_id TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              role TEXT NOT NULL,
              PRIMARY KEY(tenant_id, actor_id, role),
              FOREIGN KEY(tenant_id, actor_id)
                REFERENCES hosted_actors(tenant_id, actor_id)
            );
            CREATE TABLE IF NOT EXISTS hosted_project_grants(
              tenant_id TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id, actor_id, project_id),
              FOREIGN KEY(tenant_id, actor_id)
                REFERENCES hosted_actors(tenant_id, actor_id),
              FOREIGN KEY(tenant_id, project_id)
                REFERENCES hosted_projects(tenant_id, project_id)
            );
            CREATE TABLE IF NOT EXISTS hosted_security_audit(
              audit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id TEXT,
              actor_id TEXT,
              session_id TEXT,
              action TEXT NOT NULL,
              decision TEXT NOT NULL,
              reason TEXT NOT NULL,
              request_id TEXT NOT NULL,
              target_type TEXT NOT NULL,
              target_id TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS hosted_security_audit_request
              ON hosted_security_audit(request_id, audit_seq);
            """
        )

    def close(self) -> None:
        self.conn.close()

    def provision_tenant(self, tenant_id: str) -> None:
        self.conn.execute(
            "INSERT INTO hosted_tenants(tenant_id) VALUES(?)",
            (tenant_id,),
        )

    def provision_actor(self, tenant_id: str, actor_id: str) -> None:
        self.conn.execute(
            "INSERT INTO hosted_actors(tenant_id, actor_id) VALUES(?, ?)",
            (tenant_id, actor_id),
        )

    def provision_project(self, tenant_id: str, project_id: str) -> None:
        self.conn.execute(
            "INSERT INTO hosted_projects(tenant_id, project_id) VALUES(?, ?)",
            (tenant_id, project_id),
        )

    def assign_role(self, tenant_id: str, actor_id: str, role: str) -> None:
        self.conn.execute(
            """
            INSERT INTO hosted_role_assignments(tenant_id, actor_id, role)
            VALUES(?, ?, ?)
            """,
            (tenant_id, actor_id, role),
        )

    def grant_project(
        self, tenant_id: str, actor_id: str, project_id: str
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO hosted_project_grants(
              tenant_id, actor_id, project_id
            ) VALUES(?, ?, ?)
            """,
            (tenant_id, actor_id, project_id),
        )

    def revoke_project_grant(
        self, tenant_id: str, actor_id: str, project_id: str
    ) -> bool:
        cursor = self.conn.execute(
            """
            DELETE FROM hosted_project_grants
            WHERE tenant_id = ? AND actor_id = ? AND project_id = ?
            """,
            (tenant_id, actor_id, project_id),
        )
        return cursor.rowcount == 1

    def revoke_role(self, tenant_id: str, actor_id: str, role: str) -> bool:
        cursor = self.conn.execute(
            """
            DELETE FROM hosted_role_assignments
            WHERE tenant_id = ? AND actor_id = ? AND role = ?
            """,
            (tenant_id, actor_id, role),
        )
        return cursor.rowcount == 1

    def issue_session(
        self,
        tenant_id: str,
        session_id: str,
        actor_id: str,
        expires_at: datetime,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO hosted_sessions(
              tenant_id, session_id, actor_id, expires_at
            ) VALUES(?, ?, ?, ?)
            """,
            (tenant_id, session_id, actor_id, expires_at.isoformat()),
        )

    def revoke_session(self, tenant_id: str, session_id: str) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE hosted_sessions SET revoked_at = ?
            WHERE tenant_id = ? AND session_id = ? AND revoked_at IS NULL
            """,
            (self.clock().isoformat(), tenant_id, session_id),
        )
        return cursor.rowcount == 1

    def delete_actor(self, tenant_id: str, actor_id: str) -> bool:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """
                DELETE FROM hosted_project_grants
                WHERE tenant_id = ? AND actor_id = ?
                """,
                (tenant_id, actor_id),
            )
            self.conn.execute(
                """
                DELETE FROM hosted_role_assignments
                WHERE tenant_id = ? AND actor_id = ?
                """,
                (tenant_id, actor_id),
            )
            self.conn.execute(
                """
                DELETE FROM hosted_sessions
                WHERE tenant_id = ? AND actor_id = ?
                """,
                (tenant_id, actor_id),
            )
            cursor = self.conn.execute(
                """
                DELETE FROM hosted_actors
                WHERE tenant_id = ? AND actor_id = ?
                """,
                (tenant_id, actor_id),
            )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        return cursor.rowcount == 1

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
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO hosted_security_audit(
              tenant_id, actor_id, session_id, action, decision, reason,
              request_id, target_type, target_id, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id,
                actor_id,
                session_id,
                action,
                decision,
                reason,
                request_id,
                target_type,
                target_id,
                self.clock().isoformat(),
            ),
        )

    def list_security_audit(self, request_id: str) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT audit_seq, tenant_id, actor_id, session_id, action,
                   decision, reason, request_id, target_type, target_id,
                   created_at
            FROM hosted_security_audit
            WHERE request_id = ?
            ORDER BY audit_seq
            """,
            (request_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def load_session(
        self, tenant_id: str, session_id: str
    ) -> HostedSession | None:
        row = self.conn.execute(
            """
            SELECT actor_id, expires_at, revoked_at
            FROM hosted_sessions
            WHERE tenant_id = ? AND session_id = ?
            """,
            (tenant_id, session_id),
        ).fetchone()
        if row is None:
            return None
        roles = self.conn.execute(
            """
            SELECT role FROM hosted_role_assignments
            WHERE tenant_id = ? AND actor_id = ?
            """,
            (tenant_id, row["actor_id"]),
        )
        grants = self.conn.execute(
            """
            SELECT project_id FROM hosted_project_grants
            WHERE tenant_id = ? AND actor_id = ?
            """,
            (tenant_id, row["actor_id"]),
        )
        expires_at = datetime.fromisoformat(row["expires_at"])
        active = row["revoked_at"] is None and expires_at > self.clock()
        return HostedSession(
            actor_id=row["actor_id"],
            tenant_id=tenant_id,
            session_id=session_id,
            roles=frozenset(item["role"] for item in roles),
            project_ids=frozenset(item["project_id"] for item in grants),
            active=active,
        )
