# Roadmap

## Near term

- Add optional encrypted backup envelopes and scheduled maintenance. Online consistent snapshots, policy-based retention, and checkpointed audit compaction are implemented.
- Add an offline audit-checkpoint verification/export utility. Checkpoint chaining is implemented.
- Add optional durable per-client cursor receipts and message acknowledgement/expiry policy. Stateless cursor polling is implemented.
- Validate all MCP arguments with a small JSON Schema validator and add pagination.
- Test interoperability against the official MCP SDK conformance suite and additional clients.
- Add direct registration adapters when Craft Agents and other workspace-scoped clients publish stable non-interactive configuration APIs. Portable JSON and guided setup are implemented.

## Optional retrieval improvements

- Evaluate an optional neural local embedding adapter against the dependency-free feature-hash projection without changing the FTS-only default.
- Tune reciprocal-rank fusion, confirmation-freshness decay, and feedback weights on personal-memory relevance fixtures. The projection and inspectable score components are implemented.
- Add background candidate extraction that only creates `proposed` memories with source citations and a review queue.

## Deliberately out of scope for the MVP

Multi-user authorization, remote synchronization, automatic secret detection, CRDT conflict resolution, and autonomous promotion to `active` need a larger security and correctness design.
