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
