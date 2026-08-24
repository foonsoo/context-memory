import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_memory.store import MemoryStore


class RetrievalRepositoryTests(unittest.TestCase):
    def test_facade_delegates_ranked_search_assembly(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.db")
            try:
                project = store.create_project("retrieval-repository")
                store.upsert_memory(
                    project["id"],
                    "Bounded retrieval",
                    "Retrieval SQL lives behind the facade",
                    status="active",
                )
                with patch.object(
                    store.retrieval_repository,
                    "search",
                    wraps=store.retrieval_repository.search,
                ) as search:
                    results = store.search(project["id"], "bounded retrieval")
                search.assert_called_once_with(
                    project["id"],
                    "bounded retrieval",
                    10,
                    None,
                    None,
                    False,
                )
                self.assertEqual(results[0]["title"], "Bounded retrieval")
                self.assertIn("retrieval", results[0])
            finally:
                store.close()
