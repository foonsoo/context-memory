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

P0 Decision Brief/evaluation, P1 Research-to-Decision provenance, P2 topic Wiki revisions, P2.5 non-neural retrieval quality/latency, P3 Decision Wiki lint/review, P4 source revalidation, and the P5 navigation/export contracts are implemented. The active runtime was backed up and upgraded through Wiki migrations 13–16 on 2026-08-20. Two live Wiki review cycles produced actionable evidence and corresponding client-neutral fixes for omission noise, generation diagnostics, actionable queue ordering, historical terminal citations, and runtime-restart guidance. A third cycle on a fresh stdio connection verified the complete installed Wiki MCP surface and published the first lint-clean revision through the corrected queue. The first reader-side cycle exposed and resolved ambiguity between browse pages and exportable pages by adding explicit reader/renderability states and skipped-page counts. After deployment, a second cycle verified those fields plus real backlinks and linked Markdown across two published pages without finding additional friction.

P6 behavior-preserving compaction and maintainability passed its exit gate on
2026-08-24. The next active priority is P7-1: close the local distribution
release gap before presenting `uvx context-memory` as an available install.
Hosted-service work in P7 remains conditional on choosing that product track.
Neural embedding remains an optional evaluated adapter and is not part of the
active implementation path because its measured latency cost did not justify
making it the default.

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

1. **Detect evidence and lifecycle gaps — completed.** Deterministically report missing citations, missing sources, citations to terminal memories, unresolved disputes, stale page revisions, and relevant current memories omitted from a revision. The read-only lint contract exposes stable finding codes and never changes memory or Wiki state.
2. **Detect unsupported recommendations — completed.** Conservative English/Korean recommendation signals are checked against explicit investigation-claim and verified memory relations. Unsupported recommendation-like claims and recommendations labeled as evidence instead of inference receive stable error findings; lint remains deterministic and read-only.
3. **Report source-version age conservatively — completed.** After 30 days, cited investigation source versions receive a deterministic reinspection prompt with their observed age and version metadata. The finding explicitly records that no external change was verified and does not mark the revision stale or claim freshness.
4. **Integrate lint with explicit review — completed.** The existing `review_queue` returns typed memory candidates and the latest non-rejected revision for each Wiki page when it is proposed or has lint findings. Memory candidates expose approve/reject and conflict-dependent supersede/dispute actions; proposed Wiki revisions expose approve/reject routes through the existing revision transition. No lint-specific state or autonomous action path is introduced.
5. **Keep lint deterministic and read-only — completed.** The v1 contract identifies `check_mode=deterministic_rules`, `model_assisted=false`, and both observed and autonomous state-change flags as false. Repeated lint over unchanged authoritative state returns an identical result and creates no audit mutation. Any future model-assisted checks require a separately labeled contract and cannot change active state autonomously.

**Exit gate:** known stale, contradicted, or unsupported Wiki claims are surfaced before a Decision Brief presents them as current guidance.

**Exit result — passed 2026-08-20.** Deterministic regression scenarios surface terminal and disputed citations, stale revisions, omitted current evidence, unsupported recommendations, and aged-source reinspection prompts in the same explicit review queue used for memory and revision decisions. Repeated lint results are identical over unchanged state, read-only execution creates no audit entry, and no model-assisted or autonomous mutation path exists.

### P4 — Source revalidation and client adapters

1. **Request source reinspection — completed.** A client can append an idempotent reinspection request for one recorded source analysis when it is old, unavailable, or known to have a newer version. The request preserves the stable source identity, inspected version, optional known version, and reason while explicitly assigning retrieval to the client; the core performs no external fetch.
2. **Add thin Confluence-like client examples — completed.** Versioned, schema-checked examples cover an authorized initial page analysis and a reinspection that records a newer page version without rewriting the earlier analysis. They use only generic source-analysis tools and retain no full page or credentials.
3. **Keep vendor access outside the core — completed.** The examples explicitly assign authorization and retrieval to the client. Direct adapters remain deferred until a stable non-interactive API and repeated real-world friction justify one.

**Exit gate:** a client can refresh relevant evidence without granting the core persistent access to an external knowledge system.

**Exit result — passed 2026-08-20.** The portable workflow records a reinspection request, returns the stable source route without a core fetch, and appends a newer analyzed version through the same generic provenance contract. Every illustrated MCP call is validated against the shipped schemas; the examples contain neither credentials nor full page bodies.

### P5 — Human navigation and export

1. **Add bounded Wiki browsing and backlinks — completed.** A read-only, paginated page/topic index exposes each page's current revision and lifecycle counts. Selecting a page returns reverse citation backlinks from its cited memories to other current pages. Navigation reads authoritative Wiki tables and does not duplicate the existing text-search stack.
2. **Add richer Markdown export — completed.** A bounded browse window renders a deterministic index and stable page-ID paths with page/revision metadata, adjacent-page navigation, and related-page links derived from shared citations. SQLite remains the sole writable authority.
3. **Evaluate the review surface — completed; UI deferred.** Read-only inspection found that the active database has only migrations 1–12 and therefore no deployed Wiki revision history. Existing regression scenarios prove functional queue coverage but cannot establish real-usage ergonomics. Do not add a separate UI until a backed-up upgrade through migrations 13–16 and subsequent use reveal repeated, recorded friction.

**Exit gate:** a user can discover current topic pages, follow provenance-backed relationships, and export a useful human-readable Wiki without creating a second source of truth.

**Exit result — contract passed, adoption evidence pending 2026-08-20.** Navigation, backlinks, and linked Markdown export satisfy the read-only product contract in regression coverage. The [review-surface evaluation](P5_REVIEW_EVALUATION.md) records why a separate UI is deferred and defines the evidence required to reconsider it. After that evaluation, the active database was backed up with SQLite's Online Backup API, upgraded from migrations 1–12 through 13–16, and verified with `doctor` and `PRAGMA integrity_check`; real review-usage evidence is still pending.

### P6 — Behavior-preserving compaction and PEP 8 maintainability

Refactor only where the current implementation has measured maintenance cost. Preserve the SQLite schema, migrations, public `MemoryStore` import, CLI commands, MCP tool names and schemas, response shapes, ranking diagnostics, and deterministic behavior throughout this phase.

1. **Freeze the compatibility baseline — completed.** The versioned P6 fixture snapshots every MCP tool name/schema/annotation and every CLI command/argument contract, exercises every exported record type through an import/export round trip, and freezes normalized compact-context and Decision Brief responses, exact Wiki Markdown, retrieval constants, and the accepted P2.5 evaluation thresholds. Runtime timings and sub-micro score noise are normalized while structural and semantic response changes remain detectable. The baseline adds one characterization test without changing production code or dependencies.
2. **Adopt PEP 8 as the repository convention — completed.** Ruff 0.16.1 is pinned in the development-only extra with explicit `E`, `W`, `F`, `I`, and `N` rules, 79-character code, 72-character comments/docstrings, and deterministic formatting. CI runs both lint and format checks without adding a runtime dependency. The positive adoption list covers every production Python module and all pass without per-file ignores. The large `store.py` module completed behavior-preserving structural formatting, comment/docstring wrapping, and SQL/diagnostic string wrapping before joining the list.
3. **Remove proven dead or redundant code first — completed.** The reproducible public/private call inventory covers store-internal calls, MCP, CLI, hooks, Tasks, tests, and other Python consumers. All 28 private `MemoryStore` methods have static call sites, Vulture reports no unused production symbol, and public methods remain compatibility surface; therefore this evidence-gated slice correctly removes no production code. The inventory test freezes 89 methods (61 public including the constructor, 28 private) and fails if a future private method loses every static call site. A private helper or compatibility branch may be removed only when characterization tests prove it unreachable or a documented deprecation path exists. Do not treat shorter code as inherently better.
4. **Extract pure domain helpers from `store.py` — completed.** The
   retrieval thresholds, project-selection policy, and negative-result gate now
   live in a directly tested, database-free retrieval module. Checkpoint test
   result validation and normalization also live in a directly tested pure
   validation module. Checkpoint trigger selection, fallback priority, and
   suppression policy are now evaluated from already-observed state in a
   directly tested database-free module. Wiki revision Markdown rendering now
   operates on already-loaded page and revision data in a directly tested pure
   helper. Wiki citation, lifecycle, source-age, and result-contract lint rules
   similarly operate on state observed by `MemoryStore` in a database-free
   helper. `MemoryStore` retains compatibility delegates and constant exports.
   Audit-chain checkpoint construction, bundle serialization, and offline
   verification now live in a directly tested pure helper. Deterministic JSON
   serialization is shared through one tested primitive. Transaction boundaries
   and SQL remain in `MemoryStore`.
5. **Split persistence by bounded domain behind a stable facade — completed.** Composed repositories own project/alias/workspace/scope/session/evidence and durable event writes/cursor receipts, memory lifecycle/review/embedding persistence, ranked and cross-project retrieval candidate queries, investigation, Wiki, checkpoint state/recovery hashing, policy/search-health/maintenance/audit export, portable project transfer, and database backup/index rebuild operations using the facade's transaction connection. Dedicated context and Decision Brief assemblers own their bounded response composition, reranking, graph expansion, and outcome comparison. `context_memory.store.MemoryStore` remains the compatibility facade without mixin inheritance or repository-to-repository dependencies. A direct-SQL ownership audit leaves only connection/transaction setup, migrations, common row/idempotency/audit primitives, and startup embedding bootstrap in the facade; feature persistence SQL is behind bounded components.
6. **Decompose CLI and MCP declarations — completed.** CLI parser construction is separate from execution, and every runtime command is covered by an explicit tested registry; only the pre-database migration path remains an intentional early command. The former 391-line dispatch function is now 48 lines with bounded handlers. MCP runtime pagination and validation are separate from ordered lifecycle, checkpoint/event, memory, investigation, Wiki, and administration schema catalogs. Frozen CLI/MCP contracts, tool pagination, profiles, validation errors, and zero runtime dependencies are preserved.
7. **Consolidate repeated infrastructure — completed.** Optional SQLite row conversion and existence probes share bounded persistence primitives; idempotency and recovery hashing use the canonical JSON digest; UTC timestamp generation uses one clock primitive while `store.now()` remains the patchable compatibility seam. Canonical JSON serialization and checkpoint test-result validation remain centralized in their existing pure modules. Repository methods retain their prior exception types, return shapes, and transaction ownership.
8. **Verify every slice — completed.** The full Python 3.11–3.14 matrix, installed-wheel/empty-workspace/restart test, official MCP SDK 2.0 multi-client interoperability test, `compileall`, frozen Decision/embedding/discovery evaluations, export/import round trip, query-count regressions, Ruff lint/format, and `git diff --check` all pass. The matrix tests use only the standard library and the installed package remains free of runtime dependencies.

**Exit gate:** production modules conform to the agreed PEP 8 checks; no feature module or command dispatcher remains a maintenance hotspot; public contracts and database compatibility are unchanged; all correctness, packaging, interoperability, and performance gates pass without adding a runtime dependency.

**Exit result — passed 2026-08-24.** GitHub Actions run `32712907832` passed lint, build/interoperability, and Python 3.11–3.14 jobs. The final frozen local runs preserved Decision Recall@5/MRR at 1.0 with zero negative results (local-hash p50/p95 0.458/0.717 ms), embedding Recall@5/MRR at 0.842/0.789 with the accepted 0.25 negative rate (p50/p95 0.743/1.184 ms), and discovery accuracy/ambiguity safety at 1.0/true across 1,204 memories (p50/p95 1.291/1.394 ms). Compatibility snapshots and batched-query limits remain unchanged, the installed wheel survives restart, and `dependencies = []` remains intact.

### P7 — Distribution and hosted-service readiness

Keep the current local-first, single-user stdio product and a possible hosted service as explicit product tracks. Do not weaken the local zero-runtime-dependency path to satisfy hosted deployment needs.

1. **Close the release gap — in progress.** Reconcile the README's `uvx`/published-package instructions with the current absence of a release tag, add changelog and release policy, build reproducible wheels, test TestPyPI before PyPI, and publish signed/provenance-bearing artifacts only through an explicit release workflow.
2. **Define support and compatibility policy — completed.** The local release
   policy now states the tested Python and OS tiers, capability-based SQLite
   requirement, MCP protocol/SDK baseline, compatibility fixture guarantees,
   pre-1.0 deprecation rules, forward-only migration contract, backup/restore
   compatibility, and a restore-forward failed-upgrade procedure. Hosted and
   multi-user service support remains explicitly outside this release line.
3. **Add user-owned controls.** Provide a minimal review/control surface for onboarding, memory provenance inspection, approve/reject/correct/delete/export, storage status, backup/restore, and complete uninstall. This surface must call the same authoritative service layer and must not become a second writable Wiki store. A separate browsing UI for the local product remains evidence-gated; a hosted service cannot launch without these user controls.
4. **Design hosted identity and isolation before remote access.** Add authenticated users, tenant/project authorization, session revocation, least-privilege service roles, rate limits, quotas, and tests that prevent cross-tenant search, export, event polling, and backup access. A single bearer token is not sufficient for an untrusted network.
5. **Add transport and API production controls.** Put TLS and trusted-proxy handling at the deployment boundary; define request/body/time limits, pagination and cancellation, stable error codes, idempotency retention, CORS policy where applicable, and backward-compatible API versioning.
6. **Add data governance and privacy operations.** Document collection purpose, retention defaults, user export and deletion, account/project erasure, backup expiry, regional/storage choices, incident handling, and handling of sensitive data. Evaluate secret/PII detection as a warning and policy layer without claiming perfect detection.
7. **Make operations observable and recoverable.** Add health/readiness endpoints, structured redacted logs, metrics for latency/errors/queue depth/database size, trace/request IDs, migration status, backup age, restore drills, disk-full behavior, and operator runbooks with alert thresholds.
8. **Prove service behavior under load and failure.** Test concurrent readers/writers, process interruption, WAL growth, slow clients, malformed/oversized requests, migration rollback or forward recovery, backup restoration, and capacity limits. Define service-level objectives only after these measurements.
9. **Finish the user journey.** Test first install, client registration, first useful memory, next-session retrieval, review/correction, source reinspection, backup/restore, upgrade, and uninstall with non-developer users. Add concise diagnostics and support documentation for every failed step.

**Exit gate:** a new user can install or sign up, understand what is stored and why, complete the memory lifecycle, recover or delete their data, and receive a supported upgrade. Hosted deployment additionally passes tenant-isolation, authentication, load, failure-recovery, privacy, and observability gates.

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

Continuous peer-to-peer synchronization, CRDT editing, autonomous promotion to `active`, claiming perfect automatic secret detection, and treating provenance as proof that an external source is correct remain out of scope. Multi-user authorization is out of scope for the local product but is a mandatory P7 design and exit gate for any hosted service.
