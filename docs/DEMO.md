# Reproducible lifecycle demo

Run `python -m pip install -e . && python scripts/demo_lifecycle.py` from the
repository. It uses a temporary directory and dedicated database, never global
client configuration. Pass `--keep` only when you want to inspect the synthetic
database afterward.

Expected output contains an initial SQLite recall and source ID, a current
PostgreSQL recall and source ID, `sqlite_memory.status: superseded`, and
`postgres_memory.status: active`. On failure, inspect the raised MCP JSON-RPC
error and rerun `context-memory --db <kept-path>/demo.db doctor`.

The script crosses the CLI and MCP stdio boundary and restarts server processes.
It verifies persistence and retrieval, not real-client UI integration or an
LLM's compliance with memory-writing instructions.
