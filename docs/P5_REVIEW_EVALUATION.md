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
