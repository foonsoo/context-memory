#!/usr/bin/env python3
"""Evaluate decision-brief/v1 against frozen, public synthetic scenarios."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from context_memory.store import MemoryStore
from context_memory.embeddings import LocalHashEmbedding


DEFAULT_FIXTURE = Path(__file__).with_name("fixtures") / "decision-scenarios-v1.json"
SECTIONS = ("current_decisions", "rationale", "constraints", "alternatives", "outcomes",
            "history", "disputes", "open_questions")


def percentile(samples: list[float], proportion: float) -> float:
    return sorted(samples)[min(len(samples) - 1, int(len(samples) * proportion))]


def load_fixture(path: str | Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if fixture.get("schema_version") != 1 or fixture.get("contract_version") != "decision-brief/v1":
        raise ValueError("fixture must use schema_version 1 and decision-brief/v1")
    scenarios = fixture.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("fixture scenarios must be a non-empty list")
    ids: set[str] = set()
    for scenario in scenarios:
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in ids:
            raise ValueError("scenario ids must be unique non-empty strings")
        ids.add(scenario_id)
        memories = scenario.get("memories")
        if not isinstance(memories, list) or not memories:
            raise ValueError(f"scenario {scenario_id} memories must be non-empty")
        keys = [item.get("key") for item in memories]
        if any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
            raise ValueError(f"scenario {scenario_id} memory keys must be unique non-empty strings")
        known = set(keys)
        relevant = scenario.get("relevant")
        if relevant is not None and (not isinstance(relevant, list) or not all(isinstance(value, str) for value in relevant)):
            raise ValueError(f"scenario {scenario_id} relevant must be a string list")
        if relevant is not None and not set(relevant) <= known:
            raise ValueError(f"scenario {scenario_id} relevant references unknown keys")
        for section, expected in scenario.get("expected", {}).items():
            if section not in {*SECTIONS, "excluded_current", "uncertainty_reasons"}:
                raise ValueError(f"scenario {scenario_id} has unknown expected section: {section}")
            if not isinstance(expected, list) or not all(isinstance(value, str) for value in expected):
                raise ValueError(f"scenario {scenario_id} expected {section} must be a string list")
            if section != "uncertainty_reasons" and not set(expected) <= known:
                raise ValueError(f"scenario {scenario_id} expected {section} references unknown keys")
    return fixture


def _store(path: Path, mode: str) -> MemoryStore:
    if mode == "local-hash":
        return MemoryStore(path, embedding_provider=LocalHashEmbedding())
    previous = os.environ.get("CONTEXT_MEMORY_EMBEDDINGS")
    os.environ["CONTEXT_MEMORY_EMBEDDINGS"] = "off"
    try:
        return MemoryStore(path)
    finally:
        if previous is None:
            os.environ.pop("CONTEXT_MEMORY_EMBEDDINGS", None)
        else:
            os.environ["CONTEXT_MEMORY_EMBEDDINGS"] = previous


def _run_mode(fixture: dict[str, Any], repeats: int, directory: str, mode: str) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    latencies: list[float] = []
    all_claims = valid_claims = expected_sources = recovered_sources = 0
    expected_history = recovered_history = 0
    correct_current = stale_leaks = 0
    context_chars: list[int] = []
    reciprocal_ranks: list[float] = []
    positive_queries = positive_hits = negative_queries = negative_results = duplicate_results = returned_results = 0
    for scenario in fixture["scenarios"]:
            store = _store(Path(directory) / f"{mode}-{scenario['id']}.db", mode)
            project = store.create_project(f"decision-eval-{scenario['id']}")
            ids: dict[str, str] = {}
            for item in scenario["memories"]:
                event = store.record_event(project["id"], item["type"], item["content"],
                                           source_uri=item.get("source_uri"))
                memory = store.upsert_memory(
                    project["id"], item["title"], item["content"], item["type"], item["status"],
                    source_event_ids=[event["id"]], tags=item.get("tags"),
                    observed_at=item.get("observed_at"),
                )
                ids[item["key"]] = memory["id"]
            for index in range(scenario.get("distractor_count", 0)):
                event = store.record_event(project["id"], "fact", f"Unrelated synthetic archive record {index} about gardening")
                store.upsert_memory(project["id"], f"Archive distractor {index}", event["content"], "fact", "active",
                                    source_event_ids=[event["id"]])
            for other in scenario.get("other_projects", []):
                other_project = store.create_project(other["name"])
                for item in other["memories"]:
                    event = store.record_event(other_project["id"], item["type"], item["content"])
                    store.upsert_memory(other_project["id"], item["title"], item["content"],
                                        item["type"], item["status"], source_event_ids=[event["id"]])
            brief = None
            scenario_latencies = []
            for _ in range(repeats):
                started = time.perf_counter()
                brief = store.decision_context(project["id"], scenario["question"], 10000,
                                               discover_projects=bool(scenario.get("other_projects")))
                elapsed = (time.perf_counter() - started) * 1000
                latencies.append(elapsed)
                scenario_latencies.append(elapsed)
            assert brief is not None
            returned = {section: [entry["citations"]["memory_id"] for entry in brief[section]] for section in SECTIONS}
            expected = scenario["expected"]
            relevant_keys = scenario.get("relevant", list(dict.fromkeys(
                key for section in SECTIONS for key in expected.get(section, []))))
            ranked = [item["memory_id"] for item in brief["retrieval"]["items"][:5]]
            relevant_ids = {ids[key] for key in relevant_keys}
            rank = next((index for index, memory_id in enumerate(ranked, 1) if memory_id in relevant_ids), None)
            if relevant_ids:
                reciprocal_ranks.append(0.0 if rank is None else 1.0 / rank)
                positive_queries += 1
                positive_hits += int(rank is not None)
            else:
                negative_queries += 1
                negative_results += int(bool(ranked))
            normalized = [" ".join(item["content"].casefold().split()) for item in brief["retrieval"]["items"]]
            duplicate_results += len(normalized) - len(set(normalized))
            returned_results += len(normalized)
            current_ok = set(returned["current_decisions"]) == {ids[key] for key in expected.get("current_decisions", [])}
            correct_current += int(current_ok)
            excluded = {ids[key] for key in expected.get("excluded_current", [])}
            leaked = excluded & set(returned["current_decisions"])
            stale_leaks += len(leaked)
            section_checks = {
                section: set(returned[section]) == {ids[key] for key in keys}
                for section, keys in expected.items() if section not in {"excluded_current", "uncertainty_reasons"}
            }
            if "uncertainty_reasons" in expected:
                actual_reasons = {item["reason"] for item in brief["uncertainty"]}
                section_checks["uncertainty_reasons"] = set(expected["uncertainty_reasons"]) <= actual_reasons
            expected_history += len(expected.get("history", []))
            recovered_history += len({ids[key] for key in expected.get("history", [])} & set(returned["history"]))
            for section in SECTIONS:
                for claim in brief[section]:
                    all_claims += 1
                    citation = claim.get("citations") or {}
                    memory_id = citation.get("memory_id")
                    source_ids = citation.get("source_event_ids") or []
                    if memory_id in ids.values() and source_ids:
                        valid_claims += 1
                    for event_id in source_ids:
                        expected_sources += 1
                        try:
                            recovered_sources += int(store.get_source(event_id)["id"] == event_id)
                        except KeyError:
                            pass
            rendered_chars = len(json.dumps(brief, ensure_ascii=False, separators=(",", ":")))
            context_chars.append(rendered_chars)
            outcomes.append({
                "id": scenario["id"], "current_decision_correct": current_ok,
                "rank_at_5": rank,
                "retrieval_gate": brief["retrieval"]["retrieval_gate"],
                "stale_current_leaks": len(leaked), "section_checks": section_checks,
                "discovery_ambiguous": brief["retrieval"]["project_discovery"]["ambiguous"],
                "context_chars": rendered_chars,
                "latency_ms": {"p50": round(statistics.median(scenario_latencies), 3),
                               "p95": round(percentile(scenario_latencies, .95), 3)},
            })
            store.close()
    return {
        "provider": mode,
        "metrics": {
            "recall_at_5": positive_hits / max(1, positive_queries),
            "mrr_at_5": statistics.mean(reciprocal_ranks) if reciprocal_ranks else 1.0,
            "negative_query_false_result_rate": negative_results / negative_queries if negative_queries else 0.0,
            "duplicate_rate": duplicate_results / returned_results if returned_results else 0.0,
            "current_decision_accuracy": correct_current / len(outcomes),
            "stale_decision_leakage": stale_leaks / max(1, sum(len(s["expected"].get("excluded_current", [])) for s in fixture["scenarios"])),
            "unsupported_claim_rate": 1 - valid_claims / all_claims if all_claims else 0.0,
            "source_recovery_rate": recovered_sources / expected_sources if expected_sources else 1.0,
            "useful_history_recall": recovered_history / expected_history if expected_history else 1.0,
            "context_chars": {"median": statistics.median(context_chars), "max": max(context_chars)},
            "latency_ms": {"p50": round(statistics.median(latencies), 3), "p95": round(percentile(latencies, .95), 3)},
        },
        "scenarios": outcomes,
    }


def run(repeats: int = 5, fixture_path: str | Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    fixture = load_fixture(fixture_path)
    with tempfile.TemporaryDirectory() as directory:
        modes = {mode: _run_mode(fixture, repeats, directory, mode) for mode in ("fts-only", "local-hash")}
    return {
        "schema_version": 3,
        "contract_version": fixture["contract_version"],
        "fixture": {"source": Path(fixture_path).name, "scenarios": len(fixture["scenarios"]), "repeats": repeats},
        "modes": modes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--output")
    args = parser.parse_args()
    rendered = json.dumps(run(args.repeats, args.fixture), ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
