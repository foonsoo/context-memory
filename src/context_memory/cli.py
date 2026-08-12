from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import re
from pathlib import Path

from .mcp import MCPServer
from .store import MemoryStore


def default_db() -> str:
    return os.environ.get("CONTEXT_MEMORY_DB", str(Path.home() / ".local/share/context-memory/memory.db"))


def output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def mcp_config(db_path: str, launcher: str = "uvx", package: str = "context-memory") -> dict[str, object]:
    """Return a portable stdio MCP definition understood by most clients."""
    if launcher == "uvx":
        if package.startswith("git+") and not re.search(r"@[0-9a-fA-F]{40}(?:#|$)", package):
            raise ValueError("Git uvx sources must be pinned to a full 40-character commit SHA")
        prefix = ["context-memory"] if package == "context-memory" else ["--from", package, "context-memory"]
        command, args = "uvx", [*prefix, "--db", db_path, "serve", "--transport", "stdio"]
    elif launcher == "installed":
        installed = shutil.which("context-memory")
        invoked = Path(sys.argv[0]).expanduser().resolve() if Path(sys.argv[0]).name == "context-memory" else None
        command = str(invoked) if invoked and invoked.is_file() else (installed or "context-memory")
        args = ["--db", db_path, "serve", "--transport", "stdio"]
    else:
        command, args = sys.executable, ["-m", "context_memory.cli", "--db", db_path, "serve", "--transport", "stdio"]
    return {"type": "stdio", "command": command, "args": args}


def _client_command(client: str) -> str | None:
    names = {"claude-code": ["claude"], "codex": ["codex"], "vscode": ["code"], "craft": ["craft", "craft-agents"], "cursor": ["cursor"]}
    for name in names.get(client, []):
        if found := shutil.which(name): return found
    mac_apps = {
        "vscode": "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
        "cursor": "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
        "craft": "/Applications/Craft Agents.app/Contents/MacOS/Craft Agents",
    }
    candidate = mac_apps.get(client)
    return candidate if candidate and Path(candidate).exists() else None


def detect_clients() -> list[str]:
    return [name for name in ("claude-code", "codex", "cursor", "vscode", "craft") if _client_command(name)]


def _write_cursor_global(config: dict[str, object], target: Path | None = None) -> dict[str, object]:
    path = target or Path.home() / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    current: dict[str, object] = {}
    backup = None
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict): raise ValueError(f"Cursor MCP config must be a JSON object: {path}")
        backup = path.with_suffix(".json.bak")
        index = 1
        while backup.exists():
            backup = path.with_suffix(f".json.bak.{index}"); index += 1
        shutil.copy2(path, backup)
    servers = current.setdefault("mcpServers", {})
    if not isinstance(servers, dict): raise ValueError(f"Cursor mcpServers must be a JSON object: {path}")
    servers["context-memory"] = {k: v for k, v in config.items() if k in {"command", "args", "env"}}
    fd, temporary = tempfile.mkstemp(prefix=".mcp-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(current, stream, ensure_ascii=False, indent=2); stream.write("\n")
        os.chmod(temporary, 0o600); os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return {"config_path": str(path), "backup": str(backup) if backup else None}


def _register_client(client: str, config: dict[str, object], root: str, register: bool, cursor_config: Path | None = None) -> dict[str, object]:
    command, args = str(config["command"]), [str(x) for x in config["args"]]
    detected = bool(_client_command(client))
    result: dict[str, object] = {"client": client, "detected": detected, "registered": False}
    if client == "claude-code":
        definition = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
        register_command = ["claude", "mcp", "add-json", "--scope", "user", "context-memory", definition]
    elif client == "codex":
        register_command = ["codex", "mcp", "add", "context_memory", "--", command, *args]
    elif client == "vscode":
        executable = _client_command(client) or "code"
        definition = json.dumps({"name":"context-memory","command":command,"args":args}, ensure_ascii=False, separators=(",", ":"))
        register_command = [executable, "--add-mcp", definition]
    elif client == "cursor":
        result["config_path"] = str(cursor_config or Path.home() / ".cursor" / "mcp.json")
        if register:
            result.update(_write_cursor_global(config, cursor_config)); result["registered"] = True; result["status"] = "registered"
        else: result["status"] = "planned"
        return result
    elif client == "craft":
        result.update({"status":"manual", "next_step":"In Craft Agents, ask the agent to add a local MCP source and paste the provided mcp_json. Craft sources are workspace-scoped.", "mcp_json":config})
        return result
    elif client == "generic":
        result.update({"status":"manual", "next_step":"Add the provided MCP JSON to the client's user-level server configuration.", "mcp_json":config})
        return result
    else:
        raise ValueError(f"unsupported client: {client}")
    result["register_command"] = register_command
    if register:
        executable = _client_command(client)
        if not executable:
            result.update({"status":"unavailable", "error":f"{client} executable was not found"}); return result
        register_command[0] = executable
        try:
            subprocess.run(register_command, cwd=root, check=True, capture_output=True, text=True)
            result.update({"registered":True, "status":"registered"})
        except subprocess.CalledProcessError as exc:
            result.update({"status":"failed", "error":(exc.stderr or exc.stdout or str(exc)).strip()})
    else: result["status"] = "planned"
    return result


def _safe_register_client(client: str, config: dict[str, object], root: str, register: bool, cursor_config: Path | None) -> dict[str, object]:
    try:
        return _register_client(client, config, root, register, cursor_config)
    except Exception as exc:
        return {"client":client, "detected":bool(_client_command(client)), "registered":False, "status":"failed", "error":str(exc)}


def init_workspaces(store: MemoryStore, workspace: str, clients: list[str], launcher: str, register: bool,
                    package: str = "context-memory", cursor_config: Path | None = None) -> dict[str, object]:
    root = str(Path(workspace).expanduser().resolve())
    resolved = store.resolve_project(root)
    config = mcp_config(str(store.path), launcher, package)
    expanded = detect_clients() if clients == ["auto"] else clients
    if not expanded: expanded = ["generic"]
    invalid = sorted(set(expanded) - {"generic","claude-code","codex","cursor","vscode","craft"})
    if invalid: raise ValueError("unsupported clients: " + ", ".join(invalid))
    result: dict[str, object] = {
        "ready": True,
        "database": str(store.path),
        "workspace": root,
        "project": resolved["project"],
        "scope_id": resolved["scope_id"],
        "mcp": {"mcpServers": {"context-memory": config}},
        "workflow": [
            "context_bootstrap(cwd, focused query, client, external_id, char_budget=4000..8000, response_format=compact)",
            "record_event -> memory_upsert(source_event_ids) for durable verified knowledge",
            "session_end(session_id)",
        ],
        "clients": [_safe_register_client(client, config, root, register, cursor_config) for client in dict.fromkeys(expanded)],
    }
    return result


def init_workspace(store: MemoryStore, workspace: str, client: str, launcher: str, register: bool) -> dict[str, object]:
    """Backward-compatible single-client wrapper."""
    result = init_workspaces(store, workspace, [client], launcher, register)
    adapter = result["clients"][0]
    for key in ("register_command", "registered", "next_step", "status", "error"):
        if key in adapter: result[key] = adapter[key]
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
    serve.add_argument("--tool-profile", choices=["core", "admin", "all"], default="core",
                       help="Expose the compact working set, administrative tools, or every tool")
    init = sub.add_parser("init", help="Initialize a workspace and print/register portable MCP configuration")
    init.add_argument("--workspace", default=os.getcwd())
    client_group = init.add_mutually_exclusive_group()
    client_group.add_argument("--client", choices=["generic", "claude-code", "codex", "cursor", "vscode", "craft"], help="Single-client compatibility option")
    client_group.add_argument("--clients", help="Comma-separated clients or 'auto' (claude-code,codex,cursor,vscode,craft)")
    init.add_argument("--launcher", choices=["uvx", "installed", "python"], default="uvx")
    init.add_argument("--package", default="context-memory", help="uvx package or git+ URL pinned to a full commit SHA")
    init.add_argument("--register", action="store_true")
    sub.add_parser("doctor", help="Check the database, FTS5, and local permissions")
    project = sub.add_parser("project-create"); project.add_argument("slug"); project.add_argument("--name"); project.add_argument("--description", default="")
    sub.add_parser("project-list")
    event = sub.add_parser("event"); event.add_argument("project_id"); event.add_argument("kind"); event.add_argument("content"); event.add_argument("--session-id"); event.add_argument("--key")
    checkpoint = sub.add_parser("checkpoint", help="Record an explicit idempotent recovery checkpoint")
    checkpoint.add_argument("project_id"); checkpoint.add_argument("mode", choices=["interim", "final"])
    checkpoint.add_argument("reason", choices=["context_budget", "elapsed", "material_change", "completed", "manual"])
    checkpoint.add_argument("--goal", required=True); checkpoint.add_argument("--key", required=True)
    checkpoint.add_argument("--session-id"); checkpoint.add_argument("--scope-id")
    checkpoint.add_argument("--completed", action="append", default=[]); checkpoint.add_argument("--next-step")
    checkpoint.add_argument("--blocker", action="append", default=[]); checkpoint.add_argument("--source-event-cursor", type=int)
    checkpoint.add_argument("--context-usage", type=float)
    checkpoint.add_argument("--repository", dest="repository_path")
    checkpoint.add_argument("--test-result", action="append", default=[], help='JSON object with name, status, and optional command/details')
    checkpoint.add_argument("--verified-event", action="append", default=[])
    checkpoint.add_argument("--handoff-title"); checkpoint.add_argument("--handoff-content")
    checkpoint.add_argument("--previous-handoff-memory-id"); checkpoint.add_argument("--commit")
    checkpoint_eval = sub.add_parser("checkpoint-evaluate", help="Evaluate checkpoint thresholds without writing")
    checkpoint_eval.add_argument("project_id"); checkpoint_eval.add_argument("--context-usage", type=float)
    checkpoint_eval.add_argument("--session-id"); checkpoint_eval.add_argument("--repository", dest="repository_path")
    checkpoint_eval.add_argument("--goal", default=""); checkpoint_eval.add_argument("--completed", action="append", default=[])
    checkpoint_eval.add_argument("--next-step"); checkpoint_eval.add_argument("--blocker", action="append", default=[])
    events_since = sub.add_parser("events-since", help="Read immutable project events after a cursor")
    events_since.add_argument("project_id"); events_since.add_argument("--cursor", type=int, default=0); events_since.add_argument("--kind", action="append"); events_since.add_argument("--scope-id"); events_since.add_argument("--limit", type=int, default=100)
    memory = sub.add_parser("memory"); memory.add_argument("project_id"); memory.add_argument("title"); memory.add_argument("content"); memory.add_argument("--type", default="other"); memory.add_argument("--status", default="proposed"); memory.add_argument("--source", action="append", default=[]); memory.add_argument("--confidence", type=float, default=.5); memory.add_argument("--importance", type=float, default=.5)
    search = sub.add_parser("search"); search.add_argument("project_id"); search.add_argument("query"); search.add_argument("--limit", type=int, default=10)
    context = sub.add_parser("context"); context.add_argument("project_id"); context.add_argument("query"); context.add_argument("--budget", type=int, default=6000)
    context.add_argument("--event-cursor", type=int); context.add_argument("--event-kind", action="append"); context.add_argument("--event-limit", type=int, default=20); context.add_argument("--event-budget", type=int, default=2000)
    source = sub.add_parser("source"); source.add_argument("event_id")
    export = sub.add_parser("export", help="Export one project as deterministic JSON Lines")
    export.add_argument("project_id"); export.add_argument("--output", required=True)
    import_cmd = sub.add_parser("import", help="Restore a JSON Lines project export without overwriting existing data")
    import_cmd.add_argument("input")
    repair = sub.add_parser("repair", help="Rebuild the disposable FTS projection from authoritative memories")
    repair.add_argument("--project-id")
    policy = sub.add_parser("policy", help="Read or update project operational bounds")
    policy.add_argument("project_id"); policy.add_argument("--max-context-chars", type=int); policy.add_argument("--max-context-items", type=int)
    policy.add_argument("--audit-keep-entries", type=int); policy.add_argument("--terminal-memory-days", type=int)
    policy.add_argument("--checkpoint-soft-usage", type=float); policy.add_argument("--checkpoint-hard-usage", type=float)
    policy.add_argument("--checkpoint-elapsed-seconds", type=int); policy.add_argument("--checkpoint-event-count", type=int)
    policy.add_argument("--checkpoint-max-age-seconds", type=int)
    policy.add_argument("--checkpoint-cooldown-seconds", type=int); policy.add_argument("--checkpoint-hysteresis", type=float)
    policy.add_argument("--maintenance-interval-seconds", type=int)
    maintain = sub.add_parser("maintain", help="Plan/apply terminal-memory cleanup and checkpointed audit compaction")
    maintain.add_argument("project_id"); maintain.add_argument("--apply", action="store_true"); maintain.add_argument("--scheduled", action="store_true")
    status_cmd = sub.add_parser("status", help="Show project policy, storage counts, audit checkpoints, and search health")
    status_cmd.add_argument("project_id")
    audit_export = sub.add_parser("audit-export", help="Export a deterministic bundle for offline audit-chain verification")
    audit_export.add_argument("project_id"); audit_export.add_argument("--output", required=True)
    audit_verify = sub.add_parser("audit-verify", help="Verify an exported audit chain without opening its source database")
    audit_verify.add_argument("input"); audit_verify.add_argument("--expected-head-digest")
    backup = sub.add_parser("backup", help="Create one consistent integrity-checked SQLite snapshot")
    backup.add_argument("--output", required=True); backup.add_argument("--passphrase-env")
    decrypt = sub.add_parser("backup-decrypt", help="Decrypt an authenticated backup envelope to a SQLite snapshot")
    decrypt.add_argument("input"); decrypt.add_argument("--output", required=True); decrypt.add_argument("--passphrase-env", required=True)
    migrate = sub.add_parser("migrate-db", help="Safely migrate a live SQLite database with the Online Backup API")
    migrate.add_argument("source"); migrate.add_argument("--replace", action="store_true")
    args = p.parse_args()
    if args.command == "migrate-db":
        source = Path(args.source).expanduser().resolve(); destination = Path(args.db).expanduser().resolve()
        if source == destination: p.error("migration source and destination must differ")
        if not source.is_file(): p.error(f"migration source does not exist: {source}")
        if destination.exists() and not args.replace: p.error(f"migration destination exists; use --replace after backing it up: {destination}")
        existing_backup = None
        if destination.exists():
            previous = MemoryStore(destination)
            try:
                existing_backup = previous.backup_to(destination.with_suffix(destination.suffix + ".pre-migration.bak"))
            finally: previous.close()
        source_store = MemoryStore(source)
        try: result = source_store.backup_to(destination)
        finally: source_store.close()
        output({"migrated": True, "previous_destination_backup": existing_backup, **result})
        return
    store = MemoryStore(args.db)
    try:
        if args.command == "serve":
            token = args.token or os.environ.get("CONTEXT_MEMORY_TOKEN")
            server = MCPServer(store, args.tool_profile)
            server.serve_stdio() if args.transport == "stdio" else server.serve_http(args.host, args.port, token)
        elif args.command == "init":
            clients = [x.strip() for x in args.clients.split(",") if x.strip()] if args.clients else [args.client or "generic"]
            output(init_workspaces(store, args.workspace, clients, args.launcher, args.register, args.package))
        elif args.command == "doctor": output(doctor(store))
        elif args.command == "project-create": output(store.create_project(args.slug, args.name, args.description))
        elif args.command == "project-list": output(store.list_projects())
        elif args.command == "event": output(store.record_event(args.project_id, args.kind, args.content, session_id=args.session_id, idempotency_key=args.key))
        elif args.command == "checkpoint": output(store.create_checkpoint(
            args.project_id, args.mode, args.reason, args.goal, args.key, args.session_id, args.scope_id,
            args.completed, args.next_step, args.blocker, args.source_event_cursor, args.context_usage,
            args.repository_path, [json.loads(value) for value in args.test_result], args.verified_event,
            args.handoff_title, args.handoff_content, args.previous_handoff_memory_id, args.commit))
        elif args.command == "checkpoint-evaluate": output(store.evaluate_checkpoint(
            args.project_id, args.context_usage, args.session_id, args.repository_path,
            args.goal, args.completed, args.next_step, args.blocker))
        elif args.command == "events-since": output(store.read_events_since(args.project_id, args.cursor, args.kind, args.scope_id, args.limit))
        elif args.command == "memory": output(store.upsert_memory(args.project_id, args.title, args.content, args.type, args.status, args.confidence, args.importance, source_event_ids=args.source))
        elif args.command == "search": output(store.search(args.project_id, args.query, args.limit))
        elif args.command == "context": output(store.get_context(args.project_id, args.query, args.budget, event_cursor=args.event_cursor,
                                                                   event_kinds=args.event_kind, event_limit=args.event_limit,
                                                                   event_char_budget=args.event_budget))
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
        elif args.command == "policy":
            changes = {"max_context_chars":args.max_context_chars,"max_context_items":args.max_context_items,
                       "audit_keep_entries":args.audit_keep_entries,"terminal_memory_days":args.terminal_memory_days,
                       "checkpoint_soft_usage":args.checkpoint_soft_usage,"checkpoint_hard_usage":args.checkpoint_hard_usage,
                       "checkpoint_elapsed_seconds":args.checkpoint_elapsed_seconds,"checkpoint_event_count":args.checkpoint_event_count,
                       "checkpoint_max_age_seconds":args.checkpoint_max_age_seconds,
                       "checkpoint_cooldown_seconds":args.checkpoint_cooldown_seconds,"checkpoint_hysteresis":args.checkpoint_hysteresis,
                       "maintenance_interval_seconds":args.maintenance_interval_seconds}
            output(store.set_policy(args.project_id, **changes) if any(value is not None for value in changes.values()) else store.get_policy(args.project_id))
        elif args.command == "maintain":
            if args.scheduled and not args.apply: p.error("--scheduled requires --apply")
            output(store.maintain_scheduled(args.project_id) if args.scheduled else store.maintain(args.project_id, args.apply))
        elif args.command == "status": output(store.maintenance_status(args.project_id))
        elif args.command == "audit-export":
            destination = Path(args.output).expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            bundle = store.export_audit_chain(args.project_id)
            destination.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            output({"ok":True,"project_id":args.project_id,"output":str(destination),
                    "head_digest":bundle["head_digest"],"checkpoints":len(bundle["checkpoints"]),
                    "audit_entries":len(bundle["audit_entries"])})
        elif args.command == "audit-verify":
            source_path = Path(args.input).expanduser().resolve()
            result = store.verify_audit_chain(json.loads(source_path.read_text(encoding="utf-8")), args.expected_head_digest)
            output(result)
            if not result["ok"]: raise SystemExit(1)
        elif args.command == "backup":
            passphrase = os.environ.get(args.passphrase_env) if args.passphrase_env else None
            if args.passphrase_env and passphrase is None: p.error(f"passphrase environment variable is not set: {args.passphrase_env}")
            output(store.backup_to(args.output, passphrase))
        elif args.command == "backup-decrypt":
            passphrase = os.environ.get(args.passphrase_env)
            if passphrase is None: p.error(f"passphrase environment variable is not set: {args.passphrase_env}")
            from .backup_crypto import decrypt_file
            source, destination = Path(args.input).expanduser().resolve(), Path(args.output).expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            try:
                result = decrypt_file(source, temporary, passphrase); os.chmod(temporary, 0o600); os.replace(temporary, destination)
            finally: temporary.unlink(missing_ok=True)
            output({**result,"input":str(source),"output":str(destination)})
    finally:
        store.close()


if __name__ == "__main__":
    main()
