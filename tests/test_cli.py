import tempfile
import unittest
from pathlib import Path

from context_memory.cli import doctor, init_workspace, mcp_config
from context_memory.store import MemoryStore


class CLITests(unittest.TestCase):
    def test_portable_mcp_config(self):
        value = mcp_config("/tmp/example memory.db")
        self.assertEqual(value["type"], "stdio")
        self.assertEqual(value["command"], "uvx")
        self.assertEqual(value["args"][:2], ["context-memory", "--db"])

    def test_init_is_idempotent_and_client_neutral(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"; root.mkdir()
            store = MemoryStore(Path(temp) / "data" / "memory.db")
            try:
                first = init_workspace(store, str(root), "generic", "uvx", False)
                second = init_workspace(store, str(root), "craft", "installed", False)
                self.assertTrue(first["ready"])
                self.assertEqual(first["project"]["id"], second["project"]["id"])
                self.assertEqual(second["mcp"]["mcpServers"]["context-memory"]["command"], "context-memory")
                self.assertIn("Craft Agents", second["next_step"])
                self.assertTrue(doctor(store)["ok"])
            finally:
                store.close()

    def test_claude_registration_command_is_generated_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"; root.mkdir()
            store = MemoryStore(Path(temp) / "memory.db")
            try:
                value = init_workspace(store, str(root), "claude-code", "uvx", False)
                self.assertEqual(value["register_command"][:5], ["claude", "mcp", "add-json", "--scope", "project"])
                self.assertNotIn("registered", value)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
