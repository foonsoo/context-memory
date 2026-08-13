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
    ("unrelated astronomy telescope calibration", set(), "negative"),
    ("botanical garden irrigation schedule", set(), "negative"),
    ("compose a jazz melody for brass instruments", set(), "negative"),
    ("marine biology coral spawning observations", set(), "negative"),
]


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


def evaluate(name: str, provider: Any, root: Path, repeats: int) -> dict[str, Any]:
    db_path = root / f"{name}.db"
    started = time.perf_counter()
    if provider is None:
        with embedding_mode("off"):
            store = MemoryStore(db_path)
    else:
        store = MemoryStore(db_path, embedding_provider=provider)
    project = store.create_project(f"embedding-eval-{name}")
    ids: dict[str, str] = {}
    for key, title, content in MEMORIES:
        ids[key] = store.upsert_memory(project["id"], title, content, "fact", "active")["id"]
    index_ms = (time.perf_counter() - started) * 1000
    outcomes = []
    latencies = []
    reciprocal_ranks = []
    positive_hits = 0
    false_vector_only = 0
    for query, relevant_keys, category in QUERIES:
        runs = []
        for _ in range(repeats):
            query_started = time.perf_counter()
            runs = store.search(project["id"], query, 5, statuses=["active"])
            latencies.append((time.perf_counter() - query_started) * 1000)
        returned = [row["id"] for row in runs]
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
        })
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    provider_name = store._provider_name()
    store.close()
    positive_queries = sum(bool(keys) for _, keys, _ in QUERIES)
    negative_queries = len(QUERIES) - positive_queries
    categories = {}
    for category in sorted({category for _, _, category in QUERIES}):
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
        "negative_query_result_rate": false_vector_only / negative_queries,
        "categories": categories,
        "index_ms": round(index_ms, 3),
        "query_latency_ms": {
            "median": round(statistics.median(latencies), 3),
            "p95": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * .95))], 3),
        },
        "database_bytes": db_path.stat().st_size,
        "outcomes": outcomes,
    }


def run(model: str | None = None, repeats: int = 5) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        providers: list[tuple[str, Any]] = [("fts", None), ("local_hash", LocalHashEmbedding())]
        if model:
            providers.append(("neural", SentenceTransformerEmbedding(model)))
        results = {name: evaluate(name, provider, root, repeats) for name, provider in providers}
    return {
        "schema_version": 1,
        "fixture": {"memories": len(MEMORIES), "queries": len(QUERIES), "repeats": repeats},
        "neural_model": model,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Local path or explicit sentence-transformers model identifier")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run(args.model, args.repeats)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
