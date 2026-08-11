# MCP client setup

All clients on one OS account should launch the same installed executable and the same local database. The portable server definition is:

```json
{"type":"stdio","command":"/absolute/path/to/context-memory","args":["--db","/absolute/path/to/memory.db","serve","--transport","stdio"]}
```

Run `context-memory init --launcher installed --clients claude-code,codex,cursor,vscode,craft --register` to register supported clients. Claude Code, Codex, and VS Code use their official CLIs. Cursor's global `~/.cursor/mcp.json` is merged atomically and backed up. Craft Agents is intentionally a guided workspace-source step. Generic MCP clients can use the JSON printed by `init`.

Every client must follow the same lifecycle, independently of hooks:

1. `project_resolve(cwd)` using the canonical current workspace as the preferred identity hint.
2. `session_start` with the returned project/scope, actual client name, and client session ID when available.
3. `get_context` using the user's request as a focused query and a 4,000–8,000 character budget. Global memory is merged, and an empty local result triggers bounded discovery across the shared project registry.
4. `record_event`, then `memory_upsert` with source provenance for durable knowledge.
5. `session_end` before finishing.

Use client rule files to carry that instruction: root `AGENTS.md` for Codex and compatible clients, [`examples/CLAUDE.md`](../examples/CLAUDE.md) for Claude Code, [`examples/cursor-context-memory.mdc`](../examples/cursor-context-memory.mdc) for Cursor project rules, and equivalent workspace instructions elsewhere. [`examples/mcp.json`](../examples/mcp.json) is a generic configuration template. `examples/hooks.json` is optional Codex automation, not part of the correctness contract.

## Moving an existing database

Never copy only a live `memory.db` while WAL mode is active. Install the new release first, stop avoidable old clients, and run:

```bash
/absolute/path/to/context-memory \
  --db ~/.local/share/context-memory/memory.db \
  migrate-db /old/path/memory.db
```

The command uses SQLite's Online Backup API, includes committed WAL pages, validates integrity, writes through a temporary file, and sets mode `0600`. It refuses to overwrite a destination. `--replace` first creates `memory.db.pre-migration.bak`; use it only after checking both paths. Run `doctor`, then re-register clients against the new database.
