"""Maintenance policy and audit persistence queries."""

import sqlite3
from typing import Any


class MaintenanceRepository:
    """Own maintenance policy and audit-chain reads."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get_policy(self, project_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM project_policies WHERE project_id=?",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None

    def audit_checkpoints(self, project_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM audit_checkpoints WHERE project_id=? ORDER BY"
                " through_seq,id",
                (project_id,),
            )
        ]

    def audit_entries(self, project_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM audit_log WHERE project_id=? ORDER BY seq",
                (project_id,),
            )
        ]
