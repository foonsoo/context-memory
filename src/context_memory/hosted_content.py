from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HostedQuotaPolicy:
    max_projects_per_tenant: int = 100
    max_events_per_project: int = 100_000
    max_event_bytes: int = 65_536
    max_tenant_bytes: int = 104_857_600

    def __post_init__(self) -> None:
        values = (
            self.max_projects_per_tenant,
            self.max_events_per_project,
            self.max_event_bytes,
            self.max_tenant_bytes,
        )
        if any(value < 1 for value in values):
            raise ValueError("quota values must be positive")


class HostedQuotaExceededError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__("hosted content quota exceeded")
        self.reason = reason


class HostedContentStore:
    """Tenant-keyed hosted content persistence prototype.

    This store is deliberately separate from the local-first
    ``MemoryStore``. Every project operation requires both tenant and
    project identifiers. The database enforces the same pairing with
    composite foreign keys.
    """

    def __init__(
        self,
        path: str | Path,
        quota: HostedQuotaPolicy | None = None,
        clock: Callable[[], datetime] = _utc_now,
        *,
        wal_autocheckpoint_pages: int = 1000,
        migrate: bool = True,
    ) -> None:
        if wal_autocheckpoint_pages < 1:
            raise ValueError("WAL autocheckpoint pages must be positive")
        self.path = Path(path)
        self.quota = quota or HostedQuotaPolicy()
        self.clock = clock
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.execute(
            f"PRAGMA wal_autocheckpoint = {wal_autocheckpoint_pages}"
        )
        self.connection.execute("PRAGMA foreign_keys = ON")
        if migrate:
            try:
                self._migrate()
            except Exception:
                self.connection.close()
                raise

    def close(self) -> None:
        self.connection.close()

    def checkpoint_wal(self, mode: str = "PASSIVE") -> dict[str, int]:
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError("unsupported WAL checkpoint mode")
        row = self.connection.execute(
            f"PRAGMA wal_checkpoint({mode})"
        ).fetchone()
        return {
            "busy": int(row[0]),
            "log_pages": int(row[1]),
            "checkpointed_pages": int(row[2]),
        }

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS hosted_content_tenants(
              tenant_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS hosted_content_projects(
              tenant_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id, project_id),
              FOREIGN KEY(tenant_id)
                REFERENCES hosted_content_tenants(tenant_id)
            );
            CREATE TABLE IF NOT EXISTS hosted_content_events(
              tenant_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              event_seq INTEGER NOT NULL,
              kind TEXT NOT NULL,
              content TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(tenant_id, project_id, event_seq),
              FOREIGN KEY(tenant_id, project_id)
                REFERENCES hosted_content_projects(tenant_id, project_id)
            );
            CREATE INDEX IF NOT EXISTS hosted_content_event_lookup
              ON hosted_content_events(
                tenant_id, project_id, event_seq
              );
            """
        )
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(hosted_content_events)"
            )
        }
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if "created_at" not in columns:
                self.connection.execute(
                    """
                    ALTER TABLE hosted_content_events
                    ADD COLUMN created_at TEXT
                    """
                )
                self.connection.execute(
                    "UPDATE hosted_content_events SET created_at = ?",
                    (self.clock().isoformat(),),
                )
            self.connection.execute("PRAGMA user_version = 2")
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()

    def provision_tenant(self, tenant_id: str) -> None:
        self.connection.execute(
            "INSERT INTO hosted_content_tenants(tenant_id) VALUES(?)",
            (tenant_id,),
        )
        self.connection.commit()

    def provision_project(self, tenant_id: str, project_id: str) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS project_count
                FROM hosted_content_projects
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
            if int(row["project_count"]) >= self.quota.max_projects_per_tenant:
                raise HostedQuotaExceededError("tenant_project_limit")
            self.connection.execute(
                """
                INSERT INTO hosted_content_projects(tenant_id, project_id)
                VALUES(?, ?)
                """,
                (tenant_id, project_id),
            )
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()

    def record_event(
        self,
        tenant_id: str,
        project_id: str,
        kind: str,
        content: str,
    ) -> dict[str, object]:
        content_bytes = len(content.encode("utf-8"))
        created_at = self.clock().isoformat()
        if content_bytes > self.quota.max_event_bytes:
            raise HostedQuotaExceededError("event_byte_limit")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT COALESCE(MAX(event_seq), 0) + 1 AS next_seq,
                       COUNT(*) AS event_count
                FROM hosted_content_events
                WHERE tenant_id = ? AND project_id = ?
                """,
                (tenant_id, project_id),
            ).fetchone()
            if int(row["event_count"]) >= self.quota.max_events_per_project:
                raise HostedQuotaExceededError("project_event_limit")
            usage = self.connection.execute(
                """
                SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0)
                         AS byte_count
                FROM hosted_content_events
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
            if (
                int(usage["byte_count"]) + content_bytes
                > self.quota.max_tenant_bytes
            ):
                raise HostedQuotaExceededError("tenant_byte_limit")
            event_seq = int(row["next_seq"])
            self.connection.execute(
                """
                INSERT INTO hosted_content_events(
                  tenant_id, project_id, event_seq, kind, content, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    project_id,
                    event_seq,
                    kind,
                    content,
                    created_at,
                ),
            )
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        return {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "event_seq": event_seq,
            "kind": kind,
            "content": content,
            "created_at": created_at,
        }

    def search(
        self, tenant_id: str, project_id: str, query: str
    ) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT event_seq, kind, content, created_at
            FROM hosted_content_events
            WHERE tenant_id = ? AND project_id = ?
              AND instr(lower(content), lower(?)) > 0
            ORDER BY event_seq DESC
            LIMIT 100
            """,
            (tenant_id, project_id, query),
        ).fetchall()
        return [self._event(tenant_id, project_id, row) for row in rows]

    def export_project(
        self, tenant_id: str, project_id: str
    ) -> dict[str, object]:
        project = self.connection.execute(
            """
            SELECT project_id
            FROM hosted_content_projects
            WHERE tenant_id = ? AND project_id = ?
            """,
            (tenant_id, project_id),
        ).fetchone()
        if project is None:
            return {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "events": [],
            }
        rows = self.connection.execute(
            """
            SELECT event_seq, kind, content, created_at
            FROM hosted_content_events
            WHERE tenant_id = ? AND project_id = ?
            ORDER BY event_seq
            """,
            (tenant_id, project_id),
        ).fetchall()
        return {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "events": [
                self._event(tenant_id, project_id, row) for row in rows
            ],
        }

    def poll_events(
        self,
        tenant_id: str,
        project_id: str,
        cursor: int | None,
    ) -> dict[str, object]:
        effective_cursor = cursor or 0
        rows = self.connection.execute(
            """
            SELECT event_seq, kind, content, created_at
            FROM hosted_content_events
            WHERE tenant_id = ? AND project_id = ? AND event_seq > ?
            ORDER BY event_seq
            LIMIT 100
            """,
            (tenant_id, project_id, effective_cursor),
        ).fetchall()
        events = [self._event(tenant_id, project_id, row) for row in rows]
        return {
            "events": events,
            "next_cursor": (
                int(events[-1]["event_seq"]) if events else effective_cursor
            ),
        }

    def backup_tenant(self, tenant_id: str) -> bytes:
        projects = self.connection.execute(
            """
            SELECT project_id
            FROM hosted_content_projects
            WHERE tenant_id = ?
            ORDER BY project_id
            """,
            (tenant_id,),
        ).fetchall()
        payload = {
            "tenant_id": tenant_id,
            "projects": [
                self.export_project(tenant_id, row["project_id"])
                for row in projects
            ],
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    def restore_tenant(self, payload: bytes) -> dict[str, object]:
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("tenant backup is malformed") from exc
        tenant_id, projects = self._validate_tenant_backup(value)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT INTO hosted_content_tenants(tenant_id) VALUES(?)",
                (tenant_id,),
            )
            event_count = 0
            for project in projects:
                project_id = project["project_id"]
                self.connection.execute(
                    """
                    INSERT INTO hosted_content_projects(tenant_id, project_id)
                    VALUES(?, ?)
                    """,
                    (tenant_id, project_id),
                )
                for event in project["events"]:
                    self.connection.execute(
                        """
                        INSERT INTO hosted_content_events(
                          tenant_id, project_id, event_seq, kind, content,
                          created_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tenant_id,
                            project_id,
                            event["event_seq"],
                            event["kind"],
                            event["content"],
                            event["created_at"],
                        ),
                    )
                    event_count += 1
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        return {
            "tenant_id": tenant_id,
            "projects": len(projects),
            "events": event_count,
        }

    def _validate_tenant_backup(
        self, value: object
    ) -> tuple[str, list[dict[str, object]]]:
        if not isinstance(value, dict) or set(value) != {
            "tenant_id",
            "projects",
        }:
            raise ValueError("tenant backup has invalid fields")
        tenant_id = value["tenant_id"]
        projects = value["projects"]
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("tenant backup has invalid tenant")
        if not isinstance(projects, list):
            raise ValueError("tenant backup projects must be a list")
        if len(projects) > self.quota.max_projects_per_tenant:
            raise HostedQuotaExceededError("tenant_project_limit")
        validated: list[dict[str, object]] = []
        project_ids: set[str] = set()
        tenant_bytes = 0
        for project in projects:
            if not isinstance(project, dict) or set(project) != {
                "tenant_id",
                "project_id",
                "events",
            }:
                raise ValueError("tenant backup project has invalid fields")
            project_id = project["project_id"]
            events = project["events"]
            if (
                project["tenant_id"] != tenant_id
                or not isinstance(project_id, str)
                or not project_id
                or project_id in project_ids
                or not isinstance(events, list)
            ):
                raise ValueError("tenant backup project is invalid")
            if len(events) > self.quota.max_events_per_project:
                raise HostedQuotaExceededError("project_event_limit")
            project_ids.add(project_id)
            sequences: set[int] = set()
            checked_events = []
            for event in events:
                checked, content_bytes = self._validate_backup_event(
                    event, tenant_id, project_id
                )
                if checked["event_seq"] in sequences:
                    raise ValueError(
                        "tenant backup event sequence is duplicated"
                    )
                sequences.add(checked["event_seq"])
                tenant_bytes += content_bytes
                checked_events.append(checked)
            validated.append(
                {"project_id": project_id, "events": checked_events}
            )
        if tenant_bytes > self.quota.max_tenant_bytes:
            raise HostedQuotaExceededError("tenant_byte_limit")
        return tenant_id, validated

    def _validate_backup_event(
        self, event: object, tenant_id: str, project_id: str
    ) -> tuple[dict[str, object], int]:
        expected = {
            "tenant_id",
            "project_id",
            "event_seq",
            "kind",
            "content",
            "created_at",
        }
        if not isinstance(event, dict) or set(event) != expected:
            raise ValueError("tenant backup event has invalid fields")
        if (
            event["tenant_id"] != tenant_id
            or event["project_id"] != project_id
        ):
            raise ValueError(
                "tenant backup event crosses its project boundary"
            )
        sequence = event["event_seq"]
        kind = event["kind"]
        content = event["content"]
        created_at = event["created_at"]
        if not isinstance(sequence, int) or sequence < 1:
            raise ValueError("tenant backup event sequence is invalid")
        if not isinstance(kind, str) or not kind:
            raise ValueError("tenant backup event kind is invalid")
        if not isinstance(content, str) or not isinstance(created_at, str):
            raise ValueError("tenant backup event content is invalid")
        try:
            datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise ValueError("tenant backup event time is invalid") from exc
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > self.quota.max_event_bytes:
            raise HostedQuotaExceededError("event_byte_limit")
        return dict(event), content_bytes

    def purge_events_before(self, tenant_id: str, cutoff: datetime) -> int:
        cursor = self.connection.execute(
            """
            DELETE FROM hosted_content_events
            WHERE tenant_id = ? AND created_at < ?
            """,
            (tenant_id, cutoff.isoformat()),
        )
        self.connection.commit()
        return cursor.rowcount

    def erase_project(self, tenant_id: str, project_id: str) -> bool:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                DELETE FROM hosted_content_events
                WHERE tenant_id = ? AND project_id = ?
                """,
                (tenant_id, project_id),
            )
            cursor = self.connection.execute(
                """
                DELETE FROM hosted_content_projects
                WHERE tenant_id = ? AND project_id = ?
                """,
                (tenant_id, project_id),
            )
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        return cursor.rowcount == 1

    def erase_tenant(self, tenant_id: str) -> bool:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "DELETE FROM hosted_content_events WHERE tenant_id = ?",
                (tenant_id,),
            )
            self.connection.execute(
                "DELETE FROM hosted_content_projects WHERE tenant_id = ?",
                (tenant_id,),
            )
            cursor = self.connection.execute(
                "DELETE FROM hosted_content_tenants WHERE tenant_id = ?",
                (tenant_id,),
            )
        except Exception:
            self.connection.rollback()
            raise
        self.connection.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _event(
        tenant_id: str, project_id: str, row: sqlite3.Row
    ) -> dict[str, object]:
        return {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "event_seq": int(row["event_seq"]),
            "kind": row["kind"],
            "content": row["content"],
            "created_at": row["created_at"],
        }


class RequestScopedHostedContentStore:
    """Use one SQLite connection per threaded hosted operation."""

    def __init__(
        self,
        path: str | Path,
        quota: HostedQuotaPolicy | None = None,
        clock: Callable[[], datetime] = _utc_now,
        *,
        wal_autocheckpoint_pages: int = 1000,
    ) -> None:
        self.path = Path(path)
        self.quota = quota or HostedQuotaPolicy()
        self.clock = clock
        self.wal_autocheckpoint_pages = wal_autocheckpoint_pages
        initial = HostedContentStore(
            self.path,
            self.quota,
            self.clock,
            wal_autocheckpoint_pages=wal_autocheckpoint_pages,
        )
        initial.close()

    def record_event(
        self, tenant_id: str, project_id: str, kind: str, content: str
    ) -> dict[str, object]:
        return self._call("record_event", tenant_id, project_id, kind, content)

    def search(
        self, tenant_id: str, project_id: str, query: str
    ) -> list[dict[str, object]]:
        return self._call("search", tenant_id, project_id, query)

    def export_project(
        self, tenant_id: str, project_id: str
    ) -> dict[str, object]:
        return self._call("export_project", tenant_id, project_id)

    def poll_events(
        self, tenant_id: str, project_id: str, cursor: int | None
    ) -> dict[str, object]:
        return self._call("poll_events", tenant_id, project_id, cursor)

    def backup_tenant(self, tenant_id: str) -> bytes:
        return self._call("backup_tenant", tenant_id)

    def restore_tenant(self, payload: bytes) -> dict[str, object]:
        return self._call("restore_tenant", payload)

    def checkpoint_wal(self, mode: str = "PASSIVE") -> dict[str, int]:
        return self._call("checkpoint_wal", mode)

    def _call(self, method: str, *arguments):
        store = HostedContentStore(
            self.path,
            self.quota,
            self.clock,
            wal_autocheckpoint_pages=self.wal_autocheckpoint_pages,
            migrate=False,
        )
        try:
            return getattr(store, method)(*arguments)
        finally:
            store.close()
