# Context Memory

At the start of every task, call `project_resolve` with the current workspace as an identity hint, then `session_start` and a focused `get_context` query with a 4,000–8,000 character budget. Use the returned project and scope IDs for writes; retrieval can discover relevant registered projects when the hinted partition has no match. Do not ask the user for UUIDs. Inspect consequential citations with `get_source`.

Record durable decisions and verified facts with `record_event`, and derive memories with `memory_upsert` using source event IDs. Keep inference `proposed`; use `active` only for confirmed knowledge. Never record secrets. End with `session_end`. Hooks are optional and are not a substitute for this workflow.
