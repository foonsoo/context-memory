"""Black-box supported-client registration check for an installed wheel."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    executable = Path(sys.executable).with_name("context-memory")
    if not executable.is_file():
        raise AssertionError(
            f"installed console script not found: {executable}"
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        commands = root / "commands.jsonl"
        binaries = root / "bin"
        binaries.mkdir()
        shim = (
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "path = pathlib.Path(os.environ['REGISTRATION_COMMAND_LOG'])\n"
            "with path.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps([pathlib.Path(sys.argv[0]).name, "
            "*sys.argv[1:]]) + '\\n')\n"
        )
        for name in ("claude", "codex", "code"):
            path = binaries / name
            path.write_text(shim, encoding="utf-8")
            path.chmod(0o755)

        environment = {
            **os.environ,
            "HOME": str(root / "home"),
            "PATH": os.pathsep.join(
                [str(binaries), os.environ.get("PATH", "")]
            ),
            "REGISTRATION_COMMAND_LOG": str(commands),
        }
        database = root / "data" / "memory.db"
        registered = subprocess.run(
            [
                str(executable),
                "--db",
                str(database),
                "init",
                "--workspace",
                str(workspace),
                "--launcher",
                "installed",
                "--clients",
                "claude-code,codex,cursor,vscode,craft",
                "--register",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        result = json.loads(registered.stdout)
        statuses = {
            item["client"]: item["status"] for item in result["clients"]
        }
        if statuses != {
            "claude-code": "registered",
            "codex": "registered",
            "cursor": "registered",
            "vscode": "registered",
            "craft": "manual",
        }:
            raise AssertionError(statuses)
        if not result["restart_required"] or "Restart" not in result[
            "next_step"
        ]:
            raise AssertionError("successful registration omitted restart help")

        calls = [
            json.loads(line)
            for line in commands.read_text(encoding="utf-8").splitlines()
        ]
        if [call[0] for call in calls] != ["claude", "codex", "code"]:
            raise AssertionError(calls)
        if calls[0][1:6] != [
            "mcp",
            "add-json",
            "--scope",
            "user",
            "context-memory",
        ]:
            raise AssertionError(calls[0])
        if calls[1][1:5] != ["mcp", "add", "context_memory", "--"]:
            raise AssertionError(calls[1])
        if calls[2][1] != "--add-mcp":
            raise AssertionError(calls[2])

        cursor = root / "home" / ".cursor" / "mcp.json"
        cursor_config = json.loads(cursor.read_text(encoding="utf-8"))[
            "mcpServers"
        ]["context-memory"]
        if Path(cursor_config["command"]).resolve() != executable.resolve():
            raise AssertionError(cursor_config)
        server = subprocess.Popen(
            [cursor_config["command"], *cursor_config["args"]],
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert server.stdin is not None
            assert server.stdout is not None
            server.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "registration-e2e",
                                "version": "1",
                            },
                        },
                    }
                )
                + "\n"
            )
            server.stdin.flush()
            response = json.loads(server.stdout.readline())
            if response["result"]["serverInfo"]["name"] != "context-memory":
                raise AssertionError(response)
        finally:
            server.terminate()
            server.wait(timeout=5)


if __name__ == "__main__":
    main()
