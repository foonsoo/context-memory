import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_facade_delegates_memory_lifecycle_to_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.db")
            try:
                project = store.create_project("memory-lifecycle")
                with patch.object(
                    store.memories,
                    "upsert_memory",
                    wraps=store.memories.upsert_memory,
                ) as upsert:
                    memory = store.upsert_memory(
                        project["id"], "Decision", "Use bounded repositories"
                    )
                upsert.assert_called_once()

                with patch.object(
                    store.memories,
                    "transition",
                    wraps=store.memories.transition,
                ) as transition:
                    active = store.transition(memory["id"], "active")
                transition.assert_called_once_with(
                    memory["id"], "active", None, ""
                )
                self.assertEqual(active["status"], "active")
                self.assertIsNotNone(active["updated_at"])
                embedding = store.conn.execute(
                    "SELECT memory_id FROM memory_embeddings WHERE"
                    " memory_id=?",
                    (memory["id"],),
                ).fetchone()
                self.assertEqual(embedding["memory_id"], memory["id"])

                other = store.upsert_memory(
                    project["id"], "Outcome", "Repository boundary held"
                )
                store.transition(other["id"], "active")
                alias = store.set_search_aliases(
                    project["id"], "repository", ["persistence"]
                )
                self.assertEqual(alias["aliases"], ["persistence"])
                self.assertEqual(
                    store.list_search_aliases(project["id"])[0]["term"],
                    "repository",
                )
                relation = store.create_relation(
                    project["id"],
                    memory["id"],
                    other["id"],
                    "supports",
                )
                graph = store.traverse(project["id"], memory["id"])
                self.assertEqual(graph["edges"][0]["id"], relation["id"])
                feedback = store.record_memory_feedback(
                    memory["id"], "helpful"
                )
                self.assertEqual(feedback["helpful_count"], 1)
            finally:
                store.close()
