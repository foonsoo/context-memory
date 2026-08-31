from __future__ import annotations

import errno
import re
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Callable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HostedDatabaseTarget:
    name: str
    path: Path
    required_tables: tuple[str, ...]


class HostedStorageExhaustedError(RuntimeError):
    """Storage-capacity failure without underlying path disclosure."""


def translate_storage_error(exc: BaseException) -> BaseException:
    message = str(exc).lower()
    disk_full = isinstance(exc, OSError) and exc.errno in {
        errno.ENOSPC,
        errno.EDQUOT,
    }
    sqlite_full = isinstance(exc, sqlite3.OperationalError) and (
        "database or disk is full" in message
    )
    if disk_full or sqlite_full:
        return HostedStorageExhaustedError("hosted storage is unavailable")
    return exc


class HostedOperationsMonitor:
    """Hosted health, metrics, and redacted request logging."""

    def __init__(
        self,
        databases: tuple[HostedDatabaseTarget, ...] = (),
        *,
        migration_id: str = "hosted-v1",
        expected_migration_id: str = "hosted-v1",
        max_backup_age_seconds: int = 86_400,
        backup_completed_at: Callable[[], datetime | None] = lambda: None,
        log_sink: Callable[[dict[str, object]], None] = lambda event: None,
        clock: Callable[[], datetime] = _utc_now,
        timer: Callable[[], float] = monotonic,
    ) -> None:
        if max_backup_age_seconds < 1:
            raise ValueError("maximum backup age must be positive")
        names = [target.name for target in databases]
        if len(names) != len(set(names)) or any(
            re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) is None
            for name in names
        ):
            raise ValueError("database labels must be unique and bounded")
        self.databases = databases
        self.migration_id = migration_id
        self.expected_migration_id = expected_migration_id
        self.max_backup_age_seconds = max_backup_age_seconds
        self.backup_completed_at = backup_completed_at
        self.log_sink = log_sink
        self.clock = clock
        self.timer = timer
        self._lock = Lock()
        self._active = 0
        self._requests: Counter[tuple[str, int]] = Counter()
        self._errors: Counter[str] = Counter()
        self._latency_count: Counter[str] = Counter()
        self._latency_sum: Counter[str] = Counter()
        self._latency_max: dict[str, float] = {}

    def liveness(self) -> dict[str, object]:
        return {"status": "ok"}

    def readiness(self) -> dict[str, object]:
        checks: dict[str, dict[str, object]] = {}
        ready = True
        migration_ready = self.migration_id == self.expected_migration_id
        checks["migration"] = {
            "status": "ok" if migration_ready else "failed",
            "current": self.migration_id,
            "expected": self.expected_migration_id,
        }
        ready = ready and migration_ready
        for target in self.databases:
            result = self._database_check(target)
            checks[f"database:{target.name}"] = result
            ready = ready and result["status"] == "ok"
        backup = self._backup_check()
        checks["backup"] = backup
        ready = ready and backup["status"] == "ok"
        return {"status": "ready" if ready else "not_ready", "checks": checks}

    def request_started(
        self, request_id: str, trace_id: str, operation: str
    ) -> float:
        started = self.timer()
        with self._lock:
            self._active += 1
            depth = self._active
        self._emit(
            "request_started",
            request_id=request_id,
            trace_id=trace_id,
            operation=operation,
            queue_depth=depth,
        )
        return started

    def request_finished(
        self,
        request_id: str,
        trace_id: str,
        operation: str,
        status: int,
        started: float,
    ) -> None:
        elapsed = max(0.0, self.timer() - started)
        error_class = self._error_class(status)
        with self._lock:
            self._active = max(0, self._active - 1)
            self._requests[(operation, status)] += 1
            self._latency_count[operation] += 1
            self._latency_sum[operation] += elapsed
            self._latency_max[operation] = max(
                elapsed, self._latency_max.get(operation, 0.0)
            )
            if error_class:
                self._errors[error_class] += 1
            depth = self._active
        self._emit(
            "request_finished",
            request_id=request_id,
            trace_id=trace_id,
            operation=operation,
            status=status,
            latency_ms=round(elapsed * 1000, 3),
            queue_depth=depth,
            error_class=error_class,
        )

    def metrics_snapshot(self) -> dict[str, object]:
        with self._lock:
            operations = sorted(self._latency_count)
            result = {
                "active_requests": self._active,
                "requests": {
                    f"{operation}:{status}": count
                    for (operation, status), count in sorted(
                        self._requests.items()
                    )
                },
                "errors": dict(sorted(self._errors.items())),
                "latency_seconds": {
                    operation: {
                        "count": self._latency_count[operation],
                        "sum": self._latency_sum[operation],
                        "max": self._latency_max[operation],
                    }
                    for operation in operations
                },
            }
        result["database_bytes"] = {
            target.name: self._database_bytes(target.path)
            for target in self.databases
        }
        result["backup_age_seconds"] = self._backup_age_seconds()
        return result

    def record_listener_error(self) -> None:
        with self._lock:
            self._errors["listener_error"] += 1
        self._emit("listener_error", error_class="listener_error")

    def _database_check(
        self, target: HostedDatabaseTarget
    ) -> dict[str, object]:
        try:
            connection = sqlite3.connect(
                f"file:{target.path}?mode=ro", uri=True, timeout=1
            )
            try:
                integrity = connection.execute(
                    "PRAGMA quick_check"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                connection.close()
            missing = sorted(set(target.required_tables) - tables)
            if integrity != "ok" or missing:
                return {"status": "failed", "reason": "schema_or_integrity"}
            return {"status": "ok"}
        except (OSError, sqlite3.Error):
            return {"status": "failed", "reason": "unavailable"}

    def _backup_check(self) -> dict[str, object]:
        age = self._backup_age_seconds()
        if age is None:
            return {"status": "failed", "reason": "missing"}
        if age > self.max_backup_age_seconds:
            return {"status": "failed", "reason": "stale", "age_seconds": age}
        return {"status": "ok", "age_seconds": age}

    def _backup_age_seconds(self) -> float | None:
        try:
            completed = self.backup_completed_at()
        except Exception:
            return None
        if completed is None:
            return None
        return max(0.0, (self.clock() - completed).total_seconds())

    @staticmethod
    def _database_bytes(path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    @staticmethod
    def _error_class(status: int) -> str | None:
        if status < 400:
            return None
        if status >= 500:
            return "server_error"
        if status == 429:
            return "rate_limited"
        return "client_error"

    def _emit(self, event: str, **fields: object) -> None:
        # Callers provide only allowlisted operational fields. Tenant,
        # actor, headers, query text, request bodies, and exception
        # messages are absent.
        payload = {"event": event, "timestamp": self.clock().isoformat()}
        payload.update(fields)
        try:
            self.log_sink(payload)
        except Exception:
            # Telemetry failure cannot change request behavior.
            return


def run_sqlite_restore_drill(
    source: str | Path, required_tables: tuple[str, ...]
) -> dict[str, object]:
    """Restore to an isolated DB and verify integrity and schema."""
    source_path = Path(source).expanduser().resolve()
    try:
        with tempfile.TemporaryDirectory() as tempdir:
            restored_path = Path(tempdir) / "restore-drill.db"
            source_connection = sqlite3.connect(
                f"file:{source_path}?mode=ro", uri=True, timeout=1
            )
            restored = sqlite3.connect(restored_path)
            try:
                source_connection.backup(restored)
                integrity = restored.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in restored.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                restored.close()
                source_connection.close()
    except (OSError, sqlite3.Error):
        return {
            "status": "failed",
            "integrity": "unavailable",
            "missing_tables": list(required_tables),
        }
    missing = sorted(set(required_tables) - tables)
    return {
        "status": "passed" if integrity == "ok" and not missing else "failed",
        "integrity": integrity,
        "missing_tables": missing,
    }
