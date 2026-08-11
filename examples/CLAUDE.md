# Context Memory

At the start of every task, call `project_resolve` with the current workspace, then `session_start` and a focused `get_context` query with a 4,000–8,000 character budget. Use the returned project and scope IDs; do not ask the user for UUIDs. Inspect consequential citations with `get_source`.

Record durable decisions and verified facts with `record_event`, and derive memories with `memory_upsert` using source event IDs. Keep inference `proposed`; use `active` only for confirmed knowledge. Never record secrets. End with `session_end`. Hooks are optional and are not a substitute for this workflow.
