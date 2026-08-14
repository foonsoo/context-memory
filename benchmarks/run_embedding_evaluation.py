#!/usr/bin/env python3
"""Compare FTS-only, local-hash, and an explicit neural embedding adapter."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from context_memory.embeddings import LocalHashEmbedding, SentenceTransformerEmbedding
from context_memory.store import MemoryStore


MEMORIES = [
    ("postgres-backup", "Database recovery", "Restore PostgreSQL from an encrypted nightly snapshot."),
    ("oauth-rotation", "Identity maintenance", "Rotate OAuth signing credentials without ending active sessions."),
    ("invoice-tax", "Billing close", "Reconcile invoices, exchange rates, and tax adjustments at month end."),
    ("warehouse-route", "Fulfillment routing", "Select a carrier and warehouse for outbound parcel delivery."),
    ("ios-offline", "Mobile sync", "The iOS client queues edits while disconnected and merges them after reconnecting."),
    ("support-sla", "Escalation policy", "Page the incident lead when a priority-one support ticket nears its SLA."),
    ("memory-handoff", "Context handoff", "Save verified completion evidence so a later coding session can resume work."),
    ("search-synonyms", "Catalog discovery", "Expand product search with category synonyms and facet aliases."),
    ("fraud-velocity", "Payment risk", "Block repeated card attempts using chargeback and velocity rules."),
    ("push-delivery", "Notification delivery", "Retry failed mobile push messages with exponential backoff."),
    ("api-sandbox", "Developer portal", "Issue isolated API keys for testing integrations in a sandbox."),
    ("image-rendition", "Asset processing", "Generate thumbnail renditions and retain image metadata."),
    ("korean-memory-search", "기억 검색", "개인화된 기억을 빠르게 검색합니다."),
]

QUERIES = [
    ("recover the relational database from a protected copy", {"postgres-backup"}, "semantic"),
    ("change login signing keys while users stay signed in", {"oauth-rotation"}, "semantic"),
    ("month-end invoice tax reconciliation", {"invoice-tax"}, "lexical"),
    ("work offline on iphone then synchronize", {"ios-offline"}, "semantic"),
    ("continue coding in a future session using verified results", {"memory-handoff"}, "semantic"),
    ("priority one ticket escalation sla", {"support-sla"}, "lexical"),
    ("close the books after correcting foreign money charges", {"invoice-tax"}, "semantic"),
    ("choose the depot and courier that should ship a package", {"warehouse-route"}, "semantic"),
    ("detect too many repeated card payments", {"fraud-velocity"}, "semantic"),
    ("resend a phone notification after temporary failure", {"push-delivery"}, "semantic"),
    ("credentials for testing an integration in isolation", {"api-sandbox"}, "semantic"),
    ("make small image previews while preserving metadata", {"image-rendition"}, "semantic"),
    ("상품 검색 동의어와 패싯 별칭", {"search-synonyms"}, "multilingual"),
    ("나중 코딩 세션에서 검증된 결과로 작업 이어가기", {"memory-handoff"}, "multilingual"),
    ("postgres restoring encryptd backups", {"postgres-backup"}, "partial-wording"),
    ("retried faild notificationn deliveryy", {"push-delivery"}, "typographical"),
    ("priorityy-one suppport tickett slas", {"support-sla"}, "abbreviation-variation"),
    ("개인화 기억검색", {"korean-memory-search"}, "korean-spacing"),
    ("개인화된 기억으로 검색해줘", {"korean-memory-search"}, "korean-particle"),
    ("unrelated astronomy telescope calibration", set(), "negative"),
    ("botanical garden irrigation schedule", set(), "negative"),
    ("compose a jazz melody for brass instruments", set(), "negative"),
    ("marine biology coral spawning observations", set(), "negative"),
]


def load_fixture(path: str | Path) -> tuple[list[tuple[str, str, str]], list[tuple[str, set[str], str]]]:
    """Load relevance judgments without copying personal content into report metadata."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("fixture schema_version must be 1")
    memories_raw, queries_raw = raw.get("memories"), raw.get("queries")
    if not isinstance(memories_raw, list) or not memories_raw:
        raise ValueError("fixture memories must be a non-empty list")
    if not isinstance(queries_raw, list) or not queries_raw:
        raise ValueError("fixture queries must be a non-empty list")
    memories: list[tuple[str, str, str]] = []
    keys: set[str] = set()
    for index, item in enumerate(memories_raw):
        if not isinstance(item, dict):
            raise ValueError(f"fixture memory {index} must be an object")
        values = item.get("key"), item.get("title"), item.get("content")
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ValueError(f"fixture memory {index} requires non-empty key, title, and content")
        key, title, content = values
        if key in keys:
            raise ValueError(f"duplicate fixture memory key: {key}")
        keys.add(key)
        memories.append((key, title, content))
    queries: list[tuple[str, set[str], str]] = []
    for index, item in enumerate(queries_raw):
        if not isinstance(item, dict):
            raise ValueError(f"fixture query {index} must be an object")
        query, category, relevant_raw = item.get("query"), item.get("category"), item.get("relevant")
        if not isinstance(query, str) or not query.strip() or not isinstance(category, str) or not category.strip():
            raise ValueError(f"fixture query {index} requires non-empty query and category")
        if not isinstance(relevant_raw, list) or not all(isinstance(key, str) for key in relevant_raw):
            raise ValueError(f"fixture query {index} relevant must be a list of memory keys")
        relevant = set(relevant_raw)
        unknown = relevant - keys
        if unknown:
            raise ValueError(f"fixture query {index} references unknown memories: {sorted(unknown)}")
        queries.append((query, relevant, category))
    if not any(relevant for _, relevant, _ in queries):
        raise ValueError("fixture requires at least one positive relevance judgment")
    return memories, queries


@contextmanager
def embedding_mode(value: str) -> Iterator[None]:
    previous = os.environ.get("CONTEXT_MEMORY_EMBEDDINGS")
    os.environ["CONTEXT_MEMORY_EMBEDDINGS"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CONTEXT_MEMORY_EMBEDDINGS", None)
        else:
            os.environ["CONTEXT_MEMORY_EMBEDDINGS"] = previous


def evaluate(name: str, provider: Any, root: Path, repeats: int,
             memories: list[tuple[str, str, str]], queries: list[tuple[str, set[str], str]]) -> dict[str, Any]:
    db_path = root / f"{name}.db"
    started = time.perf_counter()
    if provider is None:
        with embedding_mode("off"):
            store = MemoryStore(db_path)
    else:
        store = MemoryStore(db_path, embedding_provider=provider)
    project = store.create_project(f"embedding-eval-{name}")
    ids: dict[str, str] = {}
    for key, title, content in memories:
        ids[key] = store.upsert_memory(project["id"], title, content, "fact", "active")["id"]
    index_ms = (time.perf_counter() - started) * 1000
    outcomes = []
    latencies = []
    reciprocal_ranks = []
    positive_hits = 0
    false_vector_only = 0
    for query, relevant_keys, category in queries:
        runs = []
        for _ in range(repeats):
            query_started = time.perf_counter()
            runs = store.search(project["id"], query, 5, statuses=["active"])
            latencies.append((time.perf_counter() - query_started) * 1000)
        gate = store._retrieval_gate(runs)
        accepted = runs if gate["status"] == "accepted" else []
        returned = [row["id"] for row in accepted]
        relevant = {ids[key] for key in relevant_keys}
        rank = next((index for index, memory_id in enumerate(returned, 1) if memory_id in relevant), None)
        if relevant:
            positive_hits += int(rank is not None)
            reciprocal_ranks.append(0.0 if rank is None else 1.0 / rank)
        else:
            false_vector_only += int(bool(returned))
        outcomes.append({
            "query": query,
            "category": category,
            "relevant": sorted(relevant_keys),
            "rank": rank,
            "returned": [next(key for key, memory_id in ids.items() if memory_id == value) for value in returned],
            "top_semantic_similarity": next((row["retrieval"]["semantic_similarity"] for row in runs
                                               if row["retrieval"]["semantic_similarity"] is not None), None),
            "retrieval_gate": {"status": gate["status"], "reason": gate["reason"]},
        })
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    provider_name = store._provider_name()
    store.close()
    positive_queries = sum(bool(keys) for _, keys, _ in queries)
    negative_queries = len(queries) - positive_queries
    categories = {}
    for category in sorted({category for _, _, category in queries}):
        selected = [item for item in outcomes if item["category"] == category and item["relevant"]]
        if selected:
            categories[category] = {
                "queries": len(selected),
                "recall_at_5": sum(item["rank"] is not None for item in selected) / len(selected),
                "mrr_at_5": statistics.mean(0.0 if item["rank"] is None else 1.0 / item["rank"] for item in selected),
            }
    return {
        "provider": provider_name or "fts-only",
        "recall_at_5": positive_hits / positive_queries,
        "mrr_at_5": statistics.mean(reciprocal_ranks),
        "negative_query_result_rate": false_vector_only / negative_queries if negative_queries else None,
        "negative_queries": {"count": negative_queries, "returned_count": false_vector_only,
                             "top_semantic_similarities": [item["top_semantic_similarity"] for item in outcomes
                                                            if not item["relevant"] and item["top_semantic_similarity"] is not None]},
        "categories": categories,
        "index_ms": round(index_ms, 3),
        "query_latency_ms": {
            "median": round(statistics.median(latencies), 3),
            "p95": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * .95))], 3),
        },
        "database_bytes": db_path.stat().st_size,
        "outcomes": outcomes,
    }


def run(model: str | None = None, repeats: int = 5, fixture: str | Path | None = None) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    memories, queries = load_fixture(fixture) if fixture else (MEMORIES, QUERIES)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        providers: list[tuple[str, Any]] = [("fts", None), ("local_hash", LocalHashEmbedding())]
        if model:
            providers.append(("neural", SentenceTransformerEmbedding(model)))
        results = {name: evaluate(name, provider, root, repeats, memories, queries) for name, provider in providers}
        if fixture:
            for result in results.values():
                result.pop("outcomes", None)
    return {
        "schema_version": 3,
        "fixture": {"source": "external" if fixture else "built-in-synthetic",
                    "memories": len(memories), "queries": len(queries), "repeats": repeats},
        "neural_model": model,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Local path or explicit sentence-transformers model identifier")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--fixture", help="Private schema-v1 JSON relevance fixture")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run(args.model, args.repeats, args.fixture)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
