# Shared context memory workflow

- At the start of a task, call `project_resolve` with the current workspace directory. Use the returned project and scope IDs for `session_start` and a focused `get_context` request with a 4,000–8,000 character budget. Treat disputed entries as warnings and inspect cited events with `get_source` before relying on consequential claims.
- Record important user statements, observed facts, test results, and decisions as immutable events with stable idempotency keys. Never store secrets, credentials, raw environment dumps, or unrelated personal data.
- Derive memories from one or more `source_event_ids`. Keep AI-inferred summaries `proposed`; use `active` only when the user, repository, or authoritative evidence confirms them.
- During work, record durable decisions and constraints when they occur. Do not dump every tool call or inject the whole database.
- When facts change, create the replacement with evidence, activate it, then mark the old memory `superseded` and link the replacement. Use `disputed` when evidence conflicts.
- Before finishing, record a concise raw completion event and promote only reusable, verified information. End the memory session.
