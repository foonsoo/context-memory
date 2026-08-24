"""Small persistence primitives shared by bounded repositories."""

import sqlite3
from typing import Any


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert an optional SQLite row to a dictionary."""
    return dict(row) if row is not None else None


def row_exists(
    connection: sqlite3.Connection, query: str, args: tuple[Any, ...]
) -> bool:
    """Return whether a query produces at least one row."""
    return connection.execute(query, args).fetchone() is not None
