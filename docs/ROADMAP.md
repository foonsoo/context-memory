# Roadmap

## Near term

- Implemented token-footprint optimization: `context_bootstrap` preserves the legacy resolve/start/context tools while reducing normal startup from three model-mediated calls to one; stdio servers expose a smaller working profile by default with explicit `admin` and backward-compatible `all` profiles; shared workflow guidance remains in `initialize.instructions`; and `get_context(response_format=compact)` returns exact stored fields and provenance once, with explicit truncation signals and the unchanged legacy response still available. A deterministic five-memory fixture reduced advertised tool JSON by 39.8% and context response JSON by 18.0%; these are dependency-free character proxies, while the call count fell 66.7%. Keep measuring real client input tokens before changing context policy limits.

- Continue expanding cross-project discovery calibration scenarios as real workloads reveal new vocabulary and ambiguity shapes. The deterministic 12-domain calibration fixture now validates strong, dominant, low-confidence, and ambiguous selection over more than 1,200 memories; the existing `.45` minimum, `.60` dominant, and `.12` margin thresholds passed without adjustment. Normalized relevance, bounded path/name identity priors, recency, dominant-candidate selection, explicit low-confidence/ambiguity results, global-memory merging, and sparse-project fallback are implemented.
- Add client-neutral automatic handoff checkpoints. MCP 2026-07-28 Tasks can make an explicitly invoked checkpoint observable but cannot detect completion of the host agent's overall work item. Implement the feature in these increments:
  1. Implemented: one idempotent `checkpoint_create` store operation, MCP tool, and CLI command with `mode=interim|final` and reasons `context_budget`, `elapsed`, `material_change`, `completed`, and `manual`. The explicit primitive records caller-supplied recovery state and does not mutate Git, memories, or session lifecycle.
  2. Implemented: persist an immutable checkpoint event containing the current goal, completed work, next executable step, blockers, source event cursor, optional client-reported context usage, captured HEAD/branch/dirty state/changed files, and explicitly supplied structured test results. Objective evidence is stored under `objective`, separate from unverified semantic summaries.
  3. Implemented: configurable client-reported context thresholds with a soft checkpoint near 60% after material change and a hard checkpoint near 75%. The read-only `checkpoint_evaluate` policy engine uses client-neutral elapsed-time, durable-event-count, repository-change, and last-checkpoint-age fallbacks when usage is unavailable.
  4. Implemented: configurable cooldown and context-usage hysteresis, deterministic recovery-state hashing, and stable suggested idempotency keys prevent repeated prompts or hooks from creating checkpoint storms. Evaluation skips checkpoints when no recoverable state changed.
  5. Implemented: interim checkpoints are non-mutating to Git and non-terminal for the memory session. They reject `reason=completed` and ended sessions, store explicit false completion/verification claims, and preserve inferred summaries without promoting them to active memory.
  6. Make final checkpoints link verified evidence, replace the previous active handoff, close the memory session, and record an already-created commit when supplied. Repository commits remain an explicit agent/CLI workflow step rather than an implicit MCP server side effect.
  7. Let optional client lifecycle hooks call the same core operation, and optionally expose checkpoint execution through the Tasks extension. Stored semantics, fallback triggers, and recovery must remain usable without Codex-, Claude-, Cursor-, or vendor-specific SDKs.
  8. Add crash/restart, concurrent-client, missing-usage-signal, threshold/cooldown, idempotency, provenance, and installed-wheel E2E coverage.
- Add optional encrypted backup envelopes and scheduled maintenance. Online consistent snapshots, policy-based retention, and checkpointed audit compaction are implemented.
- Add an offline audit-checkpoint verification/export utility. Checkpoint chaining is implemented.
- Add optional durable per-client cursor receipts and message acknowledgement/expiry policy. Stateless cursor polling is implemented.
- Expand cursor pagination beyond the MCP tool catalog when additional unbounded list endpoints are introduced. Tool arguments are validated against their advertised JSON Schemas and `tools/list` is cursor-paginated.
- Keep official MCP SDK conformance and concurrent multi-client stdio E2E coverage current as SDK protocol support evolves. The installed-wheel CI test covers SDK initialization, paginated tool discovery, structured tool calls, JSON-RPC validation errors, and two simultaneous clients sharing one WAL database.
- Add direct registration adapters when Craft Agents and other workspace-scoped clients publish stable non-interactive configuration APIs. Portable JSON and guided setup are implemented.

## Optional retrieval improvements

- Evaluate an optional neural local embedding adapter against the dependency-free feature-hash projection without changing the FTS-only default.
- Tune reciprocal-rank fusion, confirmation-freshness decay, deduplication threshold, and feedback weights on larger personal-memory relevance fixtures. The projection, inspectable score components, conservative importance adjustment, and near-duplicate context filtering are implemented.
- Evaluate stronger deterministic and optional model-assisted conflict classification. Session-end extraction of explicitly typed evidence into `proposed` memories, similarity-based conflict flags, and MCP review actions are implemented.

## Deliberately out of scope for the MVP

Multi-user authorization, remote synchronization, automatic secret detection, CRDT conflict resolution, and autonomous promotion to `active` need a larger security and correctness design.
