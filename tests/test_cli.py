import tempfile
import unittest
import json
import os
import subprocess
import sys
from pathlib import Path

from context_memory.cli import doctor, init_workspace, init_workspaces, mcp_config
from context_memory.store import MemoryStore


class CLITests(unittest.TestCase):
    def test_portable_mcp_config(self):
        value = mcp_config("/tmp/example memory.db")
        self.assertEqual(value["type"], "stdio")
        self.assertEqual(value["command"], "uvx")
        self.assertEqual(value["args"][:2], ["context-memory", "--db"])
        pinned = mcp_config("/tmp/memory.db", package="git+https://github.com/example/context-memory.git@" + "a" * 40)
        self.assertEqual(pinned["args"][:2], ["--from", "git+https://github.com/example/context-memory.git@" + "a" * 40])
        with self.assertRaisesRegex(ValueError, "pinned"):
            mcp_config("/tmp/memory.db", package="git+https://github.com/example/context-memory.git")

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
                self.assertEqual(value["register_command"][:5], ["claude", "mcp", "add-json", "--scope", "user"])
                self.assertFalse(value["registered"])
                self.assertEqual(value["status"], "planned")
            finally:
                store.close()

    def test_multi_client_plan_and_cursor_preserving_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"; root.mkdir()
            cursor = Path(temp) / ".cursor" / "mcp.json"; cursor.parent.mkdir()
            cursor.write_text('{"mcpServers":{"existing":{"command":"keep"}},"other":true}\n', encoding="utf-8")
            store = MemoryStore(Path(temp) / "memory.db")
            try:
                planned = init_workspaces(store, str(root), ["claude-code","cursor","vscode","craft"], "installed", False, cursor_config=cursor)
                self.assertEqual([x["client"] for x in planned["clients"]], ["claude-code","cursor","vscode","craft"])
                self.assertEqual(planned["clients"][-1]["status"], "manual")
                registered = init_workspaces(store, str(root), ["cursor"], "installed", True, cursor_config=cursor)
                self.assertTrue(registered["clients"][0]["registered"])
                saved = __import__("json").loads(cursor.read_text(encoding="utf-8"))
                self.assertEqual(saved["mcpServers"]["existing"]["command"], "keep")
                self.assertEqual(saved["mcpServers"]["context-memory"]["command"], "context-memory")
                self.assertTrue(saved["other"])
                self.assertTrue(cursor.with_suffix(".json.bak").exists())
            finally:
                store.close()

    def test_one_client_failure_does_not_abort_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"; root.mkdir()
            cursor = Path(temp) / "mcp.json"; cursor.write_text("not-json", encoding="utf-8")
            store = MemoryStore(Path(temp) / "memory.db")
            try:
                result = init_workspaces(store, str(root), ["cursor","craft"], "installed", True, cursor_config=cursor)
                self.assertEqual(result["clients"][0]["status"], "failed")
                self.assertEqual(result["clients"][1]["status"], "manual")
            finally:
                store.close()

    def test_init_prints_client_neutral_retrieval_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"; root.mkdir()
            store = MemoryStore(Path(temp) / "memory.db")
            try:
                result = init_workspaces(store, str(root), ["generic"], "python", False)
                self.assertEqual(result["workflow"][0], "project_resolve(cwd)")
                self.assertIn("get_context", result["workflow"][2])
                self.assertIn("session_end", result["workflow"][-1])
            finally: store.close()

    def test_migrate_db_captures_live_wal_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "old" / "memory.db"
            destination = Path(temp) / "shared" / "memory.db"
            store = MemoryStore(source)
            try:
                project = store.create_project("migration-test")
                store.record_event(project["id"], "fact", "committed while source remains open")
                env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
                command = [sys.executable, "-m", "context_memory.cli", "--db", str(destination), "migrate-db", str(source)]
                first = subprocess.run(command, cwd=Path(__file__).parents[1], env=env, check=True, capture_output=True, text=True)
                self.assertTrue(json.loads(first.stdout)["migrated"])
                migrated = MemoryStore(destination)
                try: self.assertEqual(migrated.list_projects()[0]["slug"], "migration-test")
                finally: migrated.close()
                second = subprocess.run(command, cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True)
                self.assertNotEqual(second.returncode, 0)
                self.assertIn("destination exists", second.stderr)
            finally: store.close()

    def test_checkpoint_command_records_recovery_state(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "memory.db"
            store = MemoryStore(database)
            try: project = store.create_project("cli-checkpoint")
            finally: store.close()
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
            command = [sys.executable, "-m", "context_memory.cli", "--db", str(database), "checkpoint",
                       project["id"], "final", "completed", "--goal", "Ship checkpoint core",
                       "--completed", "All tests passed", "--next-step", "Add repository facts", "--key", "cli-1",
                       "--test-result", '{"name":"CLI test","status":"passed"}']
            result = subprocess.run(command, cwd=Path(__file__).parents[1], env=env, check=True, capture_output=True, text=True)
            checkpoint = json.loads(result.stdout)
            self.assertEqual(checkpoint["mode"], "final")
            self.assertEqual(checkpoint["completed"], ["All tests passed"])
            self.assertEqual(checkpoint["objective"]["test_results"][0]["name"], "CLI test")
            evaluated_command = [sys.executable, "-m", "context_memory.cli", "--db", str(database), "checkpoint-evaluate",
                                 project["id"], "--context-usage", ".9", "--goal", "Ship checkpoint core",
                                 "--completed", "All tests passed", "--next-step", "Add repository facts"]
            evaluated = json.loads(subprocess.run(evaluated_command, cwd=Path(__file__).parents[1], env=env,
                                                  check=True, capture_output=True, text=True).stdout)
            self.assertEqual(evaluated["suppression"], "unchanged_recovery_state")


if __name__ == "__main__":
    unittest.main()
