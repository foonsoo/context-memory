"""Decision Wiki persistence queries."""

import sqlite3
from typing import Any


class WikiRepository:
    """Own page and revision identity persistence."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM wiki_pages WHERE id=?", (page_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM wiki_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        return dict(row) if row else None

    def scope_belongs_to_project(self, scope_id: str, project_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM scopes WHERE id=? AND project_id=?",
            (scope_id, project_id),
        ).fetchone()
        return row is not None

    @staticmethod
    def insert_page(
        connection: sqlite3.Connection, item: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO wiki_pages"
            " VALUES(:id,:project_id,:scope_id,:topic,:title,"
            ":manual_notes,:created_at,:updated_at)",
            item,
        )

    @staticmethod
    def update_notes(
        connection: sqlite3.Connection,
        page_id: str,
        manual_notes: str,
        updated_at: str,
    ) -> None:
        connection.execute(
            "UPDATE wiki_pages SET manual_notes=?,updated_at=? WHERE id=?",
            (manual_notes, updated_at, page_id),
        )
