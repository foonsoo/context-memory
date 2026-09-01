# P5 review-surface evaluation

## Decision

Do not add a separate Decision Wiki review UI yet. Keep the existing typed `review_queue` and explicit MCP actions as the only review surface until deployed Wiki usage demonstrates a repeated workflow problem.

## Evidence inspected on 2026-08-20

The active local Context Memory database was opened in SQLite read-only mode. Its `schema_migrations` ledger contained migrations 1–12 and it had no `wiki_pages` or `wiki_revisions` tables. The repository's Decision Wiki schema begins at migration 13 and continues through migration 16. Consequently, the active database contained no real Wiki revision or Wiki review history from which to measure queue friction.

Repository regression scenarios still establish that the current queue exposes:

- typed memory-candidate and Wiki-revision entries;
- exact source/conflict context for proposed memories;
- deterministic lint findings for the latest non-rejected Wiki revision;
- explicit approve/reject routes for proposed revisions;
- no autonomous state change or second review lifecycle.

These tests establish functional coverage, not real-usage sufficiency. Absence of deployed usage must not be presented as evidence that the current surface is ergonomically sufficient.

## Re-evaluation gate

Reconsider a separate UI only after the current runtime is backed up, upgraded through migrations 13–16, and used for real Wiki revision review. Record concrete friction such as repeated inability to locate the next item, understand a lint finding, compare a replacement revision, or execute the intended explicit action. Prefer improving the versioned queue contract when the problem is client-neutral; build a separate UI only when interaction evidence shows that a tool response cannot address the problem cleanly.

## Deployment follow-up on 2026-08-20

The active database was backed up with SQLite's Online Backup API and then upgraded from migrations 1–12 through 13–16. Post-upgrade `doctor` and `PRAGMA integrity_check` both passed, and the investigation, Wiki, and source-reinspection tables are present. The deployment gate is therefore complete; the remaining gate is real usage that reveals repeated, recorded review friction.

The first live review cycle exposed two client-neutral contract problems. Omission lint treated accumulated workflow-task handoffs as missing Wiki claims, producing 19 warnings for a two-citation revision, and task-focused generation could retrieve relevant memories but still fail because no result mapped to a standard Wiki section. The latter failure did not explain the retrieval/section mismatch. The queue also placed four old memory candidates before the Wiki revision, while an already-running MCP client required restart before newly installed Wiki tools appeared. The first two issues are addressed in the core contract before considering a separate UI: workflow tasks are excluded from omitted-claim lint, reducing the observed revision from 19 warnings to 2, and citation-free generation errors now expose retrieval gate, item count, section counts, and a focused-query hint. Queue prioritization and client restart ergonomics remain observations to validate over additional review cycles.

The second live cycle reproduced both remaining observations. A new proposed Wiki revision again appeared fifth behind the same four old memory candidates, and the still-running client again lacked the installed Wiki tools. The cycle also exposed a generator/lint conflict: a superseded decision intentionally cited in `decision_timeline` was reported as a terminal-memory error. The queue now deterministically prioritizes actionable proposed Wiki revisions and exposes `queue_priority` plus `created_at`; accumulated memory candidates remain available immediately afterward. Terminal memories remain errors in current-guidance sections but are allowed when cited only in `decision_timeline` or `considered_alternatives`. The runtime-upgrade instructions now explicitly require graceful client reconnection. These repeated problems were resolved in the existing contract and operations documentation, so they do not justify a separate UI.

The third live cycle opened a fresh stdio MCP connection after the runtime update and verified all 33 core tools, including the five required Wiki create/generate/lint/review/transition operations. Using only MCP calls, it recorded the repository-verified SQLite-authority decision, generated a two-citation Wiki revision, received deterministic lint `pass` with zero findings, observed the proposed revision first in `review_queue`, and explicitly published it. This is the first clean published revision in the upgraded active runtime. The corrected client-neutral contract is sufficient for this workflow; no separate review UI is warranted by the observed evidence.

The first reader-side cycle used a fresh stdio connection to browse that published page, inspect its backlinks, and export Markdown. Browse returned three durable page identities, including two rejected-only pages with no current revision, while export produced the single renderable document. The difference was correct but required the reader to infer why browse `page_count` was three and export `page_count` was one. The additive navigation contract now labels every page with `reader_state` and `renderable`, reports renderable/unrenderable window counts, and makes export report source and skipped page counts. Rejected audit history remains visible; it is no longer ambiguous whether a page can be opened or exported.

After installing that contract into the shared runtime and opening another fresh stdio connection, the live database returned the expected per-page states and counts: one renderable published page, two rejected-only unrenderable pages, and an export with one document from three source pages. A second evidence-backed page was then generated from the verified reader-contract decision and the existing SQLite-authority decision. It passed deterministic lint with zero findings, appeared first in the review queue, and was explicitly published. The resulting four-page browse window reported two renderable and two unrenderable pages; selecting the original page returned the new published page as a backlink through their shared cited memory. Markdown export produced two documents, reported two skipped source pages, and included correct index, previous/next, and bidirectional related-page links. No additional reader-side contract or UI friction appeared in this multi-page cycle.

The 2026-09-01 adoption cycle revisited the previously rejected product-direction page using a fresh MCP connection and repository-verified current direction. Generation produced a two-citation revision, but deterministic lint warned about an automatic-commit preference and a repository-location decision that matched only broad product-name terms. This repeated the earlier omission-noise failure outside the task type. Omission lint is now bounded to the leading ten direct lexical candidates; broad and vector-only retrieval remains available to generation but no longer creates low-confidence review warnings. After a consistent pre-upgrade backup and runtime reinstall, the same immutable revision passed lint with zero findings, appeared first in the typed review queue, and was explicitly published. The live browse window moved from two renderable/two unrenderable pages to three renderable/one unrenderable page and returned both expected backlinks. This cycle found and repaired a client-neutral lint issue; it did not reveal evidence that a separate UI would improve the workflow.
