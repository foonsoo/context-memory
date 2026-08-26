"""Black-box upgrade check from the latest published local release."""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
import tempfile
from pathlib import Path


PUBLISHED_VERSION = "0.6.2"


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
    )


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


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: upgrade_e2e.py CURRENT_WHEEL")
    current_wheel = Path(sys.argv[1]).resolve()
    if not current_wheel.is_file():
        raise AssertionError(f"current wheel not found: {current_wheel}")

    executable = Path(sys.executable).with_name("context-memory")
    run(
        sys.executable,
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        f"context-memory-mcp=={PUBLISHED_VERSION}",
    )
    if importlib.metadata.version("context-memory-mcp") != PUBLISHED_VERSION:
        raise AssertionError("published release installation was not selected")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        database = root / "data" / "memory.db"
        backup = root / "backups" / "before-upgrade.db"
        post_upgrade_backup = root / "backups" / "after-upgrade.db"

        initialized = run(
            str(executable),
            "--db",
            str(database),
            "init",
            "--workspace",
            str(workspace),
            "--launcher",
            "installed",
        )
        project_id = json.loads(initialized.stdout)["project"]["id"]
        backed_up = run(
            str(executable),
            "--db",
            str(database),
            "backup",
            "--output",
            str(backup),
        )
        if json.loads(backed_up.stdout)["integrity"] != "ok":
            raise AssertionError(backed_up.stdout)

        run(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            "--no-deps",
            str(current_wheel),
        )
        doctor = json.loads(
            run(str(executable), "--db", str(database), "doctor").stdout
        )
        if not doctor["ok"]:
            raise AssertionError(doctor)

        projects = json.loads(
            run(
                str(executable),
                "--db",
                str(database),
                "project-list",
            ).stdout
        )
        if project_id not in {project["id"] for project in projects}:
            raise AssertionError("upgrade did not preserve project identity")
        if not backup.is_file():
            raise AssertionError("upgrade removed the pre-upgrade backup")

        server = subprocess.Popen(
            [
                str(executable),
                "--db",
                str(database),
                "serve",
                "--transport",
                "stdio",
            ],
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            request(
                server,
                1,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "upgrade-e2e",
                        "version": "1",
                    },
                },
            )
            bootstrapped = request(
                server,
                2,
                "tools/call",
                {
                    "name": "context_bootstrap",
                    "arguments": {
                        "cwd": str(workspace),
                        "query": "verify the upgraded normal lifecycle",
                        "client": "upgrade-e2e",
                        "external_id": "post-upgrade-lifecycle",
                        "response_format": "compact",
                    },
                },
            )["structuredContent"]["result"]
            event = request(
                server,
                3,
                "tools/call",
                {
                    "name": "record_event",
                    "arguments": {
                        "project_id": project_id,
                        "scope_id": bootstrapped["scope_id"],
                        "session_id": bootstrapped["session"]["id"],
                        "kind": "fact",
                        "content": "normal lifecycle completed after upgrade",
                    },
                },
            )["structuredContent"]["result"]
            if not event["id"]:
                raise AssertionError(event)
            ended = request(
                server,
                4,
                "tools/call",
                {
                    "name": "session_end",
                    "arguments": {
                        "session_id": bootstrapped["session"]["id"],
                        "summary": "post-upgrade lifecycle passed",
                        "extract_candidates": False,
                    },
                },
            )["structuredContent"]["result"]
            if not ended["ended_at"]:
                raise AssertionError(ended)
        finally:
            server.terminate()
            server.wait(timeout=5)

        fresh_backup = json.loads(
            run(
                str(executable),
                "--db",
                str(database),
                "backup",
                "--output",
                str(post_upgrade_backup),
            ).stdout
        )
        if fresh_backup["integrity"] != "ok":
            raise AssertionError(fresh_backup)
        if not post_upgrade_backup.is_file():
            raise AssertionError("post-upgrade backup was not retained")


if __name__ == "__main__":
    main()
