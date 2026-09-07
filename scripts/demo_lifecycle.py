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


def mcp_session(
    db: Path,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    timeout: float = 20.0,
) -> list[Any]:
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
    command = [
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
    ]
    try:
        completed = subprocess.run(
            command,
            input="".join(json.dumps(item) + "\n" for item in requests),
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"MCP demo subprocess timed out after {timeout}s; "
            f"stdout={error.stdout!r}; stderr={error.stderr!r}"
        ) from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"MCP demo subprocess exited {error.returncode}; "
            f"stdout={error.stdout!r}; stderr={error.stderr!r}"
        ) from error
    try:
        responses = [
            json.loads(line) for line in completed.stdout.splitlines()
        ]
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"MCP demo returned invalid JSON; stdout={completed.stdout!r}; "
            f"stderr={completed.stderr!r}"
        ) from error
    if len(responses) != len(requests):
        raise RuntimeError(
            f"MCP response count mismatch: requested {len(requests)}, "
            f"received {len(responses)}; stdout={completed.stdout!r}; "
            f"stderr={completed.stderr!r}"
        )
    results = []
    for request, response in zip(requests, responses, strict=True):
        if response.get("id") != request["id"]:
            raise RuntimeError(
                f"MCP response id mismatch: expected {request['id']}, "
                f"received {response.get('id')!r}"
            )
        if "error" in response:
            raise RuntimeError(f"MCP JSON-RPC error: {response['error']}")
        if request["method"] == "initialize":
            continue
        result = response.get("result", {})
        if result.get("isError"):
            raise RuntimeError(f"MCP tool error: {result!r}")
        try:
            results.append(json.loads(result["content"][0]["text"]))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Malformed MCP tool response: {result!r}") from error
    if len(results) != len(calls):
        raise RuntimeError(
            f"MCP tool result count mismatch: called {len(calls)}, "
            f"received {len(results)}"
        )
    return results


def _recall_ids(recall: dict[str, Any]) -> tuple[set[str], set[str]]:
    items = recall.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Recall response has no items list")
    memory_ids = {item.get("memory_id") for item in items}
    source_ids = {
        event_id
        for item in items
        for event_id in item.get("source_event_ids", [])
    }
    return memory_ids, source_ids


def validate_demo(result: dict[str, Any]) -> None:
    """Fail the demo when persistence or lifecycle invariants are wrong."""
    sqlite_id = result["sqlite_memory"]["id"]
    postgres_id = result["postgres_memory"]["id"]
    initial_memories, initial_sources = _recall_ids(result["initial_recall"])
    current_memories, current_sources = _recall_ids(result["current_recall"])
    checks = {
        "initial recall contains SQLite memory": sqlite_id in initial_memories,
        "restart reads the same SQLite source": (
            result["initial_source"] in initial_sources
        ),
        "final recall contains PostgreSQL memory": postgres_id in current_memories,
        "final recall excludes superseded SQLite memory": (
            sqlite_id not in current_memories
        ),
        "final recall reads the same PostgreSQL source": (
            result["current_source"] in current_sources
        ),
        "old memory is superseded": (
            result["sqlite_memory"]["status"] == "superseded"
        ),
        "new memory is active": result["postgres_memory"]["status"] == "active",
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError("Demo validation failed: " + "; ".join(failed))


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
    result = {
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
    validate_demo(result)
    return result


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
