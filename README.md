# Context Memory

Context Memory is a local MCP server that carries verified decisions,
constraints, and work state between agent sessions. Immutable evidence remains
separate from derived memories, active memories are traceable to project-local
events, and canonical paths keep unrelated workspaces isolated.

It is designed for MCP clients under one OS account. It is not an encrypted
secrets store, a hosted team service, proof that cited content is true, or a
guarantee that an agent will record the right evidence.

## Install and get a first result

Release `0.6.2` is the latest release documented by this repository. Features
under **Unreleased** exist on `main` but are not part of that package.

```bash
uvx --from context-memory-mcp context-memory \
  --db ~/.local/share/context-memory/memory.db \
  init --workspace "$PWD" --client codex --register
uvx --from context-memory-mcp context-memory \
  --db ~/.local/share/context-memory/memory.db doctor
```

Restart the client. At task start call `context_bootstrap` with the workspace,
focused request, client name, `response_format=compact`, and a 4,000–8,000
character budget. Record evidence with `record_event`, then save an active
memory with `memory_upsert` and its event ID in `source_event_ids`. Inspect
consequential evidence with `get_source`.

For a small session-independent read:

```json
{"cwd":"/current/workspace","query":"다음 작업 진행해줘","token_budget":350,"max_items":6}
```

`context_recall` does not register unknown paths or perform other persistent
writes. Current source also provides a seven-tool `minimal` profile; it remains
unreleased until included in a tag.

## Reproducible source demo

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python scripts/demo_lifecycle.py
```

The temporary-DB demo restarts MCP stdio servers, recalls a sourced SQLite
decision, supersedes it, and recalls only the sourced PostgreSQL replacement.
Wrong results and protocol/tool errors exit non-zero.

## Important limits

- SQLite data is plaintext. Never store credentials, private keys, raw
  environment dumps, or unrelated personal data.
- A source link is an audit trail, not proof that the source or memory is true.
- Same-named folders are separate projects. Moved checkouts need an explicit
  path alias; conflicting scope/alias ownership is rejected.
- Default imports reject active memories without valid same-project events. A
  narrow compatibility flag exists only for historical source-less records.
- Benchmark fixtures are regression data, not independent evidence of better
  user or model outcomes.
- CI configuration and local runs do not prove every hosted CI, macOS, or GUI
  client check passed for the current commit.

## Detailed documentation

- [Clients and database migration](docs/CLIENTS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Project identity, provenance, and import recovery](docs/PROJECT_IDENTITY.md)
- [Lifecycle demo](docs/DEMO.md)
- [Support and compatibility](docs/SUPPORT.md)
- [Evaluation scope](docs/UTILITY.md)
- [Release and rollback](docs/RELEASING.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md) and [security policy](SECURITY.md)

Copy [AGENTS.md](AGENTS.md) or a template under [`examples/`](examples/) into a
consumer project. Hooks may automate the workflow, but correctness must not
depend on a hook firing.
