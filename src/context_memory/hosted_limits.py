from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Callable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True)
class HostedRateLimitPolicy:
    requests_per_window: int = 120
    window_seconds: int = 60

    def __post_init__(self) -> None:
        if self.requests_per_window < 1 or self.window_seconds < 1:
            raise ValueError("rate limit values must be positive")


@dataclass(frozen=True)
class HostedRateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class HostedRateLimiter:
    """Persist fixed-window limits by tenant, actor, and action."""

    def __init__(
        self,
        path: str | Path,
        policy: HostedRateLimitPolicy | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.policy = policy or HostedRateLimitPolicy()
        self.clock = clock
        self._lock = RLock()
        self.connection = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS hosted_rate_windows(
              tenant_id TEXT NOT NULL,
              actor_id TEXT NOT NULL,
              action TEXT NOT NULL,
              window_start INTEGER NOT NULL,
              request_count INTEGER NOT NULL,
              PRIMARY KEY(tenant_id, actor_id, action, window_start)
            )
            """
        )

    @_serialized
    def close(self) -> None:
        self.connection.close()

    @_serialized
    def consume(
        self, tenant_id: str, actor_id: str, action: str
    ) -> HostedRateLimitDecision:
        now_seconds = int(self.clock().timestamp())
        window = self.policy.window_seconds
        window_start = now_seconds - (now_seconds % window)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT request_count
                FROM hosted_rate_windows
                WHERE tenant_id = ? AND actor_id = ? AND action = ?
                  AND window_start = ?
                """,
                (tenant_id, actor_id, action, window_start),
            ).fetchone()
            current = int(row["request_count"]) if row else 0
            if current >= self.policy.requests_per_window:
                self.connection.rollback()
                return HostedRateLimitDecision(
                    allowed=False,
                    remaining=0,
                    retry_after_seconds=max(
                        1, window_start + window - now_seconds
                    ),
                )
            next_count = current + 1
            self.connection.execute(
                """
                INSERT INTO hosted_rate_windows(
                  tenant_id, actor_id, action, window_start, request_count
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, actor_id, action, window_start)
                DO UPDATE SET request_count = excluded.request_count
                """,
                (
                    tenant_id,
                    actor_id,
                    action,
                    window_start,
                    next_count,
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return HostedRateLimitDecision(
            allowed=True,
            remaining=self.policy.requests_per_window - next_count,
            retry_after_seconds=0,
        )
