# Roadmap

## Near term

- Add optional encrypted backup envelopes and scheduled maintenance. Online consistent snapshots, policy-based retention, and checkpointed audit compaction are implemented.
- Add an offline audit-checkpoint verification/export utility. Checkpoint chaining is implemented.
- Validate all MCP arguments with a small JSON Schema validator and add pagination.
- Test interoperability against the official MCP SDK conformance suite and additional clients.

## Optional retrieval improvements

- Add a local embedding adapter and vector table without changing the FTS-only default.
- Add reciprocal-rank fusion, recency decay, and feedback signals. Deterministic aliases, verified graph traversal, and evaluation fixtures are implemented.
- Add background candidate extraction that only creates `proposed` memories with source citations and a review queue.

## Deliberately out of scope for the MVP

Multi-user authorization, remote synchronization, automatic secret detection, CRDT conflict resolution, and autonomous promotion to `active` need a larger security and correctness design.
