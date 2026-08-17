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

### Current implementation priority

P0 Decision Brief/evaluation, P1 Research-to-Decision provenance, P2 topic Wiki revisions, and P2.5 non-neural retrieval quality/latency are implemented. The active work is **P3 Decision Wiki lint and review**. Neural embedding remains an optional evaluated adapter and is not part of the active implementation path because its measured latency cost did not justify making it the default.

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

### P1 — Research-to-Decision provenance contract

Make the consequential path from a research question to a decision durable without turning Context Memory into a document store, browsing log, or connector platform.

1. Define an `investigation_id`-centered chain with explicit stages: research question and reason, consulted sources and access reason, extracted evidence, agent inference, resulting action, decision and rationale, and later observed outcome.
2. Record selectively. Preserve only research that materially informed an action or decision; do not capture every search query, page visit, browser interaction, or full source page.
3. Standardize investigation intent: the question being answered, why it matters, the decision it is expected to inform, relevant constraints, initiator, and start/completion times.
4. Standardize optional source metadata for `source_type`, stable source/page ID, canonical URI, source version, source updated time, retrieval time, section/anchor, reason for access, and analysis method.
5. Distinguish source-explicit claims, concise excerpts when needed for verification, and agent inference. Inferences carry confidence and exact evidence links and remain proposed until verified.
6. Link actions and decisions to the evidence and inference that motivated them. A decision records selected option, alternatives considered, rationale, governing constraints, and unresolved uncertainty; an action records what changed or was produced.
7. Allow later outcomes to confirm, weaken, dispute, or supersede the original decision without rewriting its historical evidence. Decision Briefs should be able to compare the expected and observed result.
8. Define idempotency and change detection from stable source identity plus version or a privacy-safe analyzed-content fingerprint. A changed page creates new evidence; it never rewrites an old event.
9. Add a transactional helper for recording several typed claims from one analyzed source under one investigation while preserving shared source provenance and causal links.
10. Document the client workflow: read with an authorized connector/browser, analyze in the client, record decision-relevant claims and their role in the investigation, and retain the external URI for reinspection.

**Exit gate:** the system can answer why a source was consulted, what was learned, how evidence differed from inference, what action and decision followed, and what outcome was later observed. Two analyses of the same source version are idempotent, a newer version is distinguishable, and every promoted memory can recover its investigation and source identity without storing the full page.

### P2 — Topic Wiki projection and revision lifecycle

Add durable, human-readable synthesis only after the Decision Brief contract is useful.

1. Introduce topic-oriented Wiki pages and immutable revisions with `proposed`, `published`, `stale`, and `rejected` states.
2. Link each material Wiki claim or section to source memories/events. Store generation metadata without making the generator authoritative.
3. Define a standard page shape: current position, why it exists, governing constraints, considered alternatives, trade-offs, decision timeline, observed outcomes, and open questions.
4. Mark affected published revisions stale when a cited memory is superseded, disputed, expired, or materially corrected. Never silently rewrite a published revision.
5. Render Markdown as a portable view/export. SQLite remains authoritative; generated Markdown is not a second writable source of truth.
6. Keep manually authored notes separate from generated sections so regeneration cannot overwrite user content.

**Exit gate:** a topic page can be regenerated from cited current memories, retains revision history, and becomes stale deterministically when its evidence changes.

### P2.5 — Non-neural retrieval quality and latency

Improve the shared `get_context`/`decision_context` path using FTS5, explicit aliases, local-hash, lifecycle state, provenance, and graph relationships. Preserve response compatibility and require measured evidence before changing ranking or acceptance thresholds.

FTS5 remains the primary scalable candidate index. Local-hash is not an FTS replacement; its purpose is to improve recall for lexical paraphrases, partial wording, typographical variation, and Korean expression changes while preserving FTS precision. Keep local-hash on the default path only when versioned evaluation demonstrates additional relevant hits that FTS misses without an unacceptable increase in negative-query false results or p95 latency.

1. **Freeze the evaluation baseline — completed.** The versioned Decision Brief fixture now includes irrelevant/negative questions, lexical paraphrases, duplicate summaries, current-versus-superseded decisions, conservative cross-project isolation, and a 120-distractor candidate set. Report schema v2 records Recall@5/MRR, current-decision accuracy, stale-decision leakage, negative-query false-result rate, source recovery, useful-history recall, duplicate rate, payload size, and p50/p95 latency for FTS-only and default local-hash modes. The first frozen run exposed a local-hash negative-query false-result gap and larger-set latency cost without changing production ranking.
2. **Remove query-path overhead without changing results — completed.** Source-event provenance and candidate usage are loaded in bounded batch queries, semantic scanning loads only IDs with vector JSON before hydrating vector-only winners, and query-count regression coverage prevents per-result usage/source reads. The frozen evaluation retains identical Recall@5/MRR, current-decision accuracy, stale leakage, source recovery, useful-history recall, duplicate rate, and negative-result behavior.
3. **Implement strict lexical candidate generation with fallback — completed.** Query terms are required as conjunction groups, while explicit aliases remain deterministic alternatives inside their term's group. The existing expanded `OR` query runs only when the strict pass cannot fill the requested result count, preserving its ranking as the fallback contract. Results expose `lexical_strategy` as `strict` or `broad_fallback`; regression tests cover query count, alias grouping, fallback, and unchanged changed-decision/paraphrase recall.
4. **Add a calibrated negative-result gate — completed.** Lexical matches remain accepted, while vector-only candidates must pass frozen-fixture-calibrated similarity and separation thresholds. Context results expose `accepted` or `no_confident_match` with the gate reason, lexical rank, original-query coverage, local-hash similarity, lexical/vector agreement, top-result scores, separation, and applied thresholds.
5. **Bound local-hash work — completed.** When lexical candidates exist, local-hash reads and reranks only those candidates. Vector-only fallback runs only when lexical recall fails (or an explicitly supplementing provider is selected), scans memories in deterministic ID order, and stops at 1,000 candidates or 25 ms before applying the calibrated acceptance gate. Retrieval diagnostics expose the scan mode, limits, evaluated count, and truncation state; embeddings can still be disabled entirely.
6. **Improve local-hash recall without sacrificing precision — completed.** The gate-aware schema-v3 embedding fixture freezes partial wording, typographical variation, abbreviation variation, and Korean spacing/particle cases. Local-hash v2 uses 1,024 dimensions plus Hangul-only syllable bigrams; restricting short features to Hangul suppresses common ASCII noise. On the 23-query fixture it improves Recall@5 from 0.632 (FTS) to 0.842 and MRR@5 from 0.570 to 0.789 while matching FTS's 0.25 negative-query result rate. Five-repeat Decision evaluation retains perfect frozen accuracy and zero negative false results; local-hash p95 increases from 0.483 ms at v1 to 0.629 ms at v2, an accepted 0.146 ms absolute cost.
7. **Add Decision Brief reranking — completed.** Rerank only the bounded results already accepted by `get_context`, using question intent, memory type/status, direct provenance, decision/rationale/constraint/outcome role, and penalties for unsupported, stale proposed, or repetitive handoff content. General `memory_search` semantics remain unchanged; each result exposes its base rank, inferred roles, total Decision Brief score, and every score component.
8. **Expand from decision seeds — completed.** From at most three retrieved current-decision seeds, traverse one hop across supports/depends-on/supersession edges and explicit or shared investigation relations. Consider at most 50 deterministic candidates, annotate already-retrieved related evidence, deduplicate new evidence, and charge additions to the existing item and character budgets. Diagnostics expose seeds, depth, limits, considered/added counts, truncation, and every expansion path; no second hop runs implicitly.
9. **Reduce cross-project discovery work — completed.** Generate at most 12 plausible projects from ranked memory FTS evidence, shared repository aliases, or fallback project identity matching. Discovery local-hash is restricted to those projects plus universally visible global memories; diagnostics expose the project bound and selected IDs. Existing confidence, separation, and ambiguity rules remain unchanged.
10. **Re-run the complete matrix after every ranking change — completed.** The final matrix covers unit tests, compilation, Decision Brief and embedding fixtures, cross-project calibration, installed-wheel restart persistence, and official MCP SDK 2.0 multi-client interoperability. Results are frozen in `benchmarks/results/p25-exit-2026-08-17.json`.

**Exit gate:** default non-neural retrieval materially lowers negative false results and p95 latency while preserving or improving Decision Brief accuracy, useful-history recall, citations, deterministic ranking, and conservative cross-project isolation.

**Exit result — passed 2026-08-17.** Against the frozen P2.5 baseline commit `aa4d49c`, default local-hash Decision Brief p95 fell from 3.118 ms to 0.695 ms (77.7%) and negative false results fell from 0.5 to 0.0. Recall@5, MRR@5, current-decision accuracy, source recovery, and useful-history recall remain 1.0; stale leakage, unsupported claims, and duplicates remain 0.0. Maximum compact context grew from 3,550 to 4,999 characters as inspectable diagnostics and bounded related evidence were added, remaining within the 6,000-character evaluation budget. Cross-project calibration retained 1.0 accuracy and ambiguity safety.

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
| Optional neural embeddings | Improved recall in an experiment but had an unacceptable default latency trade-off | Keep opt-in and out of the active implementation sequence; do not expand unless future non-neural evidence shows an unresolved material gap |
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
- Neural-model expansion, neural reranking, model acquisition, or making neural embeddings the default.
- Early Wiki UI work before the Decision Brief, revision, and lint contracts are validated.
- Repeating detailed implementation histories for completed Craft, checkpoint, token, backup, and audit work in the active roadmap; Git history and release notes are the appropriate record.

## Completed baseline

The repository already provides the prerequisites this plan builds on: compact `context_bootstrap`, immutable sequenced events, evidence-linked memory states, explicit review and conflict actions, bounded FTS/local-hash retrieval, optional neural evaluation, aliases and verified graph edges, cross-project discovery, client-neutral interim/final checkpoints, Craft workflow guidance, consistent backup, audit verification/anchors, maintenance, and durable cursor receipts. These are maintained as tested infrastructure rather than competing roadmap priorities.

## Still out of scope

Multi-user authorization, continuous remote synchronization, CRDT editing, autonomous promotion to `active`, automatic secret detection, and trusting provenance as proof that an external source is correct require separate security or correctness designs.
