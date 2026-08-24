import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_memory.persistence import ProjectEvidenceRepository
from context_memory.store import MemoryStore


class ProjectEvidenceRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE projects(id TEXT, slug TEXT, name TEXT);
            CREATE TABLE project_aliases(
              project_id TEXT, kind TEXT, value TEXT, normalized TEXT
            );
            CREATE TABLE events(
              id TEXT, project_id TEXT, scope_id TEXT, session_id TEXT,
              kind TEXT, content TEXT, source_uri TEXT, metadata_json TEXT,
              content_hash TEXT, created_at TEXT, event_seq INTEGER
            );
            CREATE TABLE project_policies(
              project_id TEXT, message_ttl_seconds INTEGER
            );
            CREATE TABLE project_event_cursors(
              project_id TEXT, next_seq INTEGER
            );
            CREATE TABLE scopes(
              id TEXT, project_id TEXT, name TEXT, path TEXT, created_at TEXT
            );
            CREATE TABLE sessions(
              id TEXT, project_id TEXT, scope_id TEXT, client TEXT,
              external_id TEXT, started_at TEXT, ended_at TEXT,
              metadata_json TEXT
            );
            CREATE TABLE audit_log(
              seq INTEGER, entity_type TEXT, entity_id TEXT, action TEXT
            );
            INSERT INTO projects VALUES('p2','zeta','Zeta');
            INSERT INTO projects VALUES('p1','alpha','Alpha');
            INSERT INTO project_aliases VALUES('p1','name','Alpha','alpha');
            INSERT INTO events VALUES(
              'e1','p1',NULL,NULL,'fact','evidence',NULL,'{}','hash','now',1
            );
            INSERT INTO project_policies VALUES('p1',60);
            INSERT INTO project_event_cursors VALUES('p1',2);
            INSERT INTO audit_log VALUES(2,'event','e1','updated');
            INSERT INTO audit_log VALUES(1,'event','e1','created');
            """
        )
        self.repository = ProjectEvidenceRepository(self.connection)

    def tearDown(self):
        self.connection.close()

    def test_reads_bounded_project_and_evidence_queries(self):
        self.assertEqual(
            [item["id"] for item in self.repository.list_projects()],
            ["p1", "p2"],
        )
        self.assertTrue(self.repository.project_exists("p1"))
        self.assertFalse(self.repository.project_exists("missing"))
        self.assertEqual(
            self.repository.list_project_aliases("p1")[0]["value"],
            "Alpha",
        )
        self.assertEqual(self.repository.get_event("e1")["content"], "evidence")
        self.assertIsNone(self.repository.get_event("missing"))
        self.assertEqual(
            [
                item["action"]
                for item in self.repository.audit_entries("event", "e1")
            ],
            ["created", "updated"],
        )

    def test_owns_scope_and_session_persistence(self):
        scope = {
            "id": "scope",
            "project_id": "p1",
            "name": "root",
            "path": "/workspace",
            "created_at": "started",
        }
        session = {
            "id": "session",
            "project_id": "p1",
            "scope_id": "scope",
            "client": "codex",
            "external_id": "external",
            "started_at": "started",
            "ended_at": None,
            "metadata_json": "{}",
        }
        self.repository.insert_scope(self.connection, scope)
        self.repository.insert_session(self.connection, session)

        self.assertEqual(
            self.repository.find_session("p1", "codex", "external")["id"],
            "session",
        )
        self.repository.set_session_ended(
            self.connection, "session", "finished"
        )
        self.assertEqual(
            self.repository.get_session("session")["ended_at"], "finished"
        )

    def test_owns_event_sequence_policy_and_insert(self):
        self.assertEqual(
            self.repository.message_ttl_seconds(self.connection, "p1"), 60
        )
        sequence = self.repository.allocate_event_sequence(
            self.connection, "p1"
        )
        self.assertEqual(sequence, 2)
        item = {
            "id": "e2",
            "project_id": "p1",
            "scope_id": None,
            "session_id": None,
            "kind": "message",
            "content": "hello",
            "source_uri": None,
            "metadata_json": "{}",
            "content_hash": "hash",
            "created_at": "now",
            "event_seq": sequence,
        }
        self.repository.insert_event(self.connection, item)
        self.assertEqual(self.repository.get_event("e2")["event_seq"], 2)

    def test_facade_delegates_durable_cursor_operations(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.db")
            try:
                project = store.create_project("cursor-repository")
                store.record_event(project["id"], "message", "Update")
                with patch.object(
                    store.project_evidence,
                    "read_events_since",
                    wraps=store.project_evidence.read_events_since,
                ) as read:
                    result = store.read_events_since(project["id"])
                read.assert_called_once_with(project["id"], 0, None, None, 100)
                self.assertEqual(result["events"][0]["content"], "Update")

                delivered = store.poll_events(project["id"], "consumer")
                receipt = store.acknowledge_events(
                    project["id"], "consumer", delivered["next_cursor"]
                )
                self.assertEqual(
                    receipt["acknowledged_cursor"], delivered["next_cursor"]
                )
            finally:
                store.close()
