"""Project and immutable-evidence persistence queries."""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from ..serialization import canonical


class ProjectEvidenceRepository:
    """Own bounded project, alias, event, and audit read queries."""

    def __init__(
        self,
        owner: Any,
        now: Callable[[], str] | None = None,
        current_datetime: Callable[[], datetime] | None = None,
    ):
        self.store = None if isinstance(owner, sqlite3.Connection) else owner
        self.connection: sqlite3.Connection = (
            owner if self.store is None else owner.conn
        )
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat())
        self.current_datetime = current_datetime or (
            lambda: datetime.now(timezone.utc)
        )

    def project_exists(self, project_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        return row is not None

    @staticmethod
    def insert_scope(
        connection: sqlite3.Connection, item: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO scopes"
            " VALUES(:id,:project_id,:name,:path,:created_at)",
            item,
        )

    def find_session(
        self, project_id: str, client: str, external_id: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM sessions WHERE project_id=? AND client=? AND"
            " external_id=?",
            (project_id, client, external_id),
        ).fetchone()
        return dict(row) if row else None

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def insert_session(
        connection: sqlite3.Connection, item: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO sessions"
            " VALUES(:id,:project_id,:scope_id,:client,:external_id,"
            ":started_at,:ended_at,:metadata_json)",
            item,
        )

    @staticmethod
    def set_session_ended(
        connection: sqlite3.Connection, session_id: str, ended_at: str
    ) -> None:
        connection.execute(
            "UPDATE sessions SET ended_at=? WHERE id=?",
            (ended_at, session_id),
        )

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM projects ORDER BY slug"
            )
        ]

    def list_project_aliases(self, project_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM project_aliases WHERE project_id=? ORDER BY"
                " kind,normalized",
                (project_id,),
            )
        ]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM events WHERE id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def message_ttl_seconds(
        connection: sqlite3.Connection, project_id: str
    ) -> int | None:
        row = connection.execute(
            "SELECT message_ttl_seconds FROM project_policies WHERE"
            " project_id=?",
            (project_id,),
        ).fetchone()
        return row["message_ttl_seconds"] if row else None

    @staticmethod
    def allocate_event_sequence(
        connection: sqlite3.Connection, project_id: str
    ) -> int | None:
        row = connection.execute(
            "UPDATE project_event_cursors SET next_seq=next_seq+1 WHERE"
            " project_id=? RETURNING next_seq-1",
            (project_id,),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def insert_event(
        connection: sqlite3.Connection, item: dict[str, Any]
    ) -> None:
        connection.execute(
            """INSERT INTO events(id,project_id,scope_id,session_id,
          kind,content,source_uri,metadata_json,content_hash,created_at,
          event_seq) VALUES(:id,:project_id,:scope_id,:session_id,
          :kind,:content,:source_uri,:metadata_json,:content_hash,
          :created_at,:event_seq)""",
            item,
        )

    def audit_entries(
        self, entity_type: str, entity_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM audit_log WHERE entity_type=? AND entity_id=?"
                " ORDER BY seq",
                (entity_type, entity_id),
            )
        ]

    def read_events_since(
        self,
        project_id: str,
        cursor: int = 0,
        kinds: list[str] | None = None,
        scope_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read project events after a cursor without ranking them."""
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        if kinds is not None and (
            not kinds or any(not kind.strip() for kind in kinds)
        ):
            raise ValueError("kinds must contain non-empty values")
        state = self.store._row(
            "SELECT next_seq-1 AS snapshot_cursor FROM project_event_cursors"
            " WHERE project_id=?",
            (project_id,),
        )
        if not state:
            raise KeyError("project not found")
        snapshot = state["snapshot_cursor"]
        sql = (
            "SELECT * FROM events WHERE project_id=? AND event_seq>? AND"
            " event_seq<=?"
        )
        args: list[Any] = [project_id, cursor, snapshot]
        if kinds:
            unique_kinds = list(dict.fromkeys(kinds))
            sql += " AND kind IN (" + ",".join("?" for _ in unique_kinds) + ")"
            args.extend(unique_kinds)
        if scope_id:
            sql += " AND (scope_id=? OR scope_id IS NULL)"
            args.append(scope_id)
        sql += " ORDER BY event_seq LIMIT ?"
        args.append(limit + 1)
        rows = [dict(row) for row in self.store.conn.execute(sql, args)]
        has_more = len(rows) > limit
        rows = rows[:limit]
        page_cursor = rows[-1]["event_seq"] if has_more and rows else snapshot
        visible = []
        current = self.current_datetime()
        for row in rows:
            row["metadata"] = json.loads(row.pop("metadata_json"))
            expires_at = (
                row["metadata"].get("expires_at")
                if row["kind"] == "message"
                else None
            )
            if expires_at:
                try:
                    expired = (
                        datetime.fromisoformat(
                            expires_at.replace("Z", "+00:00")
                        )
                        <= current
                    )
                except (TypeError, ValueError):
                    expired = False
                if expired:
                    continue
            visible.append(row)
        rows = visible
        next_cursor = page_cursor if has_more else snapshot
        return {
            "project_id": project_id,
            "cursor": cursor,
            "snapshot_cursor": snapshot,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "events": rows,
        }

    @staticmethod
    def _receipt_stream(
        kinds: list[str] | None, scope_id: str | None
    ) -> tuple[str, str, list[str] | None]:
        normalized = sorted(set(kinds)) if kinds else None
        return scope_id or "", canonical(normalized or []), normalized

    def poll_events(
        self,
        project_id: str,
        consumer_id: str,
        kinds: list[str] | None = None,
        scope_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read a consumer receipt without acknowledging delivery."""
        consumer_id = consumer_id.strip()
        if not consumer_id:
            raise ValueError("consumer_id cannot be empty")
        scope_key, kinds_json, normalized = self._receipt_stream(
            kinds, scope_id
        )
        receipt = self.store._row(
            """SELECT * FROM event_receipts
          WHERE project_id=? AND consumer_id=?
          AND scope_key=? AND kinds_json=?""",
            (project_id, consumer_id, scope_key, kinds_json),
        )
        cursor = receipt["acknowledged_cursor"] if receipt else 0
        result = self.read_events_since(
            project_id, cursor, normalized, scope_id, limit
        )
        delivered = max(cursor, result["next_cursor"])
        ts = self.now()
        with self.store.tx() as cx:
            cx.execute(
                """INSERT INTO event_receipts(project_id,consumer_id,
              scope_key,kinds_json,acknowledged_cursor,delivered_cursor,
              created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(project_id,consumer_id,scope_key,kinds_json)
              DO UPDATE SET delivered_cursor=max(
                event_receipts.delivered_cursor,excluded.delivered_cursor),
                updated_at=excluded.updated_at""",
                (
                    project_id,
                    consumer_id,
                    scope_key,
                    kinds_json,
                    cursor,
                    delivered,
                    ts,
                    ts,
                ),
            )
        result.update(
            {
                "consumer_id": consumer_id,
                "acknowledged_cursor": cursor,
                "delivered_cursor": delivered,
            }
        )
        return result

    def acknowledge_events(
        self,
        project_id: str,
        consumer_id: str,
        cursor: int,
        kinds: list[str] | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        """Acknowledge a cursor delivered for this exact stream."""
        consumer_id = consumer_id.strip()
        if not consumer_id:
            raise ValueError("consumer_id cannot be empty")
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        scope_key, kinds_json, _ = self._receipt_stream(kinds, scope_id)
        with self.store.tx() as cx:
            row = cx.execute(
                """SELECT * FROM event_receipts
              WHERE project_id=? AND consumer_id=?
              AND scope_key=? AND kinds_json=?""",
                (project_id, consumer_id, scope_key, kinds_json),
            ).fetchone()
            if not row:
                raise KeyError(
                    "event receipt not found; poll this stream before"
                    " acknowledging"
                )
            if cursor < row["acknowledged_cursor"]:
                raise ValueError("acknowledged cursor cannot move backwards")
            if cursor > row["delivered_cursor"]:
                raise ValueError(
                    "cannot acknowledge beyond the delivered cursor"
                )
            ts = self.now()
            cx.execute(
                """UPDATE event_receipts
              SET acknowledged_cursor=?,updated_at=? WHERE project_id=?
              AND consumer_id=? AND scope_key=? AND kinds_json=?""",
                (cursor, ts, project_id, consumer_id, scope_key, kinds_json),
            )
            item = dict(
                cx.execute(
                    """SELECT * FROM event_receipts
              WHERE project_id=? AND consumer_id=?
              AND scope_key=? AND kinds_json=?""",
                    (project_id, consumer_id, scope_key, kinds_json),
                ).fetchone()
            )
            item["kinds"] = json.loads(item.pop("kinds_json"))
            item["scope_id"] = item.pop("scope_key") or None
            self.store._audit(
                cx,
                project_id,
                "event_receipt",
                f"{consumer_id}:{scope_key}:{kinds_json}",
                "acknowledged",
                item,
            )
        return item
