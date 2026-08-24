import sqlite3
import unittest

from context_memory.persistence import ProjectEvidenceRepository


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
            CREATE TABLE events(id TEXT, project_id TEXT, content TEXT);
            CREATE TABLE audit_log(
              seq INTEGER, entity_type TEXT, entity_id TEXT, action TEXT
            );
            INSERT INTO projects VALUES('p2','zeta','Zeta');
            INSERT INTO projects VALUES('p1','alpha','Alpha');
            INSERT INTO project_aliases VALUES('p1','name','Alpha','alpha');
            INSERT INTO events VALUES('e1','p1','evidence');
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
