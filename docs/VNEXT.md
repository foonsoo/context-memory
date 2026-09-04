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

## First retrieval iteration

`benchmarks/analyze_continuation_failures.py` makes project candidates,
confidence, evidence quality, and selection reasons visible for every frozen
prompt. The first report showed three concrete failures: whole-database recall
treated the first memory as a project decision, canonical cwd queries did not
combine related active facts/decisions/tasks, and Korean continuation nouns were
split from English memory terminology or hidden by inflection.

The smallest measured correction uses the existing project aggregation gate,
adds bounded vNext-only Korean lexical bridges, treats a cwd with active memory
as a project identity hint, and adds at most one active sibling per memory type.
An unselected placeholder is now returned as `project: null`, rather than being
reported as the recalled project. Prompt outcomes expose selected project,
retrieval status, and selection reason for future failure analysis.

The 10-repeat result is
`benchmarks/results/continuation-vnext-retrieval-2026-09-03.json`. Relative to
the frozen vNext baseline, continuation recall improved from 0.125 to 0.250,
artifact recovery from 0.222 to 0.389, decision recovery from 0.521 to 0.979,
next-step recovery from 0.444 to 0.833, wrong-project rate from 0.292 to 0, and
false absence from 0.417 to 0.042. Stale/error leakage remains 0 and source
recovery remains 1.0. Median returned estimated tokens increased from 303 to
346.5, still under the 350-token item-pack contract; p95 search latency changed
from 0.246 ms to 0.468 ms.

One failure remains: `scope 바뀐 뒤 다음 단계 진행하자` is conservatively
rejected as ambiguous. Artifact paths are the next largest quality gap; add
repository artifact extraction only after separately measuring its latency and
token cost.

## Path identity and artifact normalization

The remaining scope failure exposed a resolver defect rather than a retrieval
threshold problem. Registered path aliases were used as discovery priors but
not as exact inputs to `resolve_project`. On macOS temporary paths, `/var` and
`/private/var` normalization made that visible; project-name fallback hid it
when the checkout basename happened to equal the project name. Exact normalized
path aliases now resolve the registered project and create its workspace scope
before name fallback.

Artifact failure classification found that six missing blog checks already had
enough evidence in one memory: the first file used a full directory while the
next two filenames elided it. vNext now normalizes those explicit path lists
inside the selected memory and charges the added strings to the estimated-token
budget. It does not scan repository files. The remaining artifact misses exist
only in repository content and remain evidence for a separately bounded,
measured repository adapter rather than justification for adding one implicitly.

The follow-up 10-repeat result is
`benchmarks/results/continuation-vnext-paths-2026-09-04.json`. Compared with the
previous iteration, continuation recall improved from 0.250 to 0.375, artifact
recovery from 0.389 to 0.583, decision recovery from 0.979 to 1.0, and false
absence from 0.042 to 0. Wrong-project rate and stale/error leakage remain 0;
source recovery remains 1.0. All three empty-cwd blog prompts now recover the
three prior files, six-part structure, intended style, episode-four topic, and
canonical repository path.
