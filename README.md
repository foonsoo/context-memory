# Context Memory

A small, client-neutral context memory server for any MCP agent, including Claude Code, Craft Agents, Codex, Cursor, VS Code, and local-model clients. It keeps immutable evidence separate from derived memories and the FTS search projection, so a generated summary never becomes the only source of truth.

For the lowest startup overhead, call `context_bootstrap` once with the workspace, focused query, client identity, and `response_format=compact`. The older `project_resolve` → `session_start` → `get_context` sequence and legacy context response remain supported. `serve` exposes the working `core` tool profile by default; use `--tool-profile admin` for maintenance-only clients or `--tool-profile all` for the historical complete catalog.

This MVP is usable now: Python 3.11+ (Python 3.14 recommended), SQLite with WAL/FTS5, zero runtime dependencies, stdio MCP, local HTTP MCP, migrations, lifecycle hooks, and a standard-library test suite.

> **Sensitive-data warning:** this database is local, not encrypted. Do not record secrets, tokens, private keys, raw environment dumps, or unrelated personal data. The data directory is mode `0700`, but other processes running as your OS user and backups may still access it.

## Quick start: one database, every MCP client

Install one launcher, choose one database path, and register that exact pair with every client on the same computer. Until the first PyPI release, a stable source installation can be created with:

```bash
git clone https://github.com/foonsoo/context-memory.git
cd context-memory
python3.14 -m venv ~/.local/share/context-memory/runtime
~/.local/share/context-memory/runtime/bin/pip install --no-deps .
chmod 700 ~/.local/share/context-memory

~/.local/share/context-memory/runtime/bin/context-memory \
  --db ~/.local/share/context-memory/memory.db \
  init --workspace "$PWD" --launcher installed \
  --clients claude-code,codex,cursor,vscode,craft --register

~/.local/share/context-memory/runtime/bin/context-memory \
  --db ~/.local/share/context-memory/memory.db doctor
```

Restart each registered client. At the beginning of **every new task/session**, the agent should perform this client-neutral sequence:

1. `project_resolve(cwd)` with the current workspace. The path is a preferred identity hint; an unambiguous registered project name can resolve another workspace path to the same project.
2. `session_start` with the returned project/scope, actual client name, and task/session ID when available.
3. `get_context` using the current request as a focused query and a 4,000–8,000 character budget. If the selected project has no matching project memory, retrieval searches memory candidates across the shared database and aggregates normalized relevance, project-identity priors, recent activity, and the latest checkpoint by project; global memories are always merged. A sufficiently strong and separated candidate is selected automatically. For low-confidence or ambiguous results, inspect `project_discovery.candidates` and ask the user which project they mean.
4. During work, preserve durable evidence with `record_event`; derive sourced knowledge with `memory_upsert`.
5. Before finishing, record reusable verified results and call `session_end`.

Copy the appropriate instruction template into the consumer project: [`AGENTS.md`](AGENTS.md) or [`examples/AGENTS.md`](examples/AGENTS.md), [`examples/CLAUDE.md`](examples/CLAUDE.md), or [`examples/cursor-context-memory.mdc`](examples/cursor-context-memory.mdc). Generic clients can use [`examples/mcp.json`](examples/mcp.json). Hooks are optional convenience automation; the lifecycle above remains the correctness contract.

If `init --register` reports a client as unavailable, install that client's CLI and rerun the same command. Craft Agents currently returns a guided workspace-source step and portable JSON rather than editing its configuration automatically. See [docs/CLIENTS.md](docs/CLIENTS.md) for registration and migration details.

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

Export includes the project, project identity aliases, scopes, sessions, immutable events, memories, provenance links, graph edges, and audit history. It intentionally excludes local idempotency caches and FTS internals.

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

Export the checkpoint chain and remaining audit entries for verification on a machine that does not have the database:

```bash
context-memory audit-export PROJECT_UUID --output audit-chain.json
context-memory audit-verify audit-chain.json
context-memory audit-verify audit-chain.json --expected-head-digest TRUSTED_DIGEST
```

The export is deterministic. Verification checks project identity, checkpoint linkage and ranges, live-entry ordering, and the optional separately recorded head digest. Use `--expected-head-digest` when the bundle itself might have been replaced; unanchored verification can detect internal corruption but cannot establish that the entire chain is the expected one. Compacted row contents are intentionally absent, so keep a full project export when those historical details must remain reconstructable.

To create a portable detached trust anchor, provide a base64-encoded 32-byte Ed25519 private key through an environment variable. The private key is never written to the anchor; distribute the returned public key through a separate trusted channel:

```bash
context-memory audit-anchor-sign audit-chain.json --output audit-anchor.json --private-key-env CONTEXT_MEMORY_AUDIT_SIGNING_KEY
context-memory audit-anchor-verify audit-anchor.json --audit-bundle audit-chain.json --expected-project-id PROJECT_UUID --expected-public-key TRUSTED_PUBLIC_KEY
```

Signing requires the `crypto` extra. Verification authenticates the detached project/head-digest/timestamp tuple and, when `--audit-bundle` is supplied, anchors full offline chain verification to that signed digest.

For scheduler-driven maintenance, persist an interval and invoke the idempotent due-check from cron, launchd, or a systemd timer. `0` disables scheduling; the minimum enabled interval is five minutes:

```bash
context-memory policy PROJECT_UUID --maintenance-interval-seconds 86400
context-memory maintain PROJECT_UUID --apply --scheduled
```

Repeated invocations before the next due time do no work. The last start, completion, and error are visible in `status`; Context Memory does not install or run a background daemon.

Do not back up a live WAL database by copying only `memory.db`. Create one consistent, integrity-checked snapshot instead:

```bash
context-memory backup --output /secure/backups/context-memory-latest.db
```

The command atomically replaces the destination using SQLite's Online Backup API, includes committed WAL data, sets the snapshot to mode `0600`, and returns its SHA-256 digest. Reusing a stable destination name lets rsync- or block-deduplicating backup systems transfer changed pages instead of treating every dated filename as unrelated. `search_health` and `repair` detect and restore FTS projection consistency; memory insert/update/delete triggers keep the projection synchronized during normal writes.

Optional authenticated encryption uses an AES-256-GCM envelope with a scrypt-derived key. Install `context-memory[crypto]`, keep the passphrase out of argv, and name only the environment variable that contains it:

```bash
CONTEXT_MEMORY_BACKUP_PASSPHRASE='...' context-memory backup \
  --output /secure/backups/context-memory-latest.db.enc \
  --passphrase-env CONTEXT_MEMORY_BACKUP_PASSPHRASE
CONTEXT_MEMORY_BACKUP_PASSPHRASE='...' context-memory backup-decrypt \
  /secure/backups/context-memory-latest.db.enc \
  --output /secure/restore/memory.db \
  --passphrase-env CONTEXT_MEMORY_BACKUP_PASSPHRASE
```

The temporary consistent SQLite snapshot is deleted after encryption, and decryption authenticates the envelope before atomically replacing its output. Losing the passphrase makes the backup unrecoverable.

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

For domain paraphrases, `search_alias_set` adds explicit project vocabulary such as `database → PostgreSQL`. Query expansion is deterministic, auditable, and local. It complements rather than pretends to replace the default local projection: unknown semantic equivalents still require a stronger embedding projection or explicit aliases. See [docs/UTILITY.md](docs/UTILITY.md).

The dependency-free on-device feature-hash projection is enabled by default for zero-download fuzzy and partial-phrase recall. To explicitly use FTS5 only:

```bash
export CONTEXT_MEMORY_EMBEDDINGS=off
context-memory --db ~/.local/share/context-memory/memory.db serve --transport stdio
```

By default, search fuses FTS5 and local-similarity ranks and returns inspectable score components. `CONTEXT_MEMORY_EMBEDDINGS=local-hash` may still be set explicitly. Local-hash v2 uses a 1,024-dimension word/character projection with Hangul-only syllable bigrams for Korean spacing and particle variation. Reranking is limited to lexical candidates when they exist; vector-only fallback scans at most 1,000 deterministic candidates for 25 ms, and `retrieval.semantic_scan` reports the mode, limits, evaluated count, and truncation state. Cross-project fallback additionally limits local-hash to at most 12 projects supported by memory FTS, shared repository aliases, or project identity evidence, while global memories remain universally eligible. The local hash projection is useful for spelling, morphology, and overlapping wording; it is not a neural semantic model and does not replace explicit aliases for unrelated synonyms. Agents may call `memory_feedback` with `retrieved`, `used`, `helpful`, or `incorrect` to personalize later ranking. `observed_at` and `last_confirmed_at` keep discovery time separate from confirmation freshness.

An experimental local neural adapter is explicit opt-in and never downloads a model during a default install. Install `context-memory[neural]`, then set `CONTEXT_MEMORY_EMBEDDINGS=neural` and `CONTEXT_MEMORY_EMBEDDING_MODEL` to a local path or an intentionally selected sentence-transformers model identifier. `CONTEXT_MEMORY_EMBEDDING_DEVICE` is optional. Use `PYTHONPATH=src python3 benchmarks/run_embedding_evaluation.py --model MODEL` to compare FTS-only, local-hash, and neural projections on the same disposable fixture before choosing it for personal data. Pass `--fixture private-judgments.json` to evaluate a private schema-v1 fixture containing `memories` (`key`, `title`, `content`) and `queries` (`query`, `relevant`, `category`). The report records fixture counts/source plus aggregate metrics and diagnostics; keep the input outside the repository.

Feedback applies small bounded importance adjustments, and context assembly suppresses near-identical blocks. Memories default to project visibility; set `visibility=global` only for non-path-scoped user preferences or constraints that should be available to every project in the same local database.

For controlled Codex startup measurements, `benchmarks/run_codex_token_experiment.py`
creates one frozen synthetic database and a private copy per run, then records a
balanced manifest. Analyze it with `benchmarks/analyze_codex_tokens.py --manifest`.
The runner validates the exact startup sequence and retries uncontrolled model
runs; it never points the benchmark at the live Context Memory database.

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

The promotable durable kinds are `fact`, `decision`, `preference`, `constraint`, `procedure`, `task`, and `summary`. Other strings remain valid append-only event kinds, but session-bound writes return a non-fatal promotion advisory and `session_end` does not automatically convert them into proposed memories.

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

4. For long-running work, call `checkpoint_evaluate` to apply configurable soft/hard context thresholds or client-neutral elapsed/event/repository/age fallbacks. It suppresses unchanged recovery state and repeated signals using content hashing, cooldown, and hysteresis, and returns a stable `suggested_idempotency_key`. When recommended, pass that key to `checkpoint_create`. Interim creation records caller-supplied recovery state as an immutable checkpoint event without mutating Git, memories, or session lifecycle; it cannot use `reason=completed`, requires any referenced session to remain active, and records explicit false completion/verification claims. Final creation requires an active session, verified source event IDs, and handoff title/content. In one transaction it creates an active evidence-backed handoff, optionally supersedes a specified active handoff, and ends the session. A supplied `commit` must already exist in `repository_path`; the server verifies and records it but never creates Git commits. Pass `repository_path` to capture HEAD, branch, dirty state, and changed files, and pass structured `test_results` for explicitly observed outcomes. Objective facts live under `objective`, separate from semantic goal/progress summaries.
5. End the session. In a new Codex task, call `get_context` with `query: "HTTP server configuration"` and `char_budget: 4000`. The returned block cites `EVENT_UUID`; call `get_source` before relying on it when accuracy matters.

For a shell-only demo, IDs can be captured with a short Python script or inspected from the JSON output:

```bash
.venv/bin/context-memory --db .context-memory/memory.db search PROJECT_UUID "HTTP configuration"
.venv/bin/context-memory --db .context-memory/memory.db context PROJECT_UUID "HTTP configuration" --budget 4000
```

## Reducing missed records with Codex hooks

The official [Codex hooks documentation](https://learn.chatgpt.com/docs/hooks) includes `SessionStart` and `SessionEnd`. This repository provides [examples/hooks.json](examples/hooks.json) and `context_memory.hooks`. At session end the hook evaluates the portable checkpoint policy and, when triggered, calls the same interim checkpoint operation used by MCP and CLI before closing the session:

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
| `project_create`, `project_list`, `project_resolve`, `project_alias_list`, `scope_create` | Project discovery, repository identity, and path/module boundaries |
| `session_start`, `session_end` | Cross-client session lifecycle |
| `record_event` | Immutable raw evidence; `kind=message` is unverified inter-session coordination |
| `checkpoint_create` | Idempotent interim recovery marker or atomic evidence-backed final handoff/session closure; captures semantic progress, objective Git facts, tests, and an optional existing commit |
| `checkpoint_evaluate` | Read-only threshold/fallback evaluation with cooldown, hysteresis, recovery hashing, and a stable idempotency key |
| `read_events_since` | Cursor-based incremental event/message polling with pagination |
| `event_poll`, `event_ack` | Durable per-consumer polling and monotonic acknowledgement for an exact kind/scope stream |
| `memory_upsert` | Proposed/active derived memory with source event IDs |
| `memory_transition` | Activate, supersede, dispute, expire, or reject; add relationship edge |
| `search_alias_set`, `search_alias_list` | Manage deterministic project vocabulary for paraphrase expansion |
| `relation_create`, `graph_traverse` | Link verified memories and traverse active/disputed relations up to five hops |
| `memory_search` | Local FTS5/BM25 ranking plus confidence and importance |
| `review_queue`, `review_action` | Inspect typed proposed-memory and latest Wiki-revision reviews with lint; resolve memories through explicit approve/reject/supersede/dispute actions |
| `memory_correct` | Create a sourced correction candidate without overwriting history |
| `get_context` | Strict shared-budget local/global selection, registry fallback, and optional recent events |
| `decision_context` | Versioned, cited Decision Brief over the existing retrieval path; never generates a recommendation or changes memory state |
| `wiki_page_create`, `wiki_note_set`, `wiki_page_get` | Topic page identity and separately managed manual notes |
| `wiki_revision_generate`, `wiki_revision_transition`, `wiki_revision_render`, `wiki_markdown_export`, `wiki_revision_lint` | Immutable cited revisions, explicit publication lifecycle, deterministic staleness/lint, and portable linked Markdown |
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

`decision_context` is the decision-oriented read contract. It classifies retrieved memories into current decisions, rationale, constraints, alternatives, outcomes, chronological history, disputes, open questions, and uncertainty. Every evidence claim carries its memory ID and source event IDs; deterministic retrieval gaps, including a current decision with no retrieved rationale, are labeled separately and `recommendation` is always `null`. Within the bounded results already selected by `get_context`, it applies a Decision Brief-only rerank for question intent, lifecycle/type, direct provenance, decision roles, and unsupported/stale/handoff penalties. It can then expand at most one hop from three current-decision seeds through supports, depends-on, supersession, and shared-investigation evidence, with 50 candidates and the existing item/character budgets as hard limits. Added score components and expansion paths are exposed under `decision_rerank` and `decision_expansion`; general `memory_search` ordering remains unchanged.

Run `PYTHONPATH=src python3 benchmarks/run_decision_evaluation.py` to evaluate this contract against the frozen public Decision Brief scenarios. The report includes accuracy, stale leakage, citation and source recovery, history recall, payload size, and latency metrics.

### Research-to-Decision provenance

`investigation_create` records why a focused research question matters, the decision it will inform, constraints, initiator, and start time. `investigation_record_source` then atomically records one stable source identity and version (or privacy-safe analyzed-content fingerprint) plus only its consequential typed claims: evidence, inference, action, decision, rationale, or outcome. Every claim creates an immutable event and a cited memory; inference memories are always `proposed`, and causal claim links retain the exact evidence used. Repeating the same source identity and version returns the existing analysis, while a changed version creates new evidence. `investigation_get` reconstructs the complete chain and `investigation_complete` closes its lifecycle. The core stores concise analyzed claims and source metadata, never the full page or browsing log.

`source_reinspection_request` appends an idempotent request to revisit one recorded source when it is old, unavailable, or known to have a newer version. The response returns the stable source identity and URI needed by an authorized client and explicitly reports that the core performed no fetch. Requests are append-only, remain visible in `investigation_get`, and round-trip through project export/import.

See the [authorized source client workflow](docs/SOURCE_CLIENT_WORKFLOW.md) and its schema-checked [Confluence-like examples](examples/confluence-like-source-workflow.json) for initial analysis and newer-version reinspection. They are vendor-neutral orchestration examples, not a server-owned connector.

### Topic Wiki revisions

`wiki_page_create` creates a stable topic page, while `wiki_note_set` manages human-authored notes outside generated content. `wiki_revision_generate` snapshots the standard Decision Brief sections and exact memory/event citation pairs into an immutable `proposed` revision. Reviewers explicitly publish or reject it with `wiki_revision_transition`; publishing a replacement makes the prior published revision stale. A published revision also becomes stale when a cited memory is materially changed, superseded, disputed, expired, or rejected. `wiki_revision_lint` deterministically reports missing citations and sources, terminal or disputed citations, stale revisions, relevant current memories omitted from the revision, recommendation-like claims that lack explicit support or are mislabeled as evidence instead of inference, and cited source versions not reinspected for 30 days. Source age is only a reinspection prompt: it never claims that the external source changed, establishes freshness, or marks a revision stale. The response explicitly labels deterministic-rule execution, absence of model assistance, and absence of state changes; repeated calls over unchanged state are identical and read-only. `wiki_revision_render` produces Markdown from SQLite on demand, leaving SQLite as the only writable authority.

`wiki_browse` provides the first P5 human-navigation surface: a deterministic paginated topic/page index with current revision summaries. Each item exposes `reader_state` and `renderable`; window-level renderable/unrenderable counts keep rejected-only or otherwise revision-less audit pages visible without presenting them as readable current pages. When given a page ID, it also returns reverse citation backlinks showing which other current Wiki pages cite each of the selected page's memories. It reads the authoritative Wiki tables and deliberately does not introduce a second text-search index.

`wiki_markdown_export` renders a bounded browse window as an `index.md` plus stable `pages/<page-id>.md` documents. `source_page_count` and `skipped_page_count` explain when the browse window contains pages without a renderable current revision. Each exported page records stable page/revision metadata and links back to the index, adjacent pages, and related pages that share cited memories. The export is deterministic and read-only; SQLite remains the sole writable authority.

## Test and smoke test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Development-only PEP 8 checks use the pinned Ruff version and do not add a
runtime dependency:

```bash
python3 -m pip install -e '.[dev]'
ruff check .
ruff format --check .
```

The checked production-module allowlist in `pyproject.toml` expands one module
at a time as existing violations are cleared. It is intentionally a positive
adoption list rather than a permanent ignore list.

Tests cover WAL persistence and permissions, append-only enforcement, FTS and alias-expanded retrieval, provenance, budget selection, state transitions, verified graph traversal, export/import, rollback, idempotency, MCP discovery/calls, stdio process handshake, HTTP process handshake, and external-bind refusal.

CI also installs the built wheel beside the pinned official MCP Python SDK and runs `tests/official_sdk_e2e.py`. That black-box check exercises initialization, paginated tool discovery, structured tool calls, protocol validation errors, and two concurrent SDK clients sharing one database; the production package itself retains zero runtime dependencies.

Candidate approval remains explicit through MCP review actions; session extraction never promotes memories automatically. `review_queue` also includes the latest non-rejected revision for each Wiki page when it is proposed or has lint findings. These typed Wiki entries reuse deterministic lint and route proposed revision approval/rejection through `wiki_revision_transition`; they do not introduce a second review state or autonomous mutation path.

## Design and limits

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for schema invariants, ranking, security, and the Python/SQLite choice. Read [docs/ROADMAP.md](docs/ROADMAP.md) for memory revisions, retention, retrieval improvements, and optional local embeddings.

Current limits: single OS-user trust boundary, no live-database encryption or redaction, no remote synchronization, feature-hash rather than neural embeddings, no vector index, and similarity flags rather than semantic contradiction proof. Backup envelopes can be encrypted optionally. FTS5 availability depends on the Python SQLite build; typical CPython distributions include it.
