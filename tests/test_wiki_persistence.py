import tempfile
import unittest
from pathlib import Path

from context_memory.store import MemoryStore


class WikiRepositoryTests(unittest.TestCase):
    def test_page_identity_and_writes_live_behind_facade(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = MemoryStore(Path(temporary) / "memory.db")
            try:
                project = store.create_project("wiki-repository")
                scope = store.create_scope(project["id"], "root")
                page = store.create_wiki_page(
                    project["id"], "topic", "Title", scope["id"]
                )

                self.assertTrue(
                    store.wiki.scope_belongs_to_project(
                        scope["id"], project["id"]
                    )
                )
                self.assertEqual(
                    store.wiki.get_page(page["id"])["title"], "Title"
                )
                store.set_wiki_notes(page["id"], "Reviewed")
                self.assertEqual(
                    store.wiki.get_page(page["id"])["manual_notes"],
                    "Reviewed",
                )
            finally:
                store.close()
