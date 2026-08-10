from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from .mcp import MCPServer
from .store import MemoryStore


def default_db() -> str:
    return os.environ.get("CONTEXT_MEMORY_DB", str(Path.home() / ".local/share/context-memory/memory.db"))


def output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def mcp_config(db_path: str, launcher: str = "uvx") -> dict[str, object]:
    """Return a portable stdio MCP definition understood by most clients."""
    if launcher == "uvx":
        command, args = "uvx", ["context-memory", "--db", db_path, "serve", "--transport", "stdio"]
    elif launcher == "installed":
        command, args = "context-memory", ["--db", db_path, "serve", "--transport", "stdio"]
    else:
        command, args = sys.executable, ["-m", "context_memory.cli", "--db", db_path, "serve", "--transport", "stdio"]
    return {"type": "stdio", "command": command, "args": args}


def init_workspace(store: MemoryStore, workspace: str, client: str, launcher: str, register: bool) -> dict[str, object]:
    root = str(Path(workspace).expanduser().resolve())
    resolved = store.resolve_project(root)
    config = mcp_config(str(store.path), launcher)
    result: dict[str, object] = {
        "ready": True,
        "database": str(store.path),
        "workspace": root,
        "project": resolved["project"],
        "scope_id": resolved["scope_id"],
        "mcp": {"mcpServers": {"context-memory": config}},
    }
    command, args = str(config["command"]), [str(x) for x in config["args"]]
    if client == "claude-code":
        definition = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
        register_command = ["claude", "mcp", "add-json", "--scope", "project", "context-memory", definition]
        result["register_command"] = register_command
        if register:
            if not shutil.which("claude"):
                raise RuntimeError("Claude Code CLI was not found; run register_command manually after installing it")
            subprocess.run(register_command, cwd=root, check=True)
            result["registered"] = True
    elif client == "codex":
        register_command = ["codex", "mcp", "add", "context_memory", "--", command, *args]
        result["register_command"] = register_command
        if register:
            if not shutil.which("codex"):
                raise RuntimeError("Codex CLI was not found; run register_command manually after installing it")
            subprocess.run(register_command, cwd=root, check=True)
            result["registered"] = True
    elif client == "craft":
        result["next_step"] = "Paste the mcp object into Craft Agents as a local stdio source, or ask the agent to add this MCP JSON."
    else:
        result["next_step"] = "Add mcp.mcpServers.context-memory to any MCP client's server configuration."
    return result


def doctor(store: MemoryStore) -> dict[str, object]:
    fts5 = bool(store.conn.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0])
    integrity = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
    permissions_private = (store.path.parent.stat().st_mode & 0o077) == 0
    return {
        "ok": fts5 and integrity == "ok" and permissions_private,
        "database": str(store.path),
        "sqlite_version": sqlite3.sqlite_version,
        "fts5": fts5,
        "integrity": integrity,
        "permissions_private": permissions_private,
        "projects": len(store.list_projects()),
    }


def main() -> None:
    p = argparse.ArgumentParser(prog="context-memory", description="Local-first context memory")
    p.add_argument("--db", default=default_db(), help="SQLite database path")
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--token")
    init = sub.add_parser("init", help="Initialize a workspace and print/register portable MCP configuration")
    init.add_argument("--workspace", default=os.getcwd())
    init.add_argument("--client", choices=["generic", "claude-code", "codex", "craft"], default="generic")
    init.add_argument("--launcher", choices=["uvx", "installed", "python"], default="uvx")
    init.add_argument("--register", action="store_true")
    sub.add_parser("doctor", help="Check the database, FTS5, and local permissions")
    project = sub.add_parser("project-create"); project.add_argument("slug"); project.add_argument("--name"); project.add_argument("--description", default="")
    sub.add_parser("project-list")
    event = sub.add_parser("event"); event.add_argument("project_id"); event.add_argument("kind"); event.add_argument("content"); event.add_argument("--session-id"); event.add_argument("--key")
    memory = sub.add_parser("memory"); memory.add_argument("project_id"); memory.add_argument("title"); memory.add_argument("content"); memory.add_argument("--type", default="other"); memory.add_argument("--status", default="proposed"); memory.add_argument("--source", action="append", default=[]); memory.add_argument("--confidence", type=float, default=.5); memory.add_argument("--importance", type=float, default=.5)
    search = sub.add_parser("search"); search.add_argument("project_id"); search.add_argument("query"); search.add_argument("--limit", type=int, default=10)
    context = sub.add_parser("context"); context.add_argument("project_id"); context.add_argument("query"); context.add_argument("--budget", type=int, default=6000)
    source = sub.add_parser("source"); source.add_argument("event_id")
    export = sub.add_parser("export", help="Export one project as deterministic JSON Lines")
    export.add_argument("project_id"); export.add_argument("--output", required=True)
    import_cmd = sub.add_parser("import", help="Restore a JSON Lines project export without overwriting existing data")
    import_cmd.add_argument("input")
    repair = sub.add_parser("repair", help="Rebuild the disposable FTS projection from authoritative memories")
    repair.add_argument("--project-id")
    args = p.parse_args()
    store = MemoryStore(args.db)
    try:
        if args.command == "serve":
            token = args.token or os.environ.get("CONTEXT_MEMORY_TOKEN")
            server = MCPServer(store)
            server.serve_stdio() if args.transport == "stdio" else server.serve_http(args.host, args.port, token)
        elif args.command == "init": output(init_workspace(store, args.workspace, args.client, args.launcher, args.register))
        elif args.command == "doctor": output(doctor(store))
        elif args.command == "project-create": output(store.create_project(args.slug, args.name, args.description))
        elif args.command == "project-list": output(store.list_projects())
        elif args.command == "event": output(store.record_event(args.project_id, args.kind, args.content, session_id=args.session_id, idempotency_key=args.key))
        elif args.command == "memory": output(store.upsert_memory(args.project_id, args.title, args.content, args.type, args.status, args.confidence, args.importance, source_event_ids=args.source))
        elif args.command == "search": output(store.search(args.project_id, args.query, args.limit))
        elif args.command == "context": output(store.get_context(args.project_id, args.query, args.budget))
        elif args.command == "source": output(store.get_source(args.event_id))
        elif args.command == "export":
            destination = Path(args.output).expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            records = store.export_project(args.project_id)
            with destination.open("w", encoding="utf-8") as stream:
                for record in records:
                    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            output({"ok": True, "project_id": args.project_id, "output": str(destination), "records": len(records)})
        elif args.command == "import":
            source_path = Path(args.input).expanduser().resolve()
            records = [json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            output({"ok": True, **store.import_project(records)})
        elif args.command == "repair": output(store.rebuild_fts(args.project_id))
    finally:
        store.close()


if __name__ == "__main__":
    main()
