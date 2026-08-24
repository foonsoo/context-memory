import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_memory.store import MemoryStore


class TransferRepositoryTests(unittest.TestCase):
    def test_facade_delegates_portable_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = MemoryStore(root / "source.db")
            restored = MemoryStore(root / "restored.db")
            try:
                project = source.create_project("transfer-repository")
                source.record_event(project["id"], "fact", "Evidence")
                with patch.object(
                    source.transfer,
                    "export_project",
                    wraps=source.transfer.export_project,
                ) as export_project:
                    records = source.export_project(project["id"])
                export_project.assert_called_once_with(project["id"])

                with patch.object(
                    restored.transfer,
                    "import_project",
                    wraps=restored.transfer.import_project,
                ) as import_project:
                    result = restored.import_project(records)
                import_project.assert_called_once_with(records)
                self.assertEqual(result["project_id"], project["id"])
                self.assertEqual(
                    restored.export_project(project["id"]), records
                )
            finally:
                source.close()
                restored.close()
