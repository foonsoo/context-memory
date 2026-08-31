import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from context_memory.hosted_content import (
    HostedContentStore,
    RequestScopedHostedContentStore,
)

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


class HostedFailureLoadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def initialized_content(self, path):
        store = HostedContentStore(path, clock=lambda: NOW)
        store.provision_tenant("tenant-a")
        store.provision_project("tenant-a", "project-a")
        return store

    def test_request_scoped_connections_survive_concurrent_reads_and_writes(
        self,
    ):
        database = self.root / "concurrent.db"
        self.initialized_content(database).close()
        repository = RequestScopedHostedContentStore(
            database, clock=lambda: NOW, wal_autocheckpoint_pages=16
        )
        start = threading.Barrier(8)
        latencies = []
        latency_lock = threading.Lock()

        def writer(worker):
            start.wait(timeout=2)
            for index in range(30):
                began = time.perf_counter()
                repository.record_event(
                    "tenant-a",
                    "project-a",
                    "fact",
                    f"worker-{worker}-event-{index}",
                )
                with latency_lock:
                    latencies.append(time.perf_counter() - began)

        def reader():
            start.wait(timeout=2)
            for _ in range(30):
                repository.search("tenant-a", "project-a", "worker")

        began = time.perf_counter()
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(writer, worker) for worker in range(4)]
            futures += [executor.submit(reader) for _ in range(4)]
            for future in futures:
                future.result(timeout=10)
        elapsed = time.perf_counter() - began

        exported = repository.export_project("tenant-a", "project-a")
        sequences = [event["event_seq"] for event in exported["events"]]
        self.assertEqual(sequences, list(range(1, 121)))
        self.assertEqual(
            len({event["content"] for event in exported["events"]}), 120
        )
        p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
        self.assertLess(elapsed, 10)
        self.assertLess(p95, 2)

    def test_abrupt_exit_keeps_committed_and_discards_uncommitted_write(self):
        database = self.root / "interruption.db"
        self.initialized_content(database).close()

        def run_child(content, commit, exit_code):
            script = """
import os, sqlite3, sys
path, content, commit, exit_code = sys.argv[1:]
connection = sqlite3.connect(path)
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('BEGIN IMMEDIATE')
connection.execute(
    '''INSERT INTO hosted_content_events(
         tenant_id, project_id, event_seq, kind, content, created_at
       ) VALUES('tenant-a','project-a',1,'fact',?,?)''',
    (content, '2026-08-31T00:00:00+00:00'),
)
if commit == 'yes':
    connection.commit()
os._exit(int(exit_code))
"""
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(database),
                    content,
                    "yes" if commit else "no",
                    str(exit_code),
                ],
                check=False,
            )

        uncommitted = run_child("uncommitted", False, 17)
        self.assertEqual(uncommitted.returncode, 17)
        reopened = HostedContentStore(database, clock=lambda: NOW)
        self.assertEqual(
            reopened.export_project("tenant-a", "project-a")["events"], []
        )
        reopened.close()

        committed = run_child("committed", True, 18)
        self.assertEqual(committed.returncode, 18)
        reopened = HostedContentStore(database, clock=lambda: NOW)
        try:
            events = reopened.export_project("tenant-a", "project-a")["events"]
            self.assertEqual(
                [event["content"] for event in events], ["committed"]
            )
            self.assertEqual(
                reopened.connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0],
                "ok",
            )
        finally:
            reopened.close()

    def test_wal_growth_is_observed_then_truncated_after_reader_releases(self):
        database = self.root / "wal.db"
        writer = self.initialized_content(database)
        writer.record_event("tenant-a", "project-a", "fact", "initial")
        reader = sqlite3.connect(database)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM hosted_content_events").fetchone()
        for index in range(80):
            writer.record_event(
                "tenant-a", "project-a", "fact", f"{index}:" + "x" * 2048
            )
        wal_path = Path(f"{database}-wal")
        self.assertTrue(wal_path.exists())
        observed_bytes = wal_path.stat().st_size
        self.assertGreater(observed_bytes, 0)
        self.assertLess(observed_bytes, 4 * 1024 * 1024)

        reader.rollback()
        reader.close()
        result = writer.checkpoint_wal("TRUNCATE")
        self.assertEqual(result["busy"], 0)
        self.assertEqual(result["log_pages"], 0)
        self.assertEqual(wal_path.stat().st_size, 0)
        writer.close()

    def test_hosted_schema_migration_rolls_back_then_forwards(self):
        database = self.root / "migration.db"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE hosted_content_tenants(tenant_id TEXT PRIMARY KEY);
            CREATE TABLE hosted_content_projects(
              tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id, project_id)
            );
            CREATE TABLE hosted_content_events(
              tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
              event_seq INTEGER NOT NULL, kind TEXT NOT NULL,
              content TEXT NOT NULL,
              PRIMARY KEY(tenant_id, project_id, event_seq)
            );
            INSERT INTO hosted_content_tenants VALUES('tenant-a');
            INSERT INTO hosted_content_projects VALUES('tenant-a','project-a');
            INSERT INTO hosted_content_events
              VALUES('tenant-a','project-a',1,'fact','legacy');
            CREATE TRIGGER fail_migration
            BEFORE UPDATE ON hosted_content_events
            BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END;
            """
        )
        connection.close()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            HostedContentStore(database, clock=lambda: NOW)
        connection = sqlite3.connect(database)
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(hosted_content_events)"
            )
        }
        self.assertNotIn("created_at", columns)
        self.assertEqual(
            connection.execute("PRAGMA user_version").fetchone()[0], 0
        )
        connection.execute("DROP TRIGGER fail_migration")
        connection.close()

        migrated = HostedContentStore(database, clock=lambda: NOW)
        try:
            event = migrated.export_project("tenant-a", "project-a")["events"][
                0
            ]
            self.assertEqual(event["content"], "legacy")
            self.assertEqual(event["created_at"], NOW.isoformat())
            self.assertEqual(
                migrated.connection.execute("PRAGMA user_version").fetchone()[
                    0
                ],
                2,
            )
        finally:
            migrated.close()

    def test_tenant_backup_restores_exact_data_into_empty_store(self):
        source = self.initialized_content(self.root / "source.db")
        source.record_event("tenant-a", "project-a", "decision", "preserve me")
        backup = source.backup_tenant("tenant-a")
        source.close()

        restored = HostedContentStore(
            self.root / "restored.db", clock=lambda: NOW
        )
        try:
            result = restored.restore_tenant(backup)
            self.assertEqual(
                result, {"tenant_id": "tenant-a", "projects": 1, "events": 1}
            )
            self.assertEqual(
                json.loads(restored.backup_tenant("tenant-a")),
                json.loads(backup),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                restored.restore_tenant(backup)
        finally:
            restored.close()
