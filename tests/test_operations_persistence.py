import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_memory.store import MemoryStore


class OperationsRepositoryTests(unittest.TestCase):
    def test_facade_delegates_backup_and_index_rebuild(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = MemoryStore(root / "memory.db")
            try:
                project = store.create_project("operations-repository")
                store.upsert_memory(project["id"], "Index", "Rebuild me")
                with patch.object(
                    store.operations,
                    "rebuild_fts",
                    wraps=store.operations.rebuild_fts,
                ) as rebuild:
                    result = store.rebuild_fts(project["id"])
                rebuild.assert_called_once_with(project["id"])
                self.assertEqual(result["indexed_memories"], 1)

                backup = store.backup_to(root / "backup.db")
                self.assertTrue(backup["ok"])
                self.assertEqual(backup["integrity"], "ok")
            finally:
                store.close()
