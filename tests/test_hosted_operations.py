import errno
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from context_memory.hosted_operations import (
    HostedDatabaseTarget,
    HostedOperationsMonitor,
    HostedStorageExhaustedError,
    run_sqlite_restore_drill,
    translate_storage_error,
)

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


class HostedOperationsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = self.root / "service.db"
        connection = sqlite3.connect(self.database)
        connection.execute(
            "CREATE TABLE required_table(id INTEGER PRIMARY KEY)"
        )
        connection.execute("INSERT INTO required_table(id) VALUES(1)")
        connection.commit()
        connection.close()
        self.logs = []

    def tearDown(self):
        self.tempdir.cleanup()

    def monitor(self, **kwargs):
        values = {
            "databases": (
                HostedDatabaseTarget(
                    "content", self.database, ("required_table",)
                ),
            ),
            "backup_completed_at": lambda: NOW - timedelta(hours=1),
            "clock": lambda: NOW,
            "log_sink": self.logs.append,
        }
        values.update(kwargs)
        return HostedOperationsMonitor(**values)

    def test_readiness_checks_migration_database_and_backup_age(self):
        ready = self.monitor().readiness()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["checks"]["database:content"]["status"], "ok")

        stale = self.monitor(
            backup_completed_at=lambda: NOW - timedelta(days=2)
        ).readiness()
        self.assertEqual(stale["status"], "not_ready")
        self.assertEqual(stale["checks"]["backup"]["reason"], "stale")

        migration = self.monitor(migration_id="hosted-v0").readiness()
        self.assertEqual(migration["status"], "not_ready")
        self.assertEqual(migration["checks"]["migration"]["status"], "failed")

    def test_metrics_and_logs_are_redacted_and_bounded(self):
        ticks = iter((10.0, 10.125))
        monitor = self.monitor(timer=lambda: next(ticks))
        started = monitor.request_started("request-1", "trace-1", "search")
        monitor.request_finished(
            "request-1", "trace-1", "search", 500, started
        )
        metrics = monitor.metrics_snapshot()
        self.assertEqual(metrics["active_requests"], 0)
        self.assertEqual(metrics["requests"]["search:500"], 1)
        self.assertEqual(metrics["errors"]["server_error"], 1)
        self.assertEqual(metrics["latency_seconds"]["search"]["max"], 0.125)
        serialized = str(self.logs)
        self.assertNotIn("tenant", serialized)
        self.assertNotIn("content", serialized)
        self.assertNotIn("authorization", serialized)
        self.assertIn("request-1", serialized)
        self.assertIn("trace-1", serialized)

    def test_telemetry_sink_and_backup_probe_fail_closed_without_crashing(
        self,
    ):
        monitor = self.monitor(
            log_sink=lambda event: (_ for _ in ()).throw(RuntimeError("sink")),
            backup_completed_at=lambda: (_ for _ in ()).throw(
                RuntimeError("provider")
            ),
        )
        started = monitor.request_started("request-1", "trace-1", "search")
        monitor.request_finished(
            "request-1", "trace-1", "search", 200, started
        )
        self.assertEqual(monitor.readiness()["status"], "not_ready")

    def test_restore_drill_copies_and_integrity_checks_isolated_database(self):
        result = run_sqlite_restore_drill(self.database, ("required_table",))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["integrity"], "ok")
        missing = run_sqlite_restore_drill(self.database, ("missing_table",))
        self.assertEqual(missing["status"], "failed")
        unavailable = run_sqlite_restore_drill(
            self.root / "absent.db", ("required_table",)
        )
        self.assertEqual(unavailable["status"], "failed")

    def test_disk_full_errors_map_without_path_or_sql_disclosure(self):
        sqlite_error = translate_storage_error(
            sqlite3.OperationalError("database or disk is full: /private/a.db")
        )
        self.assertIsInstance(sqlite_error, HostedStorageExhaustedError)
        self.assertNotIn("/private", str(sqlite_error))
        os_error = translate_storage_error(OSError(errno.ENOSPC, "no space"))
        self.assertIsInstance(os_error, HostedStorageExhaustedError)
        unrelated = ValueError("ordinary")
        self.assertIs(translate_storage_error(unrelated), unrelated)
