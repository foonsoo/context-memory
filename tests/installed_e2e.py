"""Black-box user journey for the installed console script."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def request(
    process: subprocess.Popen[str],
    request_id: int,
    method: str,
    params: dict | None = None,
) -> dict:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    response = json.loads(process.stdout.readline())
    if "error" in response:
        raise AssertionError(response["error"])
    return response["result"]


def start(command: list[str], workspace: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    process.wait(timeout=5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def main() -> None:
    executable = Path(sys.executable).with_name("context-memory")
    if not executable.is_file():
        raise AssertionError(
            f"installed console script was not found: {executable}"
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "empty-workspace"
        workspace.mkdir()
        database = root / "state" / "memory.db"

        initialized = subprocess.run(
            [
                str(executable),
                "--db",
                str(database),
                "init",
                "--workspace",
                str(workspace),
                "--launcher",
                "installed",
            ],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        init_result = json.loads(initialized.stdout)
        server_config = init_result["mcp"]["mcpServers"]["context-memory"]
        if Path(server_config["command"]).resolve() != executable.resolve():
            raise AssertionError(
                "installed launcher did not emit its own stable "
                "console-script path"
            )

        command = [
            server_config["command"],
            *server_config["args"],
            "--tool-profile",
            "all",
        ]
        first = start(command, workspace)
        try:
            initialized_mcp = request(
                first,
                1,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "installed-e2e", "version": "1"},
                },
            )
            if initialized_mcp["serverInfo"]["name"] != "context-memory":
                raise AssertionError(initialized_mcp)
            resolved = request(
                first,
                2,
                "tools/call",
                {
                    "name": "project_resolve",
                    "arguments": {"cwd": str(workspace)},
                },
            )["structuredContent"]["result"]
            session = request(
                first,
                3,
                "tools/call",
                {
                    "name": "session_start",
                    "arguments": {
                        "project_id": resolved["project"]["id"],
                        "scope_id": resolved["scope_id"],
                        "client": "installed-e2e",
                        "external_id": "restart-check",
                    },
                },
            )["structuredContent"]["result"]
            event = request(
                first,
                4,
                "tools/call",
                {
                    "name": "record_event",
                    "arguments": {
                        "project_id": resolved["project"]["id"],
                        "scope_id": resolved["scope_id"],
                        "session_id": session["id"],
                        "kind": "fact",
                        "content": "installed server survived a restart",
                    },
                },
            )["structuredContent"]["result"]
            checkpoint = request(
                first,
                5,
                "tools/call",
                {
                    "name": "checkpoint_create",
                    "arguments": {
                        "project_id": resolved["project"]["id"],
                        "scope_id": resolved["scope_id"],
                        "session_id": session["id"],
                        "mode": "interim",
                        "reason": "material_change",
                        "goal": "Verify installed recovery",
                        "idempotency_key": "installed-checkpoint",
                        "completed": ["Recorded durable event"],
                        "next_step": "Resume after process restart",
                        "source_event_cursor": event["event_seq"],
                    },
                },
            )["structuredContent"]["result"]
            memory = request(
                first,
                6,
                "tools/call",
                {
                    "name": "memory_upsert",
                    "arguments": {
                        "project_id": resolved["project"]["id"],
                        "scope_id": resolved["scope_id"],
                        "title": "Preferred release channel",
                        "content": "Use the stable release channel",
                        "memory_type": "decision",
                        "status": "proposed",
                        "source_event_ids": [event["id"]],
                    },
                },
            )["structuredContent"]["result"]
            queue = request(
                first,
                7,
                "tools/call",
                {
                    "name": "review_queue",
                    "arguments": {"project_id": resolved["project"]["id"]},
                },
            )["structuredContent"]["result"]
            if memory["id"] not in {item["id"] for item in queue}:
                raise AssertionError("first useful memory was not reviewable")
            approved = request(
                first,
                8,
                "tools/call",
                {
                    "name": "review_action",
                    "arguments": {
                        "memory_id": memory["id"],
                        "action": "approve",
                    },
                },
            )["structuredContent"]["result"]
            if approved["status"] != "active":
                raise AssertionError(approved)
        finally:
            stop(first)

        second = start(command, workspace)
        try:
            request(
                second,
                9,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "installed-e2e", "version": "1"},
                },
            )
            source = request(
                second,
                10,
                "tools/call",
                {"name": "get_source", "arguments": {"event_id": event["id"]}},
            )["structuredContent"]["result"]
            if source["content"] != "installed server survived a restart":
                raise AssertionError(source)
            checkpoint_source = request(
                second,
                11,
                "tools/call",
                {
                    "name": "get_source",
                    "arguments": {"event_id": checkpoint["checkpoint_id"]},
                },
            )["structuredContent"]["result"]
            recovery = json.loads(checkpoint_source["metadata_json"])[
                "checkpoint"
            ]
            if recovery[
                "next_step"
            ] != "Resume after process restart" or recovery["claims"] != {
                "completion": False,
                "verification": False,
            }:
                raise AssertionError(recovery)
            retried = request(
                second,
                12,
                "tools/call",
                {
                    "name": "checkpoint_create",
                    "arguments": {
                        "project_id": resolved["project"]["id"],
                        "scope_id": resolved["scope_id"],
                        "session_id": session["id"],
                        "mode": "interim",
                        "reason": "material_change",
                        "goal": "Verify installed recovery",
                        "idempotency_key": "installed-checkpoint",
                        "completed": ["Recorded durable event"],
                        "next_step": "Resume after process restart",
                        "source_event_cursor": event["event_seq"],
                    },
                },
            )["structuredContent"]["result"]
            if retried != checkpoint:
                raise AssertionError(
                    "installed checkpoint retry was not idempotent"
                )
            resumed = request(
                second,
                13,
                "tools/call",
                {
                    "name": "context_bootstrap",
                    "arguments": {
                        "cwd": str(workspace),
                        "query": "Which release channel should we use?",
                        "client": "installed-e2e",
                        "external_id": "next-session",
                        "response_format": "compact",
                    },
                },
            )["structuredContent"]["result"]
            if memory["id"] not in {
                item["memory_id"] for item in resumed["context"]["items"]
            }:
                raise AssertionError("approved memory was not retrieved")
            recalled = request(
                second,
                140,
                "tools/call",
                {
                    "name": "context_recall",
                    "arguments": {
                        "cwd": str(workspace),
                        "query": "Continue the release channel work",
                    },
                },
            )["structuredContent"]["result"]
            if recalled["project"]["id"] != resolved["project"]["id"]:
                raise AssertionError("context recall selected the wrong project")
            if recalled["repository_path"] != str(workspace.resolve()):
                raise AssertionError("context recall lost the repository path")
            if not any(
                memory["id"] == item.get("memory_id")
                for item in recalled["items"]
            ):
                raise AssertionError("context recall missed the active decision")
            correction = request(
                second,
                14,
                "tools/call",
                {
                    "name": "memory_correct",
                    "arguments": {
                        "project_id": resolved["project"]["id"],
                        "memory_id": memory["id"],
                        "content": "Use the long-term-support release channel",
                    },
                },
            )["structuredContent"]["result"]
            corrected = request(
                second,
                15,
                "tools/call",
                {
                    "name": "review_action",
                    "arguments": {
                        "memory_id": correction["id"],
                        "action": "supersede",
                        "related_memory_id": memory["id"],
                    },
                },
            )["structuredContent"]["result"]
            if corrected["status"] != "active":
                raise AssertionError(corrected)
            investigation = request(
                second,
                16,
                "tools/call",
                {
                    "name": "investigation_create",
                    "arguments": {
                        "project_id": resolved["project"]["id"],
                        "question": "Is the release policy current?",
                        "reason": "The channel decision cites it",
                        "decision_to_inform": "Keep or revise the channel",
                    },
                },
            )["structuredContent"]["result"]
            analysis = request(
                second,
                17,
                "tools/call",
                {
                    "name": "investigation_record_source",
                    "arguments": {
                        "investigation_id": investigation["id"],
                        "source": {
                            "source_type": "documentation",
                            "stable_source_id": "release-policy",
                            "source_version": "1",
                            "access_reason": "Verify the release channel",
                            "analysis_method": "client claim extraction",
                        },
                        "claims": [
                            {
                                "key": "policy",
                                "role": "evidence",
                                "content": "The policy defines an LTS channel",
                            }
                        ],
                    },
                },
            )["structuredContent"]["result"]
            reinspection = request(
                second,
                18,
                "tools/call",
                {
                    "name": "source_reinspection_request",
                    "arguments": {
                        "source_analysis_id": analysis["source_analysis_id"],
                        "reason": "newer_version_known",
                        "known_source_version": "2",
                    },
                },
            )["structuredContent"]["result"]
            if reinspection["execution"] != {
                "owner": "client",
                "core_fetch_performed": False,
                "state": "requested",
            }:
                raise AssertionError(reinspection)
        finally:
            stop(second)

        backup = root / "backups" / "memory.db"
        backed_up = subprocess.run(
            [
                str(executable),
                "--db",
                str(database),
                "backup",
                "--output",
                str(backup),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if json.loads(backed_up.stdout)["integrity"] != "ok":
            raise AssertionError(backed_up.stdout)
        restored = root / "restored" / "memory.db"
        subprocess.run(
            [
                str(executable),
                "--db",
                str(restored),
                "restore-db",
                str(backup),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        diagnosed = subprocess.run(
            [str(executable), "--db", str(restored), "doctor"],
            check=True,
            capture_output=True,
            text=True,
        )
        if not json.loads(diagnosed.stdout)["ok"]:
            raise AssertionError(diagnosed.stdout)
        cleanup = subprocess.run(
            [str(executable), "unregister", "--clients", "generic"],
            check=True,
            capture_output=True,
            text=True,
        )
        cleanup_result = json.loads(cleanup.stdout)
        if (
            cleanup_result["applied"]
            or cleanup_result["clients"][0]["status"] != "manual"
        ):
            raise AssertionError(cleanup_result)

        erasure_backup = root / "backups" / "before-erasure.db"
        refused_erasure = subprocess.run(
            [
                str(executable),
                "--db",
                str(restored),
                "erase-db",
                "--backup",
                str(erasure_backup),
                "--confirm",
                "wrong-path",
            ],
            capture_output=True,
            text=True,
        )
        if refused_erasure.returncode == 0:
            raise AssertionError("erasure accepted an incorrect confirmation")
        if str(restored.resolve()) not in refused_erasure.stderr:
            raise AssertionError(refused_erasure.stderr)
        if not restored.exists() or erasure_backup.exists():
            raise AssertionError("refused erasure changed user data")

        erased = subprocess.run(
            [
                str(executable),
                "--db",
                str(restored),
                "erase-db",
                "--backup",
                str(erasure_backup),
                "--confirm",
                str(restored.resolve()),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        erased_result = json.loads(erased.stdout)
        if restored.exists() or erased_result["erased"] is not True:
            raise AssertionError(erased_result)
        if erased_result["backup"]["integrity"] != "ok":
            raise AssertionError(erased_result)

        recovered = root / "recovered" / "memory.db"
        subprocess.run(
            [
                str(executable),
                "--db",
                str(recovered),
                "restore-db",
                str(erasure_backup),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        recovered_doctor = subprocess.run(
            [str(executable), "--db", str(recovered), "doctor"],
            check=True,
            capture_output=True,
            text=True,
        )
        if not json.loads(recovered_doctor.stdout)["ok"]:
            raise AssertionError(recovered_doctor.stdout)


if __name__ == "__main__":
    main()
