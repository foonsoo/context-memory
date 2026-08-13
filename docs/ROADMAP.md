# Roadmap

## Product direction

Context Memory is evolving into a provenance-aware **Decision Wiki**. Its primary job is not to archive documents. It should reconstruct the decisions, rationale, constraints, alternatives, outcomes, and unresolved questions relevant to a user's current choice.

The core remains client-neutral and evidence-ledger-first:

```text
Accessible source, conversation, or repository state
  -> immutable evidence events
  -> reviewed atomic memories
  -> topic-oriented Decision Wiki revisions
  -> query-specific Decision Brief
```

External clients may read Confluence or another source with the user's existing access. The core stores useful analyzed claims and source identity/version metadata, not uploaded files, page replicas, or OCR output. SQLite events and memories remain authoritative; Wiki content is a cited, rebuildable projection.

## Priority order

### P0 — Decision Brief contract and evaluation

Prove that the system helps a user make a better current choice before adding a persistent Wiki schema.

1. Define a structured Decision Brief containing:
   - the question being decided;
   - current applicable decisions;
   - rationale and constraints;
   - alternatives previously considered;
   - observed outcomes and trade-offs;
   - chronological changes and superseded decisions;
   - disputes, uncertainty, missing evidence, and open questions;
   - exact source event and memory citations.
2. Add a read-only `decision_context` operation that composes this evidence bundle from current retrieval and graph primitives. The server must not generate an uncited recommendation or promote model output.
3. Keep `get_context` backward compatible. Reuse its character/item budgets, project discovery, lifecycle filtering, and inspectable ranking instead of building a second search stack.
4. Create versioned synthetic decision scenarios covering changed decisions, conflicting evidence, rejected alternatives, outcome feedback, missing rationale, and stale external sources.
5. Measure current-decision accuracy, stale-decision leakage, unsupported-claim rate, source recovery, useful-history recall, context size, and p50/p95 latency. Do not tune retrieval globally until a failure is demonstrated by these scenarios.

**Exit gate:** a Decision Brief reconstructs the correct current decision and material history with citations, while clearly separating evidence from inference and uncertainty.

### P1 — External source-analysis evidence contract

Make analysis of accessible pages durable without turning Context Memory into a document store or connector platform.

1. Standardize optional event metadata for `source_type`, stable source/page ID, canonical URI, source version, source updated time, retrieval time, section/anchor, and analysis method.
2. Distinguish source-explicit claims, concise excerpts when needed for verification, and agent inference. Inference remains proposed until verified.
3. Define idempotency and change detection from stable source identity plus version or a privacy-safe analyzed-content fingerprint. A changed page creates new evidence; it never rewrites an old event.
4. Add helpers for recording several typed claims from one analyzed source while preserving a shared source reference and transactional provenance.
5. Document the client workflow: read with an authorized connector/browser, analyze in the client, record only durable decision-relevant claims, and retain the external URI for reinspection.

**Exit gate:** two analyses of the same source version are idempotent, a newer version is distinguishable, and every promoted memory can recover its source identity without storing the full page.

### P2 — Topic Wiki projection and revision lifecycle

Add durable, human-readable synthesis only after the Decision Brief contract is useful.

1. Introduce topic-oriented Wiki pages and immutable revisions with `proposed`, `published`, `stale`, and `rejected` states.
2. Link each material Wiki claim or section to source memories/events. Store generation metadata without making the generator authoritative.
3. Define a standard page shape: current position, why it exists, governing constraints, considered alternatives, trade-offs, decision timeline, observed outcomes, and open questions.
4. Mark affected published revisions stale when a cited memory is superseded, disputed, expired, or materially corrected. Never silently rewrite a published revision.
5. Render Markdown as a portable view/export. SQLite remains authoritative; generated Markdown is not a second writable source of truth.
6. Keep manually authored notes separate from generated sections so regeneration cannot overwrite user content.

**Exit gate:** a topic page can be regenerated from cited current memories, retains revision history, and becomes stale deterministically when its evidence changes.

### P3 — Decision Wiki lint and review

1. Detect missing citations, missing sources, citations to terminal memories, unresolved disputes, stale page revisions, and current memories omitted from a relevant page.
2. Detect unsupported recommendations and require them to be labeled as inference rather than evidence.
3. Report source-version age as a reinspection prompt, not as proof that an external page changed. The core must not claim freshness it cannot verify.
4. Integrate lint findings into the existing review queue and explicit approve/reject/supersede/dispute workflow instead of creating an unrelated review system.
5. Make lint deterministic where possible. Optional model-assisted checks must be separately labeled and must not change active state autonomously.

**Exit gate:** known stale, contradicted, or unsupported Wiki claims are surfaced before a Decision Brief presents them as current guidance.

### P4 — Source revalidation and client adapters

1. Let a client request reinspection when a cited external source is old, unavailable, or known to have a newer version.
2. Add thin client examples for Confluence-like pages only after the generic source-analysis contract is stable.
3. Keep authorization, page retrieval, and vendor-specific APIs outside the core server. Add direct adapters only when a stable non-interactive client API exists and real use demonstrates repeated manual friction.

**Exit gate:** a client can refresh relevant evidence without granting the core persistent access to an external knowledge system.

### P5 — Human navigation and export

Add browsing, backlinks, topic indexes, and richer Markdown export only after real Decision Wiki revisions exist. A review UI is useful here but is not a prerequisite for P0–P3.

## Existing work: overlap and disposition

| Existing area | Decision Wiki relationship | Disposition |
|---|---|---|
| Immutable events, provenance, lifecycle states, review actions | Authoritative foundation | Retain unchanged |
| `get_context`, FTS/local-hash ranking, aliases, graph traversal | Retrieval foundation for Decision Briefs | Reuse; extend rather than duplicate |
| Cross-project discovery calibration | Helps locate prior context from another checkout/project | Retain as a safety regression; expand only for demonstrated Decision Brief failures |
| Optional neural embeddings and ranking tuning | May improve paraphrase recall | Defer new tuning until the Decision Wiki benchmark identifies a retrieval gap |
| Conflict classification | Directly supports decision history and lint | Fold into P0/P3 scenarios; avoid a separate parallel workstream |
| Session-end extraction and review queue | Produces reviewed atomic memories | Reuse; do not auto-publish Wiki revisions from unreviewed output |
| Interim/final checkpoints and handoffs | Work recovery rather than durable knowledge synthesis | Keep as completed infrastructure; exclude checkpoints from topic Wiki unless explicitly cited |
| Craft durable-kind contract and client templates | Portable evidence workflow | Keep as completed infrastructure; update templates only when the source-analysis contract stabilizes |
| Token-footprint optimization | Protects bounded startup/query cost | Keep the existing baseline and add Decision Brief payload measurements |
| Backup, audit anchors, maintenance, cursor receipts | Operational foundation | Keep; no active roadmap expansion unless a Decision Wiki schema requires migration/export coverage |
| Direct registration adapters | Unrelated to validating Decision Wiki value | Defer |

There is no architectural conflict between the current evidence ledger and the Decision Wiki. The principal risk is creating a second authoritative knowledge store. This roadmap avoids that by making Wiki revisions cited projections over events and memories.

## Removed from the active roadmap

- Uploaded document/file storage, binary asset management, OCR, and general-purpose document ingestion.
- A server-owned Confluence crawler or persistent vendor credentials.
- An independently indexed Wiki search stack that duplicates `get_context`.
- Uncited free-form Wiki generation or autonomous publication/promotion to active state.
- Broad embedding, ranking, or discovery tuning without a failing decision-quality scenario.
- Early Wiki UI work before the Decision Brief, revision, and lint contracts are validated.
- Repeating detailed implementation histories for completed Craft, checkpoint, token, backup, and audit work in the active roadmap; Git history and release notes are the appropriate record.

## Completed baseline

The repository already provides the prerequisites this plan builds on: compact `context_bootstrap`, immutable sequenced events, evidence-linked memory states, explicit review and conflict actions, bounded FTS/local-hash retrieval, optional neural evaluation, aliases and verified graph edges, cross-project discovery, client-neutral interim/final checkpoints, Craft workflow guidance, consistent backup, audit verification/anchors, maintenance, and durable cursor receipts. These are maintained as tested infrastructure rather than competing roadmap priorities.

## Still out of scope

Multi-user authorization, continuous remote synchronization, CRDT editing, autonomous promotion to `active`, automatic secret detection, and trusting provenance as proof that an external source is correct require separate security or correctness designs.
