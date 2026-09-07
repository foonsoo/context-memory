#!/usr/bin/env python3
"""Run a synthetic save/restart/recall/change demo through MCP stdio."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def mcp_session(db: Path, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        *[
            {
                "jsonrpc": "2.0",
                "id": index + 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            for index, (name, arguments) in enumerate(calls)
        ],
    ]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "context_memory.cli",
            "--db",
            str(db),
            "serve",
            "--transport",
            "stdio",
            "--tool-profile",
            "minimal",
        ],
        input="".join(json.dumps(item) + "\n" for item in requests),
        text=True,
        capture_output=True,
        check=True,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    results = []
    for response in responses[1:]:
        if "error" in response:
            raise RuntimeError(response["error"])
        results.append(json.loads(response["result"]["content"][0]["text"]))
    return results


def run(root: Path) -> dict[str, Any]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    db = root / "demo.db"

    bootstrap = mcp_session(
        db,
        [
            ("context_bootstrap", {"cwd": str(workspace), "query": "database", "client": "demo", "external_id": "demo-1", "response_format": "compact"}),
        ],
    )[0]
    # The second call needs the generated project id, so repeat it in a fresh
    # process. Each mcp_session invocation is an actual server restart.
    project_id = bootstrap["project"]["id"]
    session_id = bootstrap["session"]["id"]
    sqlite_event = mcp_session(
        db,
        [("record_event", {"project_id": project_id, "kind": "decision", "content": "Choose SQLite for the first local version"})],
    )[0]
    sqlite_memory = mcp_session(
        db,
        [("memory_upsert", {"project_id": project_id, "title": "Database decision", "content": "Use SQLite", "memory_type": "decision", "status": "active", "source_event_ids": [sqlite_event["id"]]})],
    )[0]
    recalled = mcp_session(
        db,
        [("context_recall", {"cwd": str(workspace), "query": "current database decision"}), ("get_source", {"event_id": sqlite_event["id"]})],
    )
    postgres_event = mcp_session(
        db,
        [("record_event", {"project_id": project_id, "kind": "decision", "content": "Move to PostgreSQL for concurrent writers"})],
    )[0]
    postgres_memory = mcp_session(
        db,
        [("memory_upsert", {"project_id": project_id, "title": "Database decision", "content": "Use PostgreSQL", "memory_type": "decision", "status": "active", "source_event_ids": [postgres_event["id"]]})],
    )[0]
    changed, _ended = mcp_session(
        db,
        [("memory_transition", {"memory_id": sqlite_memory["id"], "status": "superseded", "related_memory_id": postgres_memory["id"], "note": "PostgreSQL replaced SQLite"}), ("session_end", {"session_id": session_id, "extract_candidates": False})],
    )
    final_recall = mcp_session(
        db,
        [("context_recall", {"cwd": str(workspace), "query": "current database PostgreSQL"}), ("get_source", {"event_id": postgres_event["id"]})],
    )
    return {
        "database": str(db),
        "initial_recall": recalled[0],
        "initial_source": recalled[1]["id"],
        "current_recall": final_recall[0],
        "current_source": final_recall[1]["id"],
        "sqlite_memory": {
            "id": changed["id"],
            "status": changed["status"],
        },
        "postgres_memory": {
            "id": postgres_memory["id"],
            "status": postgres_memory["status"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="Keep the temporary demo directory")
    args = parser.parse_args()
    if args.keep:
        root = Path(tempfile.mkdtemp(prefix="context-memory-demo-"))
        print(json.dumps(run(root), indent=2))
        return
    with tempfile.TemporaryDirectory(prefix="context-memory-demo-") as temporary:
        print(json.dumps(run(Path(temporary)), indent=2))


if __name__ == "__main__":
    main()
