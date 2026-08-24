import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_memory.store import MemoryStore


class ReviewRepositoryTests(unittest.TestCase):
    def test_facade_delegates_review_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.db")
            try:
                project = store.create_project("review-repository")
                memory = store.upsert_memory(
                    project["id"], "Candidate", "Needs review"
                )
                with patch.object(
                    store.review,
                    "review_queue",
                    wraps=store.review.review_queue,
                ) as review_queue:
                    queue = store.review_queue(project["id"])
                review_queue.assert_called_once_with(project["id"])
                self.assertEqual(queue[0]["id"], memory["id"])

                approved = store.review_candidate(memory["id"], "approve")
                self.assertEqual(approved["status"], "active")
            finally:
                store.close()
