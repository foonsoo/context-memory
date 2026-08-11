# Roadmap

## Near term

- Tune cross-project discovery confidence thresholds on larger real-world multi-project fixtures. Normalized relevance, bounded path/name identity priors, recency, dominant-candidate selection, explicit low-confidence/ambiguity results, global-memory merging, and sparse-project fallback are implemented.
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
