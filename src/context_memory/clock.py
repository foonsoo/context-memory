"""Shared UTC clock primitives."""

from datetime import datetime, timezone


def utc_datetime(datetime_type: type[datetime] = datetime) -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime_type.now(timezone.utc)


def utc_now(datetime_type: type[datetime] = datetime) -> str:
    """Return the current UTC timestamp in the persisted ISO format."""
    return utc_datetime(datetime_type).isoformat()
