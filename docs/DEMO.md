# Reproducible lifecycle demo

Run `python -m pip install -e . && python scripts/demo_lifecycle.py` from the
repository. It uses a temporary directory and dedicated database, never global
client configuration. Pass `--keep` only when you want to inspect the synthetic
database afterward.

The script asserts the initial SQLite memory/source IDs, the same source after a
process restart, the final PostgreSQL memory/source IDs, exclusion of SQLite
from the final decision, and its `superseded` state. It also checks response
counts and IDs, JSON-RPC and MCP tool errors, malformed JSON, and a 20-second
subprocess timeout. Any mismatch exits non-zero so CI fails. Diagnostics include
captured stdout/stderr; rerun with `--keep` to inspect its temporary database.

The script crosses the CLI and MCP stdio boundary and restarts server processes.
It verifies persistence and retrieval, not real-client UI integration or an
LLM's compliance with memory-writing instructions.
