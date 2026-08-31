from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class HostedContentStore:
    """Tenant-keyed hosted content persistence prototype.

    This store is deliberately separate from the local-first
    ``MemoryStore``. Every project operation requires both tenant and
    project identifiers. The database enforces the same pairing with
    composite foreign keys.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

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
        self.connection.commit()

    def provision_tenant(self, tenant_id: str) -> None:
        self.connection.execute(
            "INSERT INTO hosted_content_tenants(tenant_id) VALUES(?)",
            (tenant_id,),
        )
        self.connection.commit()

    def provision_project(self, tenant_id: str, project_id: str) -> None:
        self.connection.execute(
            """
            INSERT INTO hosted_content_projects(tenant_id, project_id)
            VALUES(?, ?)
            """,
            (tenant_id, project_id),
        )
        self.connection.commit()

    def record_event(
        self,
        tenant_id: str,
        project_id: str,
        kind: str,
        content: str,
    ) -> dict[str, object]:
        with self.connection:
            row = self.connection.execute(
                """
                SELECT COALESCE(MAX(event_seq), 0) + 1 AS next_seq
                FROM hosted_content_events
                WHERE tenant_id = ? AND project_id = ?
                """,
                (tenant_id, project_id),
            ).fetchone()
            event_seq = int(row["next_seq"])
            self.connection.execute(
                """
                INSERT INTO hosted_content_events(
                  tenant_id, project_id, event_seq, kind, content
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (tenant_id, project_id, event_seq, kind, content),
            )
        return {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "event_seq": event_seq,
            "kind": kind,
            "content": content,
        }

    def search(
        self, tenant_id: str, project_id: str, query: str
    ) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT event_seq, kind, content
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
            SELECT event_seq, kind, content
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
            SELECT event_seq, kind, content
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
        }
