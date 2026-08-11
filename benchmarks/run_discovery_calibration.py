#!/usr/bin/env python3
"""Calibrate cross-project discovery on deterministic multi-project fixtures."""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from context_memory.store import MemoryStore


DOMAINS = [
    ("billing-ledger", "invoice reconciliation tax currency"),
    ("identity-gateway", "oauth session token rotation"),
    ("catalog-search", "product facets synonyms indexing"),
    ("fulfillment-router", "warehouse carrier shipment routing"),
    ("analytics-pipeline", "warehouse metrics dbt attribution"),
    ("mobile-client", "ios android offline synchronization"),
    ("support-console", "ticket escalation macros sla"),
    ("fraud-engine", "risk rules chargeback velocity"),
    ("notification-hub", "email sms push delivery"),
    ("developer-portal", "api keys documentation sandbox"),
    ("asset-manager", "images renditions metadata upload"),
    ("context-memory", "memory provenance retrieval checkpoint"),
]


def percentile(samples: list[float], fraction: float) -> float:
    return sorted(samples)[min(len(samples) - 1, int(len(samples) * fraction))]


def populate(store: MemoryStore, distractors: int) -> dict[str, dict[str, Any]]:
    projects = {}
    for slug, vocabulary in DOMAINS:
        project = store.create_project(slug, slug.replace("-", " "))
        projects[slug] = project
        for index in range(distractors):
            store.upsert_memory(
                project["id"],
                f"Sprint note {index}",
                f"{vocabulary} release planning milestone {index % 7}",
                "fact",
                "active",
            )
    return projects


def run(items_per_project: int = 40, repeats: int = 100) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / "memory.db")
        try:
            projects = populate(store, items_per_project)
            hinted = store.create_project("empty-checkout", "scratch workspace")
            target = projects["context-memory"]
            store.start_session(target["id"], "calibration", external_id="recent-target")
            store.upsert_memory(target["id"], "Next implementation checkpoint",
                                "Tune cross project discovery confidence calibration performance",
                                "task", "active")
            store.upsert_memory(projects["catalog-search"]["id"], "Search calibration",
                                "Tune product discovery relevance calibration performance",
                                "task", "active")

            scenarios = [
                ("strong", "memory provenance retrieval checkpoint", target["id"], "single_confident_candidate"),
                ("dominant", "cross project discovery confidence calibration", target["id"], "dominant_candidate"),
                ("low-confidence", "unrelated words checkpoint", None, "low_confidence"),
            ]
            outcomes = []
            for name, query, expected_project, expected_reason in scenarios:
                result = store.get_context(hinted["id"], query, 4000)
                discovery = result["project_discovery"]
                outcomes.append({"name": name, "query": query,
                                 "expected_project_id": expected_project,
                                 "selected_project_id": discovery.get("selected_project_id"),
                                 "expected_reason": expected_reason,
                                 "selection_reason": discovery.get("selection_reason"),
                                 "passed": discovery.get("selected_project_id") == expected_project
                                           and discovery.get("selection_reason") == expected_reason,
                                 "candidates": [{key: candidate[key] for key in
                                                 ("slug", "confidence", "relevance", "evidence_quality")}
                                                for candidate in discovery.get("candidates", [])[:3]]})

            first = store.create_project("ambiguous-a")
            second = store.create_project("ambiguous-b")
            for project in (first, second):
                store.upsert_memory(project["id"], "Shared migration",
                                    "rotate signing keys during regional migration", "task", "active")
            ambiguous = store.get_context(hinted["id"], "signing keys regional migration", 4000)
            ambiguous_passed = (ambiguous["project_discovery"]["selection_reason"] == "ambiguous_candidates"
                                and ambiguous["items"] == [])

            samples = []
            for _ in range(repeats):
                started = time.perf_counter()
                store.get_context(hinted["id"], "cross project discovery confidence calibration", 4000)
                samples.append((time.perf_counter() - started) * 1000)
            return {"schema_version": 1,
                    "projects": len(DOMAINS) + 3,
                    "memories": len(DOMAINS) * items_per_project + 4,
                    "items_per_project": items_per_project,
                    "repeats": repeats,
                    "accuracy": sum(item["passed"] for item in outcomes) / len(outcomes),
                    "ambiguity_safe": ambiguous_passed,
                    "latency_ms": {"p50": round(statistics.median(samples), 3),
                                   "p95": round(percentile(samples, .95), 3)},
                    "scenarios": outcomes}
        finally:
            store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items-per-project", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run(args.items_per_project, args.repeats)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
