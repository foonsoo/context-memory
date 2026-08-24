import tempfile
import unittest
from pathlib import Path

from context_memory.store import MemoryStore


class InvestigationRepositoryTests(unittest.TestCase):
    def test_assembles_provenance_read_model_behind_facade(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.db")
            try:
                project = store.create_project("investigation-repository")
                investigation = store.create_investigation(
                    project["id"], "Question?", "Reason", "Decision"
                )
                event = store.record_event(
                    project["id"], "fact", "Source evidence"
                )
                analysis = store.record_source_analysis(
                    investigation["id"],
                    {
                        "source_type": "documentation",
                        "stable_source_id": "source",
                        "source_version": "v1",
                        "access_reason": "Verify",
                        "analysis_method": "manual",
                    },
                    [
                        {
                            "key": "evidence",
                            "role": "evidence",
                            "content": "Source evidence",
                            "event_id": event["id"],
                        }
                    ],
                )

                result = store.investigations.get_investigation(
                    investigation["id"], analysis["source_analysis_id"]
                )

                self.assertEqual(
                    result["investigation"]["id"], investigation["id"]
                )
                self.assertEqual(len(result["source_analyses"]), 1)
                self.assertEqual(
                    result["source_analyses"][0]["claims"][0]["claim_key"],
                    "evidence",
                )
                self.assertTrue(result["idempotent"])
            finally:
                store.close()
