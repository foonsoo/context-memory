from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from .contracts import PROMOTABLE_EVENT_KINDS, workflow_guide
from .mcp import MCPServer
from .store import MemoryStore


def default_db() -> str:
    return os.environ.get(
        "CONTEXT_MEMORY_DB",
        str(Path.home() / ".local/share/context-memory/memory.db"),
    )


def output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def mcp_config(
    db_path: str,
    launcher: str = "uvx",
    package: str = "context-memory-mcp",
) -> dict[str, object]:
    """Return a portable stdio MCP definition for common clients."""
    if launcher == "uvx":
        if package.startswith("git+") and not re.search(
            r"@[0-9a-fA-F]{40}(?:#|$)", package
        ):
            raise ValueError(
                "Git uvx sources must be pinned to a full 40-character "
                "commit SHA"
            )
        prefix = ["--from", package, "context-memory"]
        command, args = (
            "uvx",
            [*prefix, "--db", db_path, "serve", "--transport", "stdio"],
        )
    elif launcher == "installed":
        installed = shutil.which("context-memory")
        invoked = (
            Path(sys.argv[0]).expanduser().resolve()
            if Path(sys.argv[0]).name == "context-memory"
            else None
        )
        command = (
            str(invoked)
            if invoked and invoked.is_file()
            else (installed or "context-memory")
        )
        args = ["--db", db_path, "serve", "--transport", "stdio"]
    else:
        command, args = (
            sys.executable,
            [
                "-m",
                "context_memory.cli",
                "--db",
                db_path,
                "serve",
                "--transport",
                "stdio",
            ],
        )
    return {"type": "stdio", "command": command, "args": args}


def _client_command(client: str) -> str | None:
    names = {
        "claude-code": ["claude"],
        "codex": ["codex"],
        "vscode": ["code"],
        "craft": ["craft", "craft-agents"],
        "cursor": ["cursor"],
    }
    for name in names.get(client, []):
        if found := shutil.which(name):
            return found
    mac_apps = {
        "vscode": (
            "/Applications/Visual Studio Code.app/Contents/Resources/"
            "app/bin/code"
        ),
        "cursor": "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
        "craft": "/Applications/Craft Agents.app/Contents/MacOS/Craft Agents",
    }
    candidate = mac_apps.get(client)
    return candidate if candidate and Path(candidate).exists() else None


def detect_clients() -> list[str]:
    return [
        name
        for name in ("claude-code", "codex", "cursor", "vscode", "craft")
        if _client_command(name)
    ]


def _write_cursor_global(
    config: dict[str, object], target: Path | None = None
) -> dict[str, object]:
    path = target or Path.home() / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    current: dict[str, object] = {}
    backup = None
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            raise ValueError(
                f"Cursor MCP config must be a JSON object: {path}"
            )
        backup = path.with_suffix(".json.bak")
        index = 1
        while backup.exists():
            backup = path.with_suffix(f".json.bak.{index}")
            index += 1
        shutil.copy2(path, backup)
    servers = current.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"Cursor mcpServers must be a JSON object: {path}")
    servers["context-memory"] = {
        k: v for k, v in config.items() if k in {"command", "args", "env"}
    }
    fd, temporary = tempfile.mkstemp(
        prefix=".mcp-", suffix=".json", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(current, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {
        "config_path": str(path),
        "backup": str(backup) if backup else None,
    }


def _register_client(
    client: str,
    config: dict[str, object],
    root: str,
    register: bool,
    cursor_config: Path | None = None,
) -> dict[str, object]:
    command, args = str(config["command"]), [str(x) for x in config["args"]]
    detected = bool(_client_command(client))
    result: dict[str, object] = {
        "client": client,
        "detected": detected,
        "registered": False,
    }
    if client == "claude-code":
        definition = json.dumps(
            config, ensure_ascii=False, separators=(",", ":")
        )
        register_command = [
            "claude",
            "mcp",
            "add-json",
            "--scope",
            "user",
            "context-memory",
            definition,
        ]
    elif client == "codex":
        register_command = [
            "codex",
            "mcp",
            "add",
            "context_memory",
            "--",
            command,
            *args,
        ]
    elif client == "vscode":
        executable = _client_command(client) or "code"
        definition = json.dumps(
            {"name": "context-memory", "command": command, "args": args},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        register_command = [executable, "--add-mcp", definition]
    elif client == "cursor":
        result["config_path"] = str(
            cursor_config or Path.home() / ".cursor" / "mcp.json"
        )
        if register:
            result.update(_write_cursor_global(config, cursor_config))
            result["registered"] = True
            result["status"] = "registered"
        else:
            result["status"] = "planned"
        return result
    elif client == "craft":
        result.update(
            {
                "status": "manual",
                "next_step": (
                    "In Craft Agents, add a local workspace source named "
                    "context-memory using mcp_json, then install "
                    "guide_template.content as "
                    "sources/context-memory/guide.md. "
                    "Craft Agents 0.10.0 was locally verified to require "
                    "reading a source guide before its first API call; "
                    "confirm "
                    "this behavior again for other installed versions."
                ),
                "mcp_json": config,
                "guide_template": {
                    "filename": "guide.md",
                    "content": workflow_guide(),
                },
                "promotable_event_kinds": list(PROMOTABLE_EVENT_KINDS),
            }
        )
        return result
    elif client == "generic":
        result.update(
            {
                "status": "manual",
                "next_step": (
                    "Add the provided MCP JSON to the client's user-level "
                    "server configuration."
                ),
                "mcp_json": config,
            }
        )
        return result
    else:
        raise ValueError(f"unsupported client: {client}")
    result["register_command"] = register_command
    if register:
        executable = _client_command(client)
        if not executable:
            result.update(
                {
                    "status": "unavailable",
                    "error": f"{client} executable was not found",
                }
            )
            return result
        register_command[0] = executable
        try:
            subprocess.run(
                register_command,
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            result.update({"registered": True, "status": "registered"})
        except subprocess.CalledProcessError as exc:
            result.update(
                {
                    "status": "failed",
                    "error": (exc.stderr or exc.stdout or str(exc)).strip(),
                }
            )
    else:
        result["status"] = "planned"
    return result


def _safe_register_client(
    client: str,
    config: dict[str, object],
    root: str,
    register: bool,
    cursor_config: Path | None,
) -> dict[str, object]:
    try:
        return _register_client(client, config, root, register, cursor_config)
    except Exception as exc:
        return {
            "client": client,
            "detected": bool(_client_command(client)),
            "registered": False,
            "status": "failed",
            "error": str(exc),
        }


def _remove_cursor_global(
    apply: bool, target: Path | None = None
) -> dict[str, object]:
    path = target or Path.home() / ".cursor" / "mcp.json"
    result: dict[str, object] = {
        "config_path": str(path),
        "removed": False,
    }
    if not path.exists():
        return {**result, "status": "not_registered"}
    current = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(current, dict):
        raise ValueError(f"Cursor MCP config must be a JSON object: {path}")
    servers = current.get("mcpServers")
    if not isinstance(servers, dict):
        if servers is None:
            return {**result, "status": "not_registered"}
        raise ValueError(f"Cursor mcpServers must be a JSON object: {path}")
    if "context-memory" not in servers:
        return {**result, "status": "not_registered"}
    if not apply:
        return {**result, "status": "planned"}

    backup = path.with_suffix(".json.bak")
    index = 1
    while backup.exists():
        backup = path.with_suffix(f".json.bak.{index}")
        index += 1
    shutil.copy2(path, backup)
    del servers["context-memory"]
    fd, temporary = tempfile.mkstemp(
        prefix=".mcp-", suffix=".json", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(current, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {
        **result,
        "removed": True,
        "status": "removed",
        "backup": str(backup),
    }


def _unregister_client(
    client: str,
    root: str,
    apply: bool,
    cursor_config: Path | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "client": client,
        "detected": bool(_client_command(client)),
        "removed": False,
    }
    if client == "claude-code":
        remove_command = ["claude", "mcp", "remove", "context-memory"]
    elif client == "codex":
        remove_command = ["codex", "mcp", "remove", "context_memory"]
    elif client == "cursor":
        return {
            **result,
            **_remove_cursor_global(apply, cursor_config),
        }
    elif client == "vscode":
        return {
            **result,
            "status": "manual",
            "next_step": (
                "In VS Code, run 'MCP: List Servers', select "
                "context-memory, and choose Uninstall."
            ),
        }
    elif client == "craft":
        return {
            **result,
            "status": "manual",
            "next_step": (
                "Remove the context-memory workspace source and its "
                "sources/context-memory/guide.md file in Craft Agents."
            ),
        }
    elif client == "generic":
        return {
            **result,
            "status": "manual",
            "next_step": (
                "Remove context-memory from the client's user-level MCP "
                "server configuration."
            ),
        }
    else:
        raise ValueError(f"unsupported client: {client}")

    result["remove_command"] = remove_command
    if not apply:
        return {**result, "status": "planned"}
    executable = _client_command(client)
    if not executable:
        return {
            **result,
            "status": "unavailable",
            "error": f"{client} executable was not found",
        }
    remove_command[0] = executable
    try:
        subprocess.run(
            remove_command,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        return {
            **result,
            "status": "failed",
            "error": (exc.stderr or exc.stdout or str(exc)).strip(),
        }
    return {**result, "removed": True, "status": "removed"}


def cleanup_clients(
    clients: list[str],
    root: str | Path,
    apply: bool,
    cursor_config: Path | None = None,
) -> dict[str, object]:
    """Plan or remove only Context Memory client registrations."""
    expanded = (
        ["claude-code", "codex", "cursor", "vscode", "craft"]
        if clients == ["auto"]
        else clients
    )
    if not expanded:
        expanded = ["generic"]
    supported = {
        "generic",
        "claude-code",
        "codex",
        "cursor",
        "vscode",
        "craft",
    }
    invalid = sorted(set(expanded) - supported)
    if invalid:
        raise ValueError("unsupported clients: " + ", ".join(invalid))
    resolved_root = str(Path(root).expanduser().resolve())
    results = []
    for client in dict.fromkeys(expanded):
        try:
            item = _unregister_client(
                client, resolved_root, apply, cursor_config
            )
        except Exception as exc:
            item = {
                "client": client,
                "removed": False,
                "status": "failed",
                "error": str(exc),
            }
        results.append(item)
    return {
        "applied": apply,
        "root": resolved_root,
        "clients": results,
        "restart_required": any(item["removed"] for item in results),
    }


def init_workspaces(
    store: MemoryStore,
    workspace: str,
    clients: list[str],
    launcher: str,
    register: bool,
    package: str = "context-memory-mcp",
    cursor_config: Path | None = None,
) -> dict[str, object]:
    root = str(Path(workspace).expanduser().resolve())
    resolved = store.resolve_project(root)
    config = mcp_config(str(store.path), launcher, package)
    expanded = detect_clients() if clients == ["auto"] else clients
    if not expanded:
        expanded = ["generic"]
    invalid = sorted(
        set(expanded)
        - {"generic", "claude-code", "codex", "cursor", "vscode", "craft"}
    )
    if invalid:
        raise ValueError("unsupported clients: " + ", ".join(invalid))
    result: dict[str, object] = {
        "ready": True,
        "database": str(store.path),
        "workspace": root,
        "project": resolved["project"],
        "scope_id": resolved["scope_id"],
        "mcp": {"mcpServers": {"context-memory": config}},
        "workflow": [
            "context_bootstrap(cwd, focused query, client, external_id, "
            "char_budget=4000..8000, response_format=compact)",
            "record_event -> memory_upsert(source_event_ids) for durable "
            "verified knowledge",
            "session_end(session_id)",
        ],
        "workflow_contract": workflow_guide(),
        "promotable_event_kinds": list(PROMOTABLE_EVENT_KINDS),
        "clients": [
            _safe_register_client(
                client, config, root, register, cursor_config
            )
            for client in dict.fromkeys(expanded)
        ],
    }
    registered = any(item.get("registered") for item in result["clients"])
    result["restart_required"] = registered
    if registered:
        result["next_step"] = (
            "Restart every client reported as registered, then verify "
            "context-memory in that client's MCP server list."
        )
    return result


def init_workspace(
    store: MemoryStore,
    workspace: str,
    client: str,
    launcher: str,
    register: bool,
) -> dict[str, object]:
    """Backward-compatible single-client wrapper."""
    result = init_workspaces(store, workspace, [client], launcher, register)
    adapter = result["clients"][0]
    for key in (
        "register_command",
        "registered",
        "next_step",
        "status",
        "error",
    ):
        if key in adapter:
            result[key] = adapter[key]
    return result


def doctor(store: MemoryStore) -> dict[str, object]:
    fts5 = bool(
        store.conn.execute(
            "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
        ).fetchone()[0]
    )
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


def erase_database(
    database: str | Path,
    backup: str | Path,
    confirmation: str,
) -> dict[str, object]:
    """Back up and remove the complete local authoritative database."""
    database_path = Path(database).expanduser().resolve()
    backup_path = Path(backup).expanduser().resolve()
    if confirmation != str(database_path):
        raise ValueError(
            "confirmation must exactly match the resolved database path: "
            f"{database_path}"
        )
    if not database_path.is_file():
        raise FileNotFoundError(f"database does not exist: {database_path}")
    if backup_path == database_path:
        raise ValueError("backup output must differ from the live database")

    store = MemoryStore(database_path)
    try:
        backup_result = store.backup_to(backup_path)
    finally:
        store.close()
    if not backup_result["ok"] or backup_result["integrity"] != "ok":
        raise RuntimeError("verified backup is required before erasure")

    removed = []
    for path in (
        database_path,
        database_path.with_name(database_path.name + "-wal"),
        database_path.with_name(database_path.name + "-shm"),
    ):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return {
        "erased": True,
        "database": str(database_path),
        "removed": removed,
        "backup": backup_result,
        "recoverable": True,
    }


def restore_database(
    source: str | Path,
    database: str | Path,
    *,
    replace: bool = False,
    backup_existing: str | Path | None = None,
    confirmation: str | None = None,
) -> dict[str, object]:
    """Restore a checked snapshot to the authoritative path."""
    source_path = Path(source).expanduser().resolve()
    database_path = Path(database).expanduser().resolve()
    if source_path == database_path:
        raise ValueError("restore source and destination must differ")
    if not source_path.is_file():
        raise FileNotFoundError(
            f"restore source does not exist: {source_path}"
        )
    if database_path.exists() and not replace:
        raise ValueError(
            "restore destination exists; use --replace with an existing "
            "database backup and exact path confirmation"
        )
    if replace:
        if not database_path.is_file():
            raise ValueError("--replace requires an existing database")
        if backup_existing is None:
            raise ValueError("--replace requires --backup-existing")
        if confirmation != str(database_path):
            raise ValueError(
                "confirmation must exactly match the resolved database path: "
                f"{database_path}"
            )
    elif backup_existing is not None or confirmation is not None:
        raise ValueError(
            "--backup-existing and --confirm are only valid with --replace"
        )

    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = database_path.with_name(
        f".{database_path.name}.{os.getpid()}.restore.tmp"
    )
    try:
        source_connection = sqlite3.connect(
            f"{source_path.as_uri()}?mode=ro", uri=True
        )
        target_connection = sqlite3.connect(temporary)
        try:
            source_integrity = source_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            if source_integrity != "ok":
                raise RuntimeError(
                    "restore source integrity check failed: "
                    f"{source_integrity}"
                )
            source_connection.backup(target_connection)
            restored_integrity = target_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            if restored_integrity != "ok":
                raise RuntimeError(
                    "restored database integrity check failed: "
                    f"{restored_integrity}"
                )
        finally:
            target_connection.close()
            source_connection.close()

        preflight = MemoryStore(temporary)
        try:
            preflight_verification = doctor(preflight)
        finally:
            preflight.close()
        if not preflight_verification["ok"]:
            raise RuntimeError("restored database failed doctor verification")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    existing_backup = None
    try:
        if replace:
            existing = MemoryStore(database_path)
            try:
                existing_backup = existing.backup_to(backup_existing)
            finally:
                existing.close()
        os.chmod(temporary, 0o600)
        for suffix in ("-wal", "-shm"):
            database_path.with_name(database_path.name + suffix).unlink(
                missing_ok=True
            )
        os.replace(temporary, database_path)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "restored": True,
        "source": str(source_path),
        "database": str(database_path),
        "replaced": replace,
        "previous_database_backup": existing_backup,
        "verification": preflight_verification,
    }


CommandHandler = Callable[[MemoryStore, argparse.Namespace], object]


def _event_command(store: MemoryStore, args: argparse.Namespace) -> object:
    return store.record_event(
        args.project_id,
        args.kind,
        args.content,
        session_id=args.session_id,
        idempotency_key=args.key,
    )


def _checkpoint_command(
    store: MemoryStore, args: argparse.Namespace
) -> object:
    return store.create_checkpoint(
        args.project_id,
        args.mode,
        args.reason,
        args.goal,
        args.key,
        args.session_id,
        args.scope_id,
        args.completed,
        args.next_step,
        args.blocker,
        args.source_event_cursor,
        args.context_usage,
        args.repository_path,
        [json.loads(value) for value in args.test_result],
        args.verified_event,
        args.handoff_title,
        args.handoff_content,
        args.previous_handoff_memory_id,
        args.commit,
    )


def _memory_command(store: MemoryStore, args: argparse.Namespace) -> object:
    return store.upsert_memory(
        args.project_id,
        args.title,
        args.content,
        args.type,
        args.status,
        args.confidence,
        args.importance,
        source_event_ids=args.source,
    )


def _context_command(store: MemoryStore, args: argparse.Namespace) -> object:
    return store.get_context(
        args.project_id,
        args.query,
        args.budget,
        event_cursor=args.event_cursor,
        event_kinds=args.event_kind,
        event_limit=args.event_limit,
        event_char_budget=args.event_budget,
    )


COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "doctor": lambda store, args: doctor(store),
    "project-create": lambda store, args: store.create_project(
        args.slug, args.name, args.description
    ),
    "project-list": lambda store, args: store.list_projects(),
    "event": _event_command,
    "checkpoint": _checkpoint_command,
    "checkpoint-evaluate": lambda store, args: store.evaluate_checkpoint(
        args.project_id,
        args.context_usage,
        args.session_id,
        args.repository_path,
        args.goal,
        args.completed,
        args.next_step,
        args.blocker,
    ),
    "events-since": lambda store, args: store.read_events_since(
        args.project_id,
        args.cursor,
        args.kind,
        args.scope_id,
        args.limit,
    ),
    "event-poll": lambda store, args: store.poll_events(
        args.project_id,
        args.consumer_id,
        args.kind,
        args.scope_id,
        args.limit,
    ),
    "event-ack": lambda store, args: store.acknowledge_events(
        args.project_id,
        args.consumer_id,
        args.cursor,
        args.kind,
        args.scope_id,
    ),
    "memory": _memory_command,
    "search": lambda store, args: store.search(
        args.project_id, args.query, args.limit
    ),
    "context": _context_command,
    "source": lambda store, args: store.get_source(args.event_id),
    "review-list": lambda store, args: store.review_queue(args.project_id),
    "review-action": lambda store, args: store.review_candidate(
        args.memory_id,
        args.action,
        args.related_memory_id,
        args.note,
    ),
    "memory-correct": lambda store, args: store.propose_correction(
        args.project_id,
        args.memory_id,
        args.content,
        args.title,
    ),
    "memory-transition": lambda store, args: store.transition(
        args.memory_id,
        args.status,
        args.related_memory_id,
        args.note,
    ),
    "repair": lambda store, args: store.rebuild_fts(args.project_id),
    "status": lambda store, args: store.maintenance_status(args.project_id),
}


def _run_serve(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    token = args.token or os.environ.get("CONTEXT_MEMORY_TOKEN")
    server = MCPServer(store, args.tool_profile)
    if args.transport == "stdio":
        server.serve_stdio()
    else:
        server.serve_http(args.host, args.port, token)


def _run_init(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    clients = (
        [x.strip() for x in args.clients.split(",") if x.strip()]
        if args.clients
        else [args.client or "generic"]
    )
    output(
        init_workspaces(
            store,
            args.workspace,
            clients,
            args.launcher,
            args.register,
            args.package,
        )
    )


def _run_unregister(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    clients = (
        [x.strip() for x in args.clients.split(",") if x.strip()]
        if args.clients
        else [args.client or "auto"]
    )
    output(cleanup_clients(clients, args.workspace, args.apply))


def _run_export(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    destination = Path(args.output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    records = store.export_project(args.project_id)
    with destination.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    output(
        {
            "ok": True,
            "project_id": args.project_id,
            "output": str(destination),
            "records": len(records),
        }
    )


def _run_import(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    source_path = Path(args.input).expanduser().resolve()
    records = [
        json.loads(line)
        for line in source_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    output({"ok": True, **store.import_project(records)})


def _run_policy(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    changes = {
        "max_context_chars": args.max_context_chars,
        "max_context_items": args.max_context_items,
        "audit_keep_entries": args.audit_keep_entries,
        "terminal_memory_days": args.terminal_memory_days,
        "checkpoint_soft_usage": args.checkpoint_soft_usage,
        "checkpoint_hard_usage": args.checkpoint_hard_usage,
        "checkpoint_elapsed_seconds": args.checkpoint_elapsed_seconds,
        "checkpoint_event_count": args.checkpoint_event_count,
        "checkpoint_max_age_seconds": args.checkpoint_max_age_seconds,
        "checkpoint_cooldown_seconds": (args.checkpoint_cooldown_seconds),
        "checkpoint_hysteresis": args.checkpoint_hysteresis,
        "maintenance_interval_seconds": (args.maintenance_interval_seconds),
        "message_ttl_seconds": args.message_ttl_seconds,
    }
    output(
        store.set_policy(args.project_id, **changes)
        if any(value is not None for value in changes.values())
        else store.get_policy(args.project_id)
    )


def _run_maintain(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    if args.scheduled and not args.apply:
        p.error("--scheduled requires --apply")
    output(
        store.maintain_scheduled(args.project_id)
        if args.scheduled
        else store.maintain(args.project_id, args.apply)
    )


def _run_audit_export(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    destination = Path(args.output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    bundle = store.export_audit_chain(args.project_id)
    destination.write_text(
        json.dumps(
            bundle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    output(
        {
            "ok": True,
            "project_id": args.project_id,
            "output": str(destination),
            "head_digest": bundle["head_digest"],
            "checkpoints": len(bundle["checkpoints"]),
            "audit_entries": len(bundle["audit_entries"]),
        }
    )


def _run_audit_verify(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    source_path = Path(args.input).expanduser().resolve()
    result = store.verify_audit_chain(
        json.loads(source_path.read_text(encoding="utf-8")),
        args.expected_head_digest,
    )
    output(result)
    if not result["ok"]:
        raise SystemExit(1)


def _run_audit_anchor_sign(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    from .audit_anchor import create_anchor

    source_path = Path(args.input).expanduser().resolve()
    bundle = json.loads(source_path.read_text(encoding="utf-8"))
    chain = store.verify_audit_chain(bundle)
    if not chain["ok"] or not chain["head_digest"]:
        p.error("audit bundle must contain a valid checkpoint chain")
    secret = os.environ.get(args.private_key_env)
    if secret is None:
        p.error(
            "private key environment variable is not set: "
            f"{args.private_key_env}"
        )
    anchor = create_anchor(chain["project_id"], chain["head_digest"], secret)
    destination = Path(args.output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            anchor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    output(
        {
            "ok": True,
            "output": str(destination),
            "project_id": anchor["project_id"],
            "head_digest": anchor["head_digest"],
            "public_key": anchor["public_key"],
        }
    )


def _run_audit_anchor_verify(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    from .audit_anchor import verify_anchor

    anchor = json.loads(
        Path(args.input).expanduser().resolve().read_text(encoding="utf-8")
    )
    result = verify_anchor(
        anchor, args.expected_project_id, args.expected_public_key
    )
    if result["ok"] and args.audit_bundle:
        bundle = json.loads(
            Path(args.audit_bundle)
            .expanduser()
            .resolve()
            .read_text(encoding="utf-8")
        )
        chain = store.verify_audit_chain(bundle, result["head_digest"])
        result["audit_chain"] = chain
        if not chain["ok"] or chain["project_id"] != result["project_id"]:
            result["ok"] = False
            result["errors"].append(
                "audit bundle does not match the signed anchor"
            )
    output(result)
    if not result["ok"]:
        raise SystemExit(1)


def _run_backup(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    passphrase = (
        os.environ.get(args.passphrase_env) if args.passphrase_env else None
    )
    if args.passphrase_env and passphrase is None:
        p.error(
            "passphrase environment variable is not set: "
            f"{args.passphrase_env}"
        )
    output(store.backup_to(args.output, passphrase))


def _run_backup_decrypt(
    store: MemoryStore, args: argparse.Namespace, p: argparse.ArgumentParser
) -> None:
    passphrase = os.environ.get(args.passphrase_env)
    if passphrase is None:
        p.error(
            "passphrase environment variable is not set: "
            f"{args.passphrase_env}"
        )
    from .backup_crypto import decrypt_file

    source, destination = (
        Path(args.input).expanduser().resolve(),
        Path(args.output).expanduser().resolve(),
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        result = decrypt_file(source, temporary, passphrase)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    output({**result, "input": str(source), "output": str(destination)})


RUNTIME_COMMAND_HANDLERS = {
    "serve": _run_serve,
    "init": _run_init,
    "unregister": _run_unregister,
    "export": _run_export,
    "import": _run_import,
    "policy": _run_policy,
    "maintain": _run_maintain,
    "audit-export": _run_audit_export,
    "audit-verify": _run_audit_verify,
    "audit-anchor-sign": _run_audit_anchor_sign,
    "audit-anchor-verify": _run_audit_anchor_verify,
    "backup": _run_backup,
    "backup-decrypt": _run_backup_decrypt,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="context-memory", description="Local-first context memory"
    )
    p.add_argument("--db", default=default_db(), help="SQLite database path")
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--token")
    serve.add_argument(
        "--tool-profile",
        choices=["core", "admin", "all"],
        default="core",
        help=(
            "Expose the compact working set, administrative tools, or every "
            "tool"
        ),
    )
    init = sub.add_parser(
        "init",
        help=(
            "Initialize a workspace and print/register portable MCP "
            "configuration"
        ),
    )
    init.add_argument("--workspace", default=os.getcwd())
    client_group = init.add_mutually_exclusive_group()
    client_group.add_argument(
        "--client",
        choices=[
            "generic",
            "claude-code",
            "codex",
            "cursor",
            "vscode",
            "craft",
        ],
        help="Single-client compatibility option",
    )
    client_group.add_argument(
        "--clients",
        help=(
            "Comma-separated clients or 'auto' "
            "(claude-code,codex,cursor,vscode,craft)"
        ),
    )
    init.add_argument(
        "--launcher", choices=["uvx", "installed", "python"], default="uvx"
    )
    init.add_argument(
        "--package",
        default="context-memory-mcp",
        help="uvx package or git+ URL pinned to a full commit SHA",
    )
    init.add_argument("--register", action="store_true")
    unregister = sub.add_parser(
        "unregister",
        help="Plan or remove Context Memory client registrations",
    )
    unregister.add_argument("--workspace", default=os.getcwd())
    unregister_group = unregister.add_mutually_exclusive_group()
    unregister_group.add_argument(
        "--client",
        choices=[
            "generic",
            "claude-code",
            "codex",
            "cursor",
            "vscode",
            "craft",
        ],
        help="Single client to clean up",
    )
    unregister_group.add_argument(
        "--clients",
        help=(
            "Comma-separated clients or 'auto' "
            "(claude-code,codex,cursor,vscode,craft)"
        ),
    )
    unregister.add_argument(
        "--apply",
        action="store_true",
        help="Apply removals; the default only prints the cleanup plan",
    )
    sub.add_parser(
        "doctor", help="Check the database, FTS5, and local permissions"
    )
    project = sub.add_parser("project-create")
    project.add_argument("slug")
    project.add_argument("--name")
    project.add_argument("--description", default="")
    sub.add_parser("project-list")
    event = sub.add_parser("event")
    event.add_argument("project_id")
    event.add_argument("kind")
    event.add_argument("content")
    event.add_argument("--session-id")
    event.add_argument("--key")
    checkpoint = sub.add_parser(
        "checkpoint", help="Record an explicit idempotent recovery checkpoint"
    )
    checkpoint.add_argument("project_id")
    checkpoint.add_argument("mode", choices=["interim", "final"])
    checkpoint.add_argument(
        "reason",
        choices=[
            "context_budget",
            "elapsed",
            "material_change",
            "completed",
            "manual",
        ],
    )
    checkpoint.add_argument("--goal", required=True)
    checkpoint.add_argument("--key", required=True)
    checkpoint.add_argument("--session-id")
    checkpoint.add_argument("--scope-id")
    checkpoint.add_argument("--completed", action="append", default=[])
    checkpoint.add_argument("--next-step")
    checkpoint.add_argument("--blocker", action="append", default=[])
    checkpoint.add_argument("--source-event-cursor", type=int)
    checkpoint.add_argument("--context-usage", type=float)
    checkpoint.add_argument("--repository", dest="repository_path")
    checkpoint.add_argument(
        "--test-result",
        action="append",
        default=[],
        help="JSON object with name, status, and optional command/details",
    )
    checkpoint.add_argument("--verified-event", action="append", default=[])
    checkpoint.add_argument("--handoff-title")
    checkpoint.add_argument("--handoff-content")
    checkpoint.add_argument("--previous-handoff-memory-id")
    checkpoint.add_argument("--commit")
    checkpoint_eval = sub.add_parser(
        "checkpoint-evaluate",
        help="Evaluate checkpoint thresholds without writing",
    )
    checkpoint_eval.add_argument("project_id")
    checkpoint_eval.add_argument("--context-usage", type=float)
    checkpoint_eval.add_argument("--session-id")
    checkpoint_eval.add_argument("--repository", dest="repository_path")
    checkpoint_eval.add_argument("--goal", default="")
    checkpoint_eval.add_argument("--completed", action="append", default=[])
    checkpoint_eval.add_argument("--next-step")
    checkpoint_eval.add_argument("--blocker", action="append", default=[])
    events_since = sub.add_parser(
        "events-since", help="Read immutable project events after a cursor"
    )
    events_since.add_argument("project_id")
    events_since.add_argument("--cursor", type=int, default=0)
    events_since.add_argument("--kind", action="append")
    events_since.add_argument("--scope-id")
    events_since.add_argument("--limit", type=int, default=100)
    event_poll = sub.add_parser(
        "event-poll", help="Poll from a durable per-consumer event receipt"
    )
    event_poll.add_argument("project_id")
    event_poll.add_argument("consumer_id")
    event_poll.add_argument("--kind", action="append")
    event_poll.add_argument("--scope-id")
    event_poll.add_argument("--limit", type=int, default=100)
    event_ack = sub.add_parser(
        "event-ack", help="Acknowledge a previously delivered event cursor"
    )
    event_ack.add_argument("project_id")
    event_ack.add_argument("consumer_id")
    event_ack.add_argument("cursor", type=int)
    event_ack.add_argument("--kind", action="append")
    event_ack.add_argument("--scope-id")
    memory = sub.add_parser("memory")
    memory.add_argument("project_id")
    memory.add_argument("title")
    memory.add_argument("content")
    memory.add_argument("--type", default="other")
    memory.add_argument("--status", default="proposed")
    memory.add_argument("--source", action="append", default=[])
    memory.add_argument("--confidence", type=float, default=0.5)
    memory.add_argument("--importance", type=float, default=0.5)
    search = sub.add_parser("search")
    search.add_argument("project_id")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    context = sub.add_parser("context")
    context.add_argument("project_id")
    context.add_argument("query")
    context.add_argument("--budget", type=int, default=6000)
    context.add_argument("--event-cursor", type=int)
    context.add_argument("--event-kind", action="append")
    context.add_argument("--event-limit", type=int, default=20)
    context.add_argument("--event-budget", type=int, default=2000)
    source = sub.add_parser("source")
    source.add_argument("event_id")
    review_list = sub.add_parser(
        "review-list",
        help="List proposed memories and actionable Wiki revisions",
    )
    review_list.add_argument("project_id")
    review_action = sub.add_parser(
        "review-action",
        help="Approve or reject a candidate, or supersede/dispute a memory",
    )
    review_action.add_argument("memory_id")
    review_action.add_argument(
        "action", choices=["approve", "reject", "supersede", "dispute"]
    )
    review_action.add_argument("--related-memory-id")
    review_action.add_argument("--note", default="")
    memory_correct = sub.add_parser(
        "memory-correct",
        help="Create an evidence-backed proposed correction for review",
    )
    memory_correct.add_argument("project_id")
    memory_correct.add_argument("memory_id")
    memory_correct.add_argument("content")
    memory_correct.add_argument("--title")
    memory_transition = sub.add_parser(
        "memory-transition",
        help="Explicitly change a memory lifecycle status",
    )
    memory_transition.add_argument("memory_id")
    memory_transition.add_argument(
        "status",
        choices=["active", "superseded", "disputed", "expired", "rejected"],
    )
    memory_transition.add_argument("--related-memory-id")
    memory_transition.add_argument("--note", default="")
    export = sub.add_parser(
        "export", help="Export one project as deterministic JSON Lines"
    )
    export.add_argument("project_id")
    export.add_argument("--output", required=True)
    import_cmd = sub.add_parser(
        "import",
        help=(
            "Restore a JSON Lines project export without overwriting existing "
            "data"
        ),
    )
    import_cmd.add_argument("input")
    repair = sub.add_parser(
        "repair",
        help=(
            "Rebuild the disposable FTS projection from authoritative memories"
        ),
    )
    repair.add_argument("--project-id")
    policy = sub.add_parser(
        "policy", help="Read or update project operational bounds"
    )
    policy.add_argument("project_id")
    policy.add_argument("--max-context-chars", type=int)
    policy.add_argument("--max-context-items", type=int)
    policy.add_argument("--audit-keep-entries", type=int)
    policy.add_argument("--terminal-memory-days", type=int)
    policy.add_argument("--checkpoint-soft-usage", type=float)
    policy.add_argument("--checkpoint-hard-usage", type=float)
    policy.add_argument("--checkpoint-elapsed-seconds", type=int)
    policy.add_argument("--checkpoint-event-count", type=int)
    policy.add_argument("--checkpoint-max-age-seconds", type=int)
    policy.add_argument("--checkpoint-cooldown-seconds", type=int)
    policy.add_argument("--checkpoint-hysteresis", type=float)
    policy.add_argument("--maintenance-interval-seconds", type=int)
    policy.add_argument("--message-ttl-seconds", type=int)
    maintain = sub.add_parser(
        "maintain",
        help=(
            "Plan/apply terminal-memory cleanup and checkpointed audit "
            "compaction"
        ),
    )
    maintain.add_argument("project_id")
    maintain.add_argument("--apply", action="store_true")
    maintain.add_argument("--scheduled", action="store_true")
    status_cmd = sub.add_parser(
        "status",
        help=(
            "Show project policy, storage counts, audit checkpoints, and "
            "search health"
        ),
    )
    status_cmd.add_argument("project_id")
    audit_export = sub.add_parser(
        "audit-export",
        help=(
            "Export a deterministic bundle for offline audit-chain "
            "verification"
        ),
    )
    audit_export.add_argument("project_id")
    audit_export.add_argument("--output", required=True)
    audit_verify = sub.add_parser(
        "audit-verify",
        help=(
            "Verify an exported audit chain without opening its source "
            "database"
        ),
    )
    audit_verify.add_argument("input")
    audit_verify.add_argument("--expected-head-digest")
    audit_sign = sub.add_parser(
        "audit-anchor-sign",
        help=(
            "Create a detached Ed25519 anchor for an audit bundle's head "
            "digest"
        ),
    )
    audit_sign.add_argument("input")
    audit_sign.add_argument("--output", required=True)
    audit_sign.add_argument("--private-key-env", required=True)
    audit_anchor_verify = sub.add_parser(
        "audit-anchor-verify",
        help="Verify a detached audit anchor and optionally its audit bundle",
    )
    audit_anchor_verify.add_argument("input")
    audit_anchor_verify.add_argument("--audit-bundle")
    audit_anchor_verify.add_argument("--expected-project-id")
    audit_anchor_verify.add_argument("--expected-public-key")
    backup = sub.add_parser(
        "backup",
        help="Create one consistent integrity-checked SQLite snapshot",
    )
    backup.add_argument("--output", required=True)
    backup.add_argument("--passphrase-env")
    decrypt = sub.add_parser(
        "backup-decrypt",
        help="Decrypt an authenticated backup envelope to a SQLite snapshot",
    )
    decrypt.add_argument("input")
    decrypt.add_argument("--output", required=True)
    decrypt.add_argument("--passphrase-env", required=True)
    migrate = sub.add_parser(
        "migrate-db",
        help=(
            "Safely migrate a live SQLite database with the Online Backup API"
        ),
    )
    migrate.add_argument("source")
    migrate.add_argument("--replace", action="store_true")
    erase = sub.add_parser(
        "erase-db",
        help="Back up and completely erase the local authoritative database",
    )
    erase.add_argument("--backup", required=True)
    erase.add_argument(
        "--confirm",
        required=True,
        help="Exact resolved database path to acknowledge complete erasure",
    )
    restore = sub.add_parser(
        "restore-db",
        help="Restore an integrity-checked SQLite snapshot",
    )
    restore.add_argument("source")
    restore.add_argument("--replace", action="store_true")
    restore.add_argument("--backup-existing")
    restore.add_argument(
        "--confirm",
        help="Exact resolved database path required with --replace",
    )
    return p


def main() -> None:
    p = build_parser()
    args = p.parse_args()
    if args.command == "erase-db":
        try:
            output(erase_database(args.db, args.backup, args.confirm))
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            p.error(str(exc))
        return
    if args.command == "restore-db":
        try:
            output(
                restore_database(
                    args.source,
                    args.db,
                    replace=args.replace,
                    backup_existing=args.backup_existing,
                    confirmation=args.confirm,
                )
            )
        except (
            FileNotFoundError,
            RuntimeError,
            sqlite3.DatabaseError,
            ValueError,
        ) as exc:
            p.error(str(exc))
        return
    if args.command == "migrate-db":
        source = Path(args.source).expanduser().resolve()
        destination = Path(args.db).expanduser().resolve()
        if source == destination:
            p.error("migration source and destination must differ")
        if not source.is_file():
            p.error(f"migration source does not exist: {source}")
        if destination.exists() and not args.replace:
            p.error(
                "migration destination exists; use --replace after backing "
                f"it up: {destination}"
            )
        existing_backup = None
        if destination.exists():
            previous = MemoryStore(destination)
            try:
                existing_backup = previous.backup_to(
                    destination.with_suffix(
                        destination.suffix + ".pre-migration.bak"
                    )
                )
            finally:
                previous.close()
        source_store = MemoryStore(source)
        try:
            result = source_store.backup_to(destination)
        finally:
            source_store.close()
        output(
            {
                "migrated": True,
                "previous_destination_backup": existing_backup,
                **result,
            }
        )
        return
    store = MemoryStore(args.db)
    try:
        handler = COMMAND_HANDLERS.get(args.command)
        if handler:
            output(handler(store, args))
        else:
            RUNTIME_COMMAND_HANDLERS[args.command](store, args, p)
    finally:
        store.close()


if __name__ == "__main__":
    main()
