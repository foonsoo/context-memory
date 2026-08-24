"""Memory identity and retrieval persistence queries."""

import sqlite3
from typing import Any


class MemoryRepository:
    """Own durable memory identity reads behind the store facade."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, memory_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_proposed(self, memory_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM memories WHERE id=? AND status='proposed'",
            (memory_id,),
        ).fetchone()
        return dict(row) if row else None
