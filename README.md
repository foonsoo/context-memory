# Context Memory

A small, client-neutral context memory server for any MCP agent, including Claude Code, Craft Agents, Codex, Cursor, VS Code, and local-model clients. It keeps immutable evidence separate from derived memories and the FTS search projection, so a generated summary never becomes the only source of truth.

This MVP is usable now: Python 3.11+ (Python 3.14 recommended), SQLite with WAL/FTS5, zero runtime dependencies, stdio MCP, local HTTP MCP, migrations, lifecycle hooks, and a standard-library test suite.

> **Sensitive-data warning:** this database is local, not encrypted. Do not record secrets, tokens, private keys, raw environment dumps, or unrelated personal data. The data directory is mode `0700`, but other processes running as your OS user and backups may still access it.

## Install

### Published package (recommended)

Initialize the current folder without cloning the repository or managing a virtual environment:

```bash
uvx context-memory init
```

The command creates the local SQLite database, maps the current workspace to a stable project and scope, and prints portable stdio MCP JSON. No project UUID needs to be copied. Register the same database with several clients on this computer in one command:

```bash
uvx context-memory init --clients claude-code,codex,cursor,vscode,craft --register
# Or register every detected supported client:
uvx context-memory init --clients auto --register
uvx context-memory doctor
```

`--register` is opt-in because it changes client configuration. Results are reported per client, so one missing client does not prevent other registrations. Claude Code uses user scope, Cursor is merged into `~/.cursor/mcp.json` with a backup, and VS Code uses its official `--add-mcp` CLI. Craft Agents remains a guided manual step because its documented sources are workspace-scoped. The older `--client NAME` form remains supported.

### Pin Git installs or avoid Git startup checks

Do not put an unpinned `git+https://...` source in an MCP launch command. Package runners may resolve the remote branch whenever a client starts the server. Pin the full commit SHA for reproducible, fast startup:

```bash
SOURCE='git+https://github.com/OWNER/context-memory.git@0123456789abcdef0123456789abcdef01234567'
uvx --from "$SOURCE" context-memory init \
  --package "$SOURCE" \
  --clients claude-code,cursor,vscode \
  --register
```

`context-memory init` rejects unpinned Git package sources. A PyPI package name, or a one-time installed executable with `--launcher installed`, does not use a Git URL at MCP startup.

Until the first PyPI release, use the source install below and replace the default launcher with `--launcher installed`.

### From source

```bash
git clone https://github.com/foonsoo/context-memory.git context-memory
cd context-memory
python3.14 -m venv .venv  # recommended; Python 3.11+ is supported
.venv/bin/pip install -e .
.venv/bin/context-memory init --launcher installed
```

The default database is `~/.local/share/context-memory/memory.db`. Pass `--db .context-memory/memory.db` if repository-local isolation is preferred and add `.context-memory/` to the consumer repository's `.gitignore`.

For a previous live WAL database, use the integrity-checked `migrate-db` flow in [docs/CLIENTS.md](docs/CLIENTS.md); do not copy only the main SQLite file.

Run without installing, if preferred:

```bash
PYTHONPATH=src python3 -m context_memory.cli --db .context-memory/memory.db project-list
```

## Connect Codex (recommended: stdio)

Codex currently supports local stdio and Streamable HTTP MCP servers. The desktop app, CLI, and IDE extension on the same host share MCP configuration. Project-scoped `.codex/config.toml` is loaded only for trusted projects. See the official [MCP configuration documentation](https://learn.chatgpt.com/docs/extend/mcp) and [AGENTS.md documentation](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

Create `.codex/config.toml` in the repository that should use memory:

```toml
[mcp_servers.context_memory]
command = "/absolute/path/to/context-memory/.venv/bin/context-memory"
args = ["--db", "/absolute/path/to/consumer-repo/.context-memory/memory.db", "serve", "--transport", "stdio"]
required = true
default_tools_approval_mode = "writes"
```

Or add it with the CLI:

```bash
codex mcp add context_memory -- /absolute/path/to/context-memory/.venv/bin/context-memory --db /absolute/path/to/memory.db serve --transport stdio
codex mcp list
```

Restart Codex, then use `/mcp` to verify the server. Copy [examples/AGENTS.md](examples/AGENTS.md) when durable workflow guidance is desired. Agents should call `project_resolve` with the current workspace instead of relying on a manually copied UUID. MCP server instructions also remind clients to retrieve context and preserve provenance.

## Connect other agents

Context Memory uses standard MCP stdio and Streamable HTTP-shaped JSON-RPC messages and contains no Codex or Claude SDK dependency. The same database can therefore be used by multiple clients. `project_resolve(cwd)` maps each canonical workspace path automatically, while `session_start.client` records which client produced a session.

- Claude Code: user-level registration through `claude mcp add-json --scope user`
- Cursor: global merge into `~/.cursor/mcp.json`; existing servers and unrelated keys are preserved
- VS Code: user-profile registration through `code --add-mcp`
- Codex: user-level registration through `codex mcp add`
- Craft Agents: the command prints source JSON and a guided workspace-source step
- Windsurf and other clients: use the portable JSON printed by `context-memory init`
- Remote or sandboxed clients that cannot spawn a local process: run the optional HTTP transport and connect to `http://127.0.0.1:8765/mcp`

Client-specific hooks are optional convenience integrations. Correctness must not depend on them: the MCP initialization instructions and tool descriptions carry the portable workflow.

See [docs/CLIENTS.md](docs/CLIENTS.md) for the exact shared lifecycle contract and client configuration behavior.

### Two different sharing scenarios

#### One computer, several MCP clients

Use one local database path and register the same stdio definition in every client:

```bash
context-memory --db ~/.local/share/context-memory/memory.db init \
  --clients claude-code,codex,cursor,vscode,craft \
  --launcher installed \
  --register
```

Each client starts its own lightweight server process, while SQLite WAL coordinates access to the same database. `session_start.client` preserves which client wrote a session. This is the primary supported sharing mode.

#### Several computers or OS users

A local stdio command and local SQLite path are not shared across machines or OS accounts. Do **not** put a live WAL database in Dropbox, iCloud Drive, NFS, or another file-sync/network folder; partial or conflicting WAL synchronization can corrupt or diverge.

For occasional transfer, use `export`/`import`. Continuous multi-machine sharing requires one always-on MCP service in front of the database, TLS, authentication, backups, and multi-user authorization. The MVP HTTP listener is localhost-first and bearer-token capable, but it is not a production multi-user deployment.

### Portable backup and restore

Export includes the project, scopes, sessions, immutable events, memories, provenance links, graph edges, and audit history. It intentionally excludes local idempotency caches and FTS internals.

```bash
context-memory export PROJECT_UUID --output project-memory.jsonl
context-memory import project-memory.jsonl
context-memory repair --project-id PROJECT_UUID
```

Import is additive and refuses to overwrite an existing project ID or slug. Search indexes are rebuilt from the exported memories. `repair` independently reconstructs a damaged or stale FTS projection from authoritative memory rows.

### Bounded operation and consistent backup

Every project has conservative defaults: 12,000 context characters, 20 context items, 10,000 detailed audit entries, and 180 days for terminal (`superseded`, `rejected`, `expired`) memories. Inspect or change them with:

```bash
context-memory policy PROJECT_UUID
context-memory policy PROJECT_UUID \
  --max-context-chars 8000 \
  --max-context-items 15 \
  --audit-keep-entries 5000 \
  --terminal-memory-days 90
```

Maintenance is dry-run unless `--apply` is explicit:

```bash
context-memory maintain PROJECT_UUID
context-memory maintain PROJECT_UUID --apply
context-memory status PROJECT_UUID
```

Applying maintenance preserves all immutable source events, removes old terminal memory projections, and replaces excess audit detail with chained SHA-256 checkpoints. Export first if reconstructable detail older than the retention window is required.

Do not back up a live WAL database by copying only `memory.db`. Create one consistent, integrity-checked snapshot instead:

```bash
context-memory backup --output /secure/backups/context-memory-latest.db
```

The command atomically replaces the destination using SQLite's Online Backup API, includes committed WAL data, sets the snapshot to mode `0600`, and returns its SHA-256 digest. Reusing a stable destination name lets rsync- or block-deduplicating backup systems transfer changed pages instead of treating every dated filename as unrelated. `search_health` and `repair` detect and restore FTS projection consistency; memory insert/update/delete triggers keep the projection synchronized during normal writes.

## Why an evidence ledger instead of a graph-first memory

Context Memory does not claim that graphs are universally worse. Graph databases are useful for multi-hop relationship questions and entity traversal. They are a poor mandatory foundation when the common questions are “what did we decide?”, “is this still true?”, and “where did this claim come from?”.

The default evidence-ledger design provides:

- deterministic local operation without an extraction LLM, embedding model, graph database, or background service;
- immutable source events, so summaries can be inspected rather than trusted as the only truth;
- explicit proposed, active, disputed, and superseded states for contradictory or outdated facts;
- bounded retrieval cost through FTS5 and a strict character budget;
- simple backup, export, migration, and incident inspection using one SQLite file;
- predictable writes: the system does not silently invent entities or edges during ingestion.

Verified memories can now be connected with `supports`, `depends_on`, and `related_to` edges and traversed up to five hops. Traversal excludes superseded/rejected nodes by default, while the evidence ledger remains authoritative. This supplies useful graph queries without a graph daemon or automatic entity extraction.

For domain paraphrases, `search_alias_set` adds explicit project vocabulary such as `database → PostgreSQL`. Query expansion is deterministic, auditable, and local. It complements rather than pretends to replace optional embeddings: unknown semantic equivalents still require an embedding projection or explicit aliases. See [docs/UTILITY.md](docs/UTILITY.md).

For zero-download fuzzy and partial-phrase recall, enable the optional on-device projection before starting the server:

```bash
export CONTEXT_MEMORY_EMBEDDINGS=local-hash
context-memory --db ~/.local/share/context-memory/memory.db serve --transport stdio
```

Search then fuses FTS5 and local-similarity ranks and returns inspectable score components. The local hash projection is useful for spelling, morphology, and overlapping wording; it is not a neural semantic model and does not replace explicit aliases for unrelated synonyms. Agents may call `memory_feedback` with `retrieved`, `used`, `helpful`, or `incorrect` to personalize later ranking. `observed_at` and `last_confirmed_at` keep discovery time separate from confirmation freshness.

## Reproducible comparison benchmark

The repository includes [benchmarks/run_benchmark.py](benchmarks/run_benchmark.py). It starts all products through MCP stdio and uses no API keys:

```bash
PYTHONPATH=src python3 benchmarks/run_benchmark.py --items 1000 --repeats 200
```

It compares Context Memory with `@modelcontextprotocol/server-memory` and `@ideadesignmedia/memory-mcp`. The runner measures exact recall, query latency, changed-fact behavior, source recovery, history preservation, multi-hop support, ingest time, and local storage. `npx` and network access are required the first time competitors are downloaded. Treat the checked-in local result as a reproducibility artifact, not a universal ranking.

The checked-in final result was produced with CPython 3.14.6 and records Python, SQLite, OS, architecture, Node, and npm versions. Competitor npm versions are pinned in the runner.

### Optional local HTTP

```bash
.venv/bin/context-memory --db .context-memory/memory.db serve --transport http
```

This listens only on `127.0.0.1:8765`. Configure Codex with:

```toml
[mcp_servers.context_memory]
url = "http://127.0.0.1:8765/mcp"
```

Binding a non-loopback address is refused unless `--token` or `CONTEXT_MEMORY_TOKEN` is set. For an authenticated local endpoint:

```toml
[mcp_servers.context_memory]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "CONTEXT_MEMORY_TOKEN"
```

Bearer authentication without TLS is not safe across an untrusted network. Remote production service, OAuth, and TLS termination are intentionally out of scope.

## First record and next-session retrieval

1. Call `project_resolve` with the current workspace path, then start a memory session with the returned project and scope IDs. Set `client` to the actual client name (for example `claude-code`, `craft-agent`, or `codex`) and pass its task/session ID as `external_id` when available.
2. Record authoritative evidence with `record_event`:

```json
{
  "project_id": "PROJECT_UUID",
  "kind": "decision",
  "content": "Use port 8765 for the local HTTP listener.",
  "source_uri": "docs/ARCHITECTURE.md",
  "idempotency_key": "decision-http-port-v1"
}
```

3. Pass the returned event ID to `memory_upsert`. Confirmed repository decisions may be `active`; generated interpretations should be `proposed`:

```json
{
  "project_id": "PROJECT_UUID",
  "title": "Local HTTP port",
  "content": "The default local HTTP listener uses port 8765.",
  "memory_type": "decision",
  "status": "active",
  "confidence": 1.0,
  "importance": 0.7,
  "source_event_ids": ["EVENT_UUID"],
  "tags": ["http", "configuration"],
  "idempotency_key": "memory-http-port-v1"
}
```

4. End the session. In a new Codex task, call `get_context` with `query: "HTTP server configuration"` and `char_budget: 4000`. The returned block cites `EVENT_UUID`; call `get_source` before relying on it when accuracy matters.

For a shell-only demo, IDs can be captured with a short Python script or inspected from the JSON output:

```bash
.venv/bin/context-memory --db .context-memory/memory.db search PROJECT_UUID "HTTP configuration"
.venv/bin/context-memory --db .context-memory/memory.db context PROJECT_UUID "HTTP configuration" --budget 4000
```

## Reducing missed records with Codex hooks

The official [Codex hooks documentation](https://learn.chatgpt.com/docs/hooks) includes `SessionStart` and `SessionEnd`. This repository provides [examples/hooks.json](examples/hooks.json) and `context_memory.hooks`:

- `SessionStart` starts/resumes the DB session and injects a budgeted set of verified relevant memories.
- `SessionEnd` stores the final assistant message as an immutable event explicitly marked `unverified_ai_output`; it does **not** promote it to memory.
- `AGENTS.md` prompts in-task recording at the moment decisions and evidence appear. Hooks alone cannot reliably infer every important fact.

Copy the hook file to the consumer's `.codex/hooks.json`, then launch Codex with these variables available:

```bash
export CONTEXT_MEMORY_DB="$PWD/.context-memory/memory.db"
export CONTEXT_MEMORY_PROJECT="my-project"
export CONTEXT_MEMORY_CONTEXT_BUDGET=5000
```

Project hooks require a trusted project and explicit review in Codex. Use `/hooks`, review the exact commands, then trust them. `SessionEnd` has a strict three-second maximum; the hook performs only local SQLite writes. The hook is a convenience layer, while explicit MCP calls remain the auditable path for structured sources, memory promotion, supersession, and disputes.

## MCP tools

| Tool | Purpose |
|---|---|
| `project_create`, `project_list`, `scope_create` | Project and path/module boundaries |
| `session_start`, `session_end` | Cross-client session lifecycle |
| `record_event` | Immutable raw evidence; `kind=message` is unverified inter-session coordination |
| `read_events_since` | Cursor-based incremental event/message polling with pagination |
| `memory_upsert` | Proposed/active derived memory with source event IDs |
| `memory_transition` | Activate, supersede, dispute, expire, or reject; add relationship edge |
| `search_alias_set`, `search_alias_list` | Manage deterministic project vocabulary for paraphrase expansion |
| `relation_create`, `graph_traverse` | Link verified memories and traverse active/disputed relations up to five hops |
| `memory_search` | Local FTS5/BM25 ranking plus confidence and importance |
| `get_context` | Strict shared-budget selection of active/disputed context plus optional recent events |
| `get_source` | Retrieve original event evidence |
| `get_audit` | Inspect append-only entity history |

Writes respond only after commit. Repeating a supported operation with the same idempotency key and identical arguments returns the original response; reusing the key with different arguments fails.

### Cursor-based session messages

Use immutable `message` events for short-lived, unverified coordination between sessions or MCP clients. They remain source events and are never promoted into ranked memory automatically:

```bash
context-memory event PROJECT_UUID message "schema.py is being migrated; avoid editing it" --key msg-session-a-1
context-memory events-since PROJECT_UUID --cursor 0 --kind message
```

Every event receives a project-local monotonic `event_seq`. `read_events_since` returns events in sequence order with `next_cursor`, `snapshot_cursor`, and `has_more`; persist `next_cursor` per client/workflow and pass it on the next call. Cursor state belongs to the exact project, scope, and kind-filter combination and must not be reused with another stream definition.

`get_context` can combine ranked memory with bounded polling while keeping the two result sections separate:

```bash
context-memory context PROJECT_UUID "database migration" \
  --event-cursor 42 \
  --event-kind message \
  --event-budget 1500
```

The response places verified memory in `context`/`items` and unverified stream entries in `recent_events`. The event allowance is capped at 4,000 characters and consumes the same effective project context budget; unused event allowance remains available to memories. If an event body is truncated, retrieve the immutable full event with `get_source(event_id)`.

## Test and smoke test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests cover WAL persistence and permissions, append-only enforcement, FTS and alias-expanded retrieval, provenance, budget selection, state transitions, verified graph traversal, export/import, rollback, idempotency, MCP discovery/calls, stdio process handshake, HTTP process handshake, and external-bind refusal.

## Design and limits

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for schema invariants, ranking, security, and the Python/SQLite choice. Read [docs/ROADMAP.md](docs/ROADMAP.md) for memory revisions, retention, retrieval improvements, and optional local embeddings.

Current limits: single OS-user trust boundary, no encryption/redaction, no remote synchronization, no automatic retention, basic lexical ranking, no vector index, and no autonomous candidate review UI. FTS5 availability depends on the Python SQLite build; typical CPython distributions include it.
