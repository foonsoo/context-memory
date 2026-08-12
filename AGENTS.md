# Global Context Memory workflow

- At the start of every task, use one `context_bootstrap` call with the current workspace directory, the request as a focused query, a 4,000–8,000 character budget, and `response_format=compact`. Supply the actual MCP client name and current client session/task ID as `external_id` when available. Treat the directory as an identity hint: retrieval may discover relevant memories from another registered project when the selected partition has no matching project memory. Use the returned project and scope for writes; never require the user to provide a project UUID. The separate resolve/start/context calls remain valid when individual control is required.
- Inspect consequential citations with `get_source`. Treat disputed memories as warnings.
- Record durable user decisions, constraints, verified facts, and material test results as immutable events. Never store credentials, secrets, raw environment dumps, unrelated personal data, or routine tool chatter.
- Derive memories from source event IDs. Keep model-inferred summaries `proposed`; activate only information confirmed by the user, repository, tests, or authoritative evidence.
- When information changes, create and activate the evidenced replacement, then mark the prior memory `superseded`. Use `disputed` for unresolved conflicts.
- Before finishing, preserve concise completion evidence and promote only reusable verified information. End the memory session.

This contract is client-neutral. Hooks may automate parts of it, but correctness must not depend on a hook being installed or fired.
