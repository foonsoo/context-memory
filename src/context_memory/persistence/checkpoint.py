"""Checkpoint state observation queries."""

import sqlite3
from typing import Any


class CheckpointRepository:
    """Observe durable state used by checkpoint policy evaluation."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def session_start(self, session_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT project_id,started_at FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def latest(self, project_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM events WHERE project_id=? AND kind='checkpoint'"
            " ORDER BY event_seq DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None

    def event_cursor(self, project_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT next_seq-1 AS value FROM project_event_cursors WHERE"
            " project_id=?",
            (project_id,),
        ).fetchone()
        return row["value"] if row else None

    def durable_events_after(self, project_id: str, event_seq: int) -> int:
        return self.connection.execute(
            "SELECT count(*) FROM events WHERE project_id=? AND event_seq>?"
            " AND kind<>'checkpoint'",
            (project_id, event_seq),
        ).fetchone()[0]
