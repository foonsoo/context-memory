# Roadmap

## Near term

- Tune cross-project discovery confidence thresholds on larger real-world multi-project fixtures. Normalized relevance, bounded path/name identity priors, recency, dominant-candidate selection, explicit low-confidence/ambiguity results, global-memory merging, and sparse-project fallback are implemented.
- Add client-neutral automatic handoff checkpoints. MCP 2026-07-28 Tasks can make an explicitly invoked checkpoint observable but cannot detect completion of the host agent's overall work item. Implement the feature in these increments:
  1. Add one idempotent `checkpoint_create` store operation, MCP tool, and CLI command with `mode=interim|final` and reasons such as `context_budget`, `elapsed`, `material_change`, `completed`, and `manual`.
  2. Persist an immutable checkpoint event containing the current goal, completed work, next executable step, blockers, source event cursor, HEAD/branch, dirty/clean state, changed-file summary, explicitly supplied test results, and optional client-reported context usage. Keep objective repository facts separate from unverified semantic summaries.
  3. Support configurable client-reported context thresholds: a soft checkpoint near 60% after material change and a hard checkpoint near 75%. When usage is unavailable, use client-neutral elapsed-time, durable-event-count, repository-change, and last-checkpoint-age fallbacks.
  4. Add cooldown, hysteresis, content hashing, and idempotency keys so repeated prompts or hooks do not create checkpoint storms. Skip checkpoints when no recoverable state changed.
  5. Make interim checkpoints non-mutating to Git and non-terminal for the memory session. Store recoverable working state without claiming completion or verification; do not automatically promote inferred summaries to active memory.
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
