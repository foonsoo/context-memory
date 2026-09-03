# Context Recall vNext

## Product contract

Context Recall helps an agent continue work without knowing a prior session,
memory lifecycle, exact keyword, or repository path. It should feel like an LLM
Wiki query while returning less material: an action-oriented context pack, not a
memory dump.

The primary interface is `context_recall(cwd, query, token_budget, max_items)`.
It is read-only and does not start a session. `cwd` is an identity hint rather
than a search boundary. The response contains the selected canonical project,
repository path, a few current facts/decisions/tasks, and source event IDs for
on-demand inspection.

## Budgets before tokenizers

The v1 implementation uses a deterministic, conservative token estimate. Exact
tokenizers are deliberately not a core dependency because tokenization varies by
model, adds package/model data, and does not improve retrieval recall. A client
may later provide an exact counter, but only measured overflow or material cost
error should justify it.

The default budget is 350 estimated tokens, with a hard range of 64–2,048. Long
memory bodies are reduced to query-bearing sentences. Provenance bodies,
retrieval diagnostics, lifecycle detail, and source text are omitted until the
client explicitly asks for them.

## Evaluation gate

Development is judged against repository-only continuation and an LLM Wiki
baseline using real prompts. Track continuation recall, wrong-project rate,
false absence, stale/error leakage, source recovery, returned tokens, and p50/p95
latency. Token optimization is accepted only when recall and false-absence do not
regress.

The frozen first bakeoff is `benchmarks/fixtures/continuation-scenarios-v1.json`:
24 Korean and English continuation phrasings across eight work cases. Run it with
`python benchmarks/run_continuation_evaluation.py --repeats 5`. It compares a
repository-only search, the legacy Context Memory context pack, a deterministic
exported-page proxy named `llm-wiki-snapshot`, and Context Recall vNext. The proxy
is deliberately labelled as such; a live LLM Wiki export can replace its adapter
without changing the fixture or metrics.

The first phrasing in every case starts from an empty placeholder directory.
All three blog-four phrasings do, making canonical path recovery part of the
acceptance case. Later phrasings use the canonical checkout so the same run also
measures the ordinary repository baseline. The report includes artifact,
decision, and next-step recovery separately, total continuation recall,
wrong-project and false-absence rates, stale/error leakage, source recovery,
returned and total-input estimated tokens, and search p50/p95 latency.

The existing memory system remains a data source and comparison baseline. New
vNext work should not add dependencies on session start/end, manual promotion,
or checkpoint lifecycle to the recall path.
