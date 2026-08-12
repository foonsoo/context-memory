"""Black-box check for the built wheel and its installed console script."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def request(process: subprocess.Popen[str], request_id: int, method: str, params: dict | None = None) -> dict:
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
        raise AssertionError(f"installed console script was not found: {executable}")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "empty-workspace"
        workspace.mkdir()
        database = root / "state" / "memory.db"

        initialized = subprocess.run(
            [str(executable), "--db", str(database), "init", "--workspace", str(workspace), "--launcher", "installed"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        init_result = json.loads(initialized.stdout)
        server_config = init_result["mcp"]["mcpServers"]["context-memory"]
        if Path(server_config["command"]).resolve() != executable.resolve():
            raise AssertionError("installed launcher did not emit its own stable console-script path")

        command = [server_config["command"], *server_config["args"]]
        first = start(command, workspace)
        try:
            initialized_mcp = request(first, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "installed-e2e", "version": "1"}})
            if initialized_mcp["serverInfo"]["name"] != "context-memory":
                raise AssertionError(initialized_mcp)
            resolved = request(first, 2, "tools/call", {"name": "project_resolve", "arguments": {"cwd": str(workspace)}})["structuredContent"]["result"]
            session = request(first, 3, "tools/call", {"name": "session_start", "arguments": {"project_id": resolved["project"]["id"], "scope_id": resolved["scope_id"], "client": "installed-e2e", "external_id": "restart-check"}})["structuredContent"]["result"]
            event = request(first, 4, "tools/call", {"name": "record_event", "arguments": {"project_id": resolved["project"]["id"], "scope_id": resolved["scope_id"], "session_id": session["id"], "kind": "fact", "content": "installed server survived a restart"}})["structuredContent"]["result"]
            checkpoint = request(first, 5, "tools/call", {"name": "checkpoint_create", "arguments": {
                "project_id": resolved["project"]["id"], "scope_id": resolved["scope_id"],
                "session_id": session["id"], "mode": "interim", "reason": "material_change",
                "goal": "Verify installed recovery", "idempotency_key": "installed-checkpoint",
                "completed": ["Recorded durable event"], "next_step": "Resume after process restart",
                "source_event_cursor": event["event_seq"],
            }})["structuredContent"]["result"]
        finally:
            stop(first)

        second = start(command, workspace)
        try:
            request(second, 6, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "installed-e2e", "version": "1"}})
            source = request(second, 7, "tools/call", {"name": "get_source", "arguments": {"event_id": event["id"]}})["structuredContent"]["result"]
            if source["content"] != "installed server survived a restart":
                raise AssertionError(source)
            checkpoint_source = request(second, 8, "tools/call", {"name": "get_source", "arguments": {"event_id": checkpoint["checkpoint_id"]}})["structuredContent"]["result"]
            recovery = json.loads(checkpoint_source["metadata_json"])["checkpoint"]
            if recovery["next_step"] != "Resume after process restart" or recovery["claims"] != {"completion": False, "verification": False}:
                raise AssertionError(recovery)
            retried = request(second, 9, "tools/call", {"name": "checkpoint_create", "arguments": {
                "project_id": resolved["project"]["id"], "scope_id": resolved["scope_id"],
                "session_id": session["id"], "mode": "interim", "reason": "material_change",
                "goal": "Verify installed recovery", "idempotency_key": "installed-checkpoint",
                "completed": ["Recorded durable event"], "next_step": "Resume after process restart",
                "source_event_cursor": event["event_seq"],
            }})["structuredContent"]["result"]
            if retried != checkpoint:
                raise AssertionError("installed checkpoint retry was not idempotent")
        finally:
            stop(second)


if __name__ == "__main__":
    main()
