import argparse
import tempfile
import unittest
import json
import os
import re
import subprocess
import sys
from unittest import mock
from pathlib import Path

from context_memory import cli
from context_memory.cli import (
    cleanup_clients,
    doctor,
    erase_database,
    init_workspace,
    init_workspaces,
    mcp_config,
    restore_database,
)
from context_memory.store import MemoryStore
from context_memory.contracts import PROMOTABLE_EVENT_KINDS, workflow_guide


class CLITests(unittest.TestCase):
    def test_command_registry_covers_every_runtime_command(self):
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )

        self.assertEqual(
            set(subparsers.choices),
            {
                "migrate-db",
                "erase-db",
                "restore-db",
                *cli.COMMAND_HANDLERS,
                *cli.RUNTIME_COMMAND_HANDLERS,
            },
        )

    def test_portable_mcp_config(self):
        value = mcp_config("/tmp/example memory.db")
        self.assertEqual(value["type"], "stdio")
        self.assertEqual(value["command"], "uvx")
        self.assertEqual(
            value["args"][:3],
            ["--from", "context-memory-mcp", "context-memory"],
        )
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
                craft = planned["clients"][-1]
                self.assertEqual(craft["guide_template"]["filename"], "guide.md")
                self.assertEqual(craft["guide_template"]["content"], workflow_guide())
                self.assertEqual(craft["promotable_event_kinds"], list(PROMOTABLE_EVENT_KINDS))
                self.assertIn("confirm", craft["next_step"])
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

    def test_client_cleanup_is_planned_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cursor = root / "mcp.json"
            cursor.write_text(
                '{"mcpServers":{"context-memory":{"command":"cm"},'
                '"keep":{"command":"other"}},"other":true}\n',
                encoding="utf-8",
            )
            with mock.patch.object(
                cli, "_client_command", return_value="/usr/bin/client"
            ), mock.patch.object(subprocess, "run") as run:
                result = cleanup_clients(
                    ["claude-code", "codex", "cursor", "vscode"],
                    root,
                    False,
                    cursor,
                )
            self.assertFalse(result["applied"])
            self.assertFalse(result["restart_required"])
            self.assertEqual(
                [item["status"] for item in result["clients"]],
                ["planned", "planned", "planned", "manual"],
            )
            run.assert_not_called()
            saved = json.loads(cursor.read_text(encoding="utf-8"))
            self.assertIn("context-memory", saved["mcpServers"])

    def test_client_cleanup_removes_only_owned_registrations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cursor = root / "mcp.json"
            cursor.write_text(
                '{"mcpServers":{"context-memory":{"command":"cm"},'
                '"keep":{"command":"other"}},"other":true}\n',
                encoding="utf-8",
            )
            with mock.patch.object(
                cli, "_client_command", return_value="/usr/bin/client"
            ), mock.patch.object(subprocess, "run") as run:
                result = cleanup_clients(
                    ["claude-code", "codex", "cursor"],
                    root,
                    True,
                    cursor,
                )
            self.assertTrue(result["restart_required"])
            self.assertTrue(
                all(item["removed"] for item in result["clients"])
            )
            self.assertEqual(
                [call.args[0][1:] for call in run.call_args_list],
                [
                    ["mcp", "remove", "context-memory"],
                    ["mcp", "remove", "context_memory"],
                ],
            )
            saved = json.loads(cursor.read_text(encoding="utf-8"))
            self.assertNotIn("context-memory", saved["mcpServers"])
            self.assertEqual(saved["mcpServers"]["keep"]["command"], "other")
            self.assertTrue(saved["other"])
            self.assertTrue(cursor.with_suffix(".json.bak").exists())

    def test_init_prints_client_neutral_retrieval_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"; root.mkdir()
            store = MemoryStore(Path(temp) / "memory.db")
            try:
                result = init_workspaces(store, str(root), ["generic"], "python", False)
                self.assertIn("context_bootstrap", result["workflow"][0])
                self.assertIn("session_end", result["workflow"][-1])
                self.assertEqual(result["workflow_contract"], workflow_guide())
                self.assertEqual(result["promotable_event_kinds"], list(PROMOTABLE_EVENT_KINDS))
            finally: store.close()

    def test_repository_instruction_templates_match_generated_contract(self):
        root = Path(__file__).parents[1]
        expected = workflow_guide()
        for relative in ("AGENTS.md", "examples/AGENTS.md", "examples/guide.md"):
            with self.subTest(relative=relative):
                self.assertEqual((root / relative).read_text(encoding="utf-8"), expected)

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

    def test_complete_erasure_requires_exact_confirmation_and_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "data" / "memory.db"
            backup = Path(temporary) / "backups" / "memory.db"
            store = MemoryStore(database)
            try:
                project = store.create_project("erase-test")
                store.record_event(
                    project["id"], "fact", "Preserve this in the backup"
                )
            finally:
                store.close()

            with self.assertRaisesRegex(
                ValueError,
                rf"exactly match.*{re.escape(str(database.resolve()))}",
            ):
                erase_database(database, backup, "erase")
            self.assertTrue(database.exists())
            self.assertFalse(backup.exists())

            result = erase_database(database, backup, str(database.resolve()))
            self.assertTrue(result["erased"])
            self.assertTrue(result["recoverable"])
            self.assertFalse(database.exists())
            self.assertTrue(backup.exists())

            restored = MemoryStore(backup)
            try:
                self.assertEqual(
                    restored.get_source(
                        restored.read_events_since(project["id"])["events"][0][
                            "id"
                        ]
                    )["content"],
                    "Preserve this in the backup",
                )
            finally:
                restored.close()

    def test_restore_database_verifies_source_and_protects_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "backup.db"
            destination = root / "data" / "memory.db"
            previous_backup = root / "backups" / "previous.db"

            original = MemoryStore(root / "original.db")
            try:
                project = original.create_project("restored-project")
                original.record_event(
                    project["id"], "fact", "Restored evidence"
                )
                original.backup_to(source)
            finally:
                original.close()

            first = restore_database(source, destination)
            self.assertTrue(first["restored"])
            self.assertTrue(first["verification"]["ok"])

            with self.assertRaisesRegex(ValueError, "use --replace"):
                restore_database(source, destination)
            with self.assertRaisesRegex(
                ValueError,
                rf"exactly match.*{re.escape(str(destination.resolve()))}",
            ):
                restore_database(
                    source,
                    destination,
                    replace=True,
                    backup_existing=previous_backup,
                    confirmation="wrong",
                )

            replacement = MemoryStore(root / "replacement.db")
            try:
                replacement_project = replacement.create_project(
                    "replacement-project"
                )
                replacement.backup_to(source)
            finally:
                replacement.close()
            result = restore_database(
                source,
                destination,
                replace=True,
                backup_existing=previous_backup,
                confirmation=str(destination.resolve()),
            )
            self.assertTrue(result["replaced"])
            self.assertTrue(previous_backup.exists())

            restored = MemoryStore(destination)
            previous = MemoryStore(previous_backup)
            try:
                self.assertEqual(
                    restored.list_projects()[0]["id"],
                    replacement_project["id"],
                )
                self.assertEqual(
                    previous.list_projects()[0]["id"], project["id"]
                )
            finally:
                restored.close()
                previous.close()

    def test_checkpoint_command_records_recovery_state(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "memory.db"
            store = MemoryStore(database)
            try:
                project = store.create_project("cli-checkpoint")
                session = store.start_session(project["id"], "test", external_id="cli-final")
                evidence = store.record_event(project["id"], "deployment", "CLI verification", session_id=session["id"])
            finally: store.close()
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
            command = [sys.executable, "-m", "context_memory.cli", "--db", str(database), "checkpoint",
                       project["id"], "final", "completed", "--goal", "Ship checkpoint core",
                       "--completed", "All tests passed", "--next-step", "Add repository facts", "--key", "cli-1",
                       "--session-id", session["id"], "--verified-event", evidence["id"],
                       "--handoff-title", "CLI handoff", "--handoff-content", "Proceed to repository facts",
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

    def test_user_owned_review_and_correction_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "memory.db"
            store = MemoryStore(database)
            try:
                project = store.create_project("cli-user-controls")
                event = store.record_event(
                    project["id"], "fact", "The retention period is 30 days"
                )
                memory = store.upsert_memory(
                    project["id"],
                    "Retention",
                    "The retention period is 30 days",
                    "fact",
                    "proposed",
                    source_event_ids=[event["id"]],
                )
            finally:
                store.close()

            env = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            }
            base = [
                sys.executable,
                "-m",
                "context_memory.cli",
                "--db",
                str(database),
            ]

            queued = subprocess.run(
                base + ["review-list", project["id"]],
                cwd=Path(__file__).parents[1],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(queued.stdout)[0]["id"], memory["id"])

            approved = subprocess.run(
                base + ["review-action", memory["id"], "approve"],
                cwd=Path(__file__).parents[1],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(approved.stdout)["status"], "active")

            corrected = subprocess.run(
                base
                + [
                    "memory-correct",
                    project["id"],
                    memory["id"],
                    "The retention period is 60 days",
                    "--title",
                    "Updated retention",
                ],
                cwd=Path(__file__).parents[1],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            correction = json.loads(corrected.stdout)
            self.assertEqual(correction["status"], "proposed")
            self.assertEqual(correction["title"], "Updated retention")

            rejected = subprocess.run(
                base
                + [
                    "memory-transition",
                    correction["id"],
                    "rejected",
                    "--note",
                    "User declined this correction",
                ],
                cwd=Path(__file__).parents[1],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(rejected.stdout)["status"], "rejected")

    def test_audit_export_and_offline_verify_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "memory.db"
            bundle = Path(temporary) / "audit.json"
            store = MemoryStore(database)
            try:
                project = store.create_project("cli-audit")
                for index in range(105): store.record_event(project["id"], "fact", f"audit {index}")
                store.set_policy(project["id"], audit_keep_entries=100)
                head = store.maintain(project["id"], True)["checkpoint"]["digest"]
            finally: store.close()
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
            base = [sys.executable, "-m", "context_memory.cli", "--db", str(database)]
            exported = subprocess.run(base + ["audit-export", project["id"], "--output", str(bundle)],
                                      cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True)
            self.assertEqual(exported.returncode, 0, exported.stderr)
            verified = subprocess.run(base + ["audit-verify", str(bundle), "--expected-head-digest", head],
                                      cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertTrue(json.loads(verified.stdout)["anchored"])

    def test_detached_audit_anchor_sign_and_verify_commands(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest("cryptography extra is not installed")
        import base64
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "memory.db"; bundle = Path(temporary) / "audit.json"; anchor = Path(temporary) / "anchor.json"
            store = MemoryStore(database)
            try:
                project = store.create_project("signed-audit")
                for index in range(105): store.record_event(project["id"], "fact", f"signed {index}")
                store.set_policy(project["id"], audit_keep_entries=100); store.maintain(project["id"], True)
                bundle.write_text(json.dumps(store.export_audit_chain(project["id"])), encoding="utf-8")
            finally: store.close()
            secret = base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()
            env = {**os.environ,"PYTHONPATH":str(Path(__file__).parents[1] / "src"),"AUDIT_SIGNING_KEY":secret}
            base = [sys.executable,"-m","context_memory.cli","--db",str(database)]
            signed = subprocess.run(base + ["audit-anchor-sign",str(bundle),"--output",str(anchor),"--private-key-env","AUDIT_SIGNING_KEY"],
                                    cwd=Path(__file__).parents[1],env=env,capture_output=True,text=True)
            self.assertEqual(signed.returncode, 0, signed.stderr)
            public_key = json.loads(signed.stdout)["public_key"]
            verified = subprocess.run(base + ["audit-anchor-verify",str(anchor),"--audit-bundle",str(bundle),"--expected-project-id",project["id"],"--expected-public-key",public_key],
                                      cwd=Path(__file__).parents[1],env=env,capture_output=True,text=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertTrue(json.loads(verified.stdout)["audit_chain"]["anchored"])
            tampered = json.loads(anchor.read_text()); tampered["head_digest"] = "0" * 64; anchor.write_text(json.dumps(tampered))
            rejected = subprocess.run(base + ["audit-anchor-verify",str(anchor)],cwd=Path(__file__).parents[1],env=env,capture_output=True,text=True)
            self.assertEqual(rejected.returncode, 1)


if __name__ == "__main__":
    unittest.main()
