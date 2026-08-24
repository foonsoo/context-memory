import tempfile
import unittest
from pathlib import Path

from context_memory.store import MemoryStore


class MemoryRepositoryTests(unittest.TestCase):
    def test_memory_identity_reads_live_behind_facade(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.db")
            try:
                project = store.create_project("memory-repository")
                event = store.record_event(project["id"], "fact", "Evidence")
                memory = store.upsert_memory(
                    project["id"],
                    "Evidence",
                    "Evidence",
                    source_event_ids=[event["id"]],
                )

                self.assertEqual(
                    store.memories.get(memory["id"])["id"], memory["id"]
                )
                self.assertEqual(
                    store.memories.get_proposed(memory["id"])["status"],
                    "proposed",
                )
                self.assertIsNone(store.memories.get("missing"))
            finally:
                store.close()
