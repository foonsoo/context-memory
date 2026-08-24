"""Project and immutable-evidence persistence queries."""

import sqlite3
from typing import Any


class ProjectEvidenceRepository:
    """Own bounded project, alias, event, and audit read queries."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

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
