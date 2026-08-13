# Utility and evaluation

## Where this system is useful

Context Memory is intended for repeated work where facts change and mistakes must be explainable: software projects, research, operations, personal knowledge workflows, and multiple agents sharing one local workspace. It is most valuable when users need compact task context plus a path back to authoritative evidence.

It is less useful for one-off chats, pure document search, very large enterprise corpora, or workloads dominated by multi-hop entity questions. A search engine, RAG system, or temporal graph may fit those cases better.

## Expected practical gains

The architecture should reduce four costs:

1. Re-explanation: active decisions and constraints can be retrieved in a bounded context block.
2. False-memory recovery: each derived memory cites immutable source events.
3. Stale-memory risk: replacement and dispute states remain explicit and auditable.
4. Operations: SQLite and FTS5 avoid API keys, model downloads, vector services, and graph-database lifecycle work.

These are architectural expectations, not yet product-performance claims. They should be validated before claiming that the tool improves task success.

## Graph-first comparison

| Question or property | Evidence ledger + FTS5 | Graph-first memory |
|---|---|---|
| Why is this fact believed? | Direct event citation | Depends on whether edges retain source provenance |
| A decision changed | Explicit supersession/dispute transition | Often requires temporal edge modeling and conflict resolution |
| Multi-hop entity relationship | Limited | Strong |
| Ingestion without an LLM | Yes | Possible, but automatic graph extraction commonly needs one |
| Deterministic rebuild | Events and memories are authoritative | Only if raw sources and extraction versions are retained |
| Minimum local operations | One SQLite file | Commonly graph DB plus embedding/LLM services |
| Backup and inspection | Simple | More operationally involved |
| Semantic paraphrase recall | Limited without optional embeddings | Usually stronger with embeddings/extraction |

The recommended hybrid is ledger-first: keep events and verified memories authoritative, then build optional embeddings or graph edges as disposable projections. A projection can be regenerated after changing an extraction model without rewriting history.

## Evaluation plan

Create a public, synthetic benchmark with versioned scenarios rather than private user data:

- decision recall after 1, 10, and 50 sessions;
- changed decision where an older memory must not be presented as current;
- two contradictory sources that must surface as disputed;
- exact-source recovery for every answer;
- paraphrased queries that stress lexical retrieval;
- multi-hop relationship queries that favor graph systems;
- cold install time, idle memory, database size, retrieval latency, and context characters returned.

Compare at least these modes:

1. no persistent memory;
2. a rolling Markdown memory file;
3. Context Memory using FTS5;
4. Context Memory with an optional embedding projection;
5. a graph-first system.

Report task accuracy, stale-fact rate, unsupported-claim rate, source-recovery rate, p50/p95 latency, tokens or characters injected, installation steps, external services, and estimated API cost. The key hypothesis is not that FTS5 beats graphs on every query. It is that ledger-first memory yields better provenance and change handling at materially lower operational cost, while graph-first systems win on genuine multi-hop queries.

The P0 Decision Brief regression is runnable with `PYTHONPATH=src python3 benchmarks/run_decision_evaluation.py`. Its frozen schema-v1 fixture covers changed decisions, conflicting evidence, rejected alternatives, outcome feedback, missing rationale, and stale external sources. Each scenario uses an isolated disposable database and reports exact current-decision accuracy, stale-decision leakage, unsupported-claim rate, source recovery, useful-history recall, compact JSON character size, and p50/p95 latency. This deterministic suite is an exit-gate regression for `decision-brief/v1`, not evidence that the product improves human decisions.

### Local engineering baseline

An initial synthetic run on 2026-08-10 inserted 1,000 events and 1,000 sourced active memories into a fresh database, then executed 200 repeated context queries. On the development Mac with Python's SQLite 3.51.0, the resulting database was 2,834,432 bytes; context retrieval measured 2.657 ms p50 and 2.849 ms p95. Insertion took 0.309 seconds. This is a smoke/performance baseline, not a cross-system benchmark or evidence of task-quality improvement. Hardware, filesystem, SQLite build, query selectivity, and data shape affect the result.

Cross-project confidence can be checked separately with `PYTHONPATH=src python3 benchmarks/run_discovery_calibration.py`. The deterministic fixture spreads overlapping release vocabulary across realistic project domains, checks strong, dominant, low-confidence, and ambiguous selection behavior, and reports p50/p95 discovery latency. It is a calibration regression, not a task-quality benchmark; add scenarios before changing confidence thresholds and retain the scenario details with any reported result.

On 2026-08-11, a 12-domain run with 100 distractor memories per domain (1,204 memories total) passed all calibrated selections and ambiguity safety checks. Across 200 repeated discovery queries, retrieval measured 0.233 ms p50 and 0.249 ms p95 on the development Mac. These results supported retaining the existing confidence thresholds; they are environment-specific engineering evidence, not a general latency guarantee.

### MCP competitor run (2026-08-10)

The reproducible runner used 1,000 synthetic policy records and 200 exact-query repetitions. Every product ran as an MCP stdio subprocess without API keys. The final run used CPython 3.14.6, SQLite 3.53.4, Node 25.6.1, and npm 11.9.0 on arm64 macOS 26.3.1. Results and full environment metadata are stored in `benchmarks/results/local-2026-08-10.json`.

| Product | Query p50 / p95 | Ingest | Storage | Exact recall | Stale hidden | Source recovery | History retained | Multi-hop |
|---|---:|---:|---:|---|---|---|---|---|
| Context Memory 0.2.0 | 0.068 / 0.085 ms | 0.222 s | 2,711,552 B | yes | yes | yes | yes | yes |
| `@ideadesignmedia/memory-mcp` 2.0.3 | 0.258 / 0.295 ms | 0.338 s | 319,488 B | yes | yes | no | no | no |
| `@modelcontextprotocol/server-memory` 2026.7.4 | 0.489 / 0.704 ms | 0.005 s | 132,987 B | yes | no | no | yes | yes |

Interpretation requires care. The graph server supports batch entity creation, while the other two were ingested through individual MCP calls, so its ingestion time is not directly comparable. Context Memory stores source events, provenance joins, and append-only audit snapshots, explaining much of its larger file. The simple SQLite tool is compact and updates stale values effectively, but overwrites history and cannot recover a source. Both Context Memory and the graph server pass a two-hop relation test; Context Memory additionally filters superseded nodes by lifecycle status. Adding a new observation to the graph server leaves the old observation visible. Exact lexical retrieval succeeded in all three products.

The official graph package uses npm version `2026.7.4` while its MCP `serverInfo.version` reports `0.6.3`; the result records both values. Competitor npm versions are pinned by the runner so a future `latest` release cannot silently change a checked result.

All three default lexical modes missed the deliberately disjoint paraphrase `database durability repository` for `PostgreSQL persistence engine`. Context Memory recovered it after three explicit vocabulary aliases were configured. This demonstrates deterministic domain query expansion, not general semantic understanding; the benchmark records `default_paraphrase_recall=false` and `configured_alias_recall=true` separately.

## Known limitations

- Lexical FTS can miss semantically equivalent wording unless project aliases cover it; arbitrary paraphrases still need optional embeddings.
- Agent compliance with recording instructions is not guaranteed without client hooks.
- A local unencrypted database shares the OS-user trust boundary.
- There is no automatic retention, redaction, cross-device sync, or candidate-review UI yet.
- Provenance proves what source supported a memory; it does not prove the source itself was correct.
