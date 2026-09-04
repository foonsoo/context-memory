#!/usr/bin/env python3
"""Replay provenance-bearing real continuation prompts without claiming holdout."""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from context_memory.recall import estimate_tokens
from context_memory.store import MemoryStore


DEFAULT_FIXTURE = (
    Path(__file__).with_name("fixtures") / "continuation-live-replay-v1.json"
)


def load_fixture(path: str | Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if fixture.get("schema_version") != 1:
        raise ValueError("fixture must use schema_version 1")
    if fixture.get("contract_version") != "continuation-live-replay/v1":
        raise ValueError("fixture must use continuation-live-replay/v1")
    if fixture.get("evaluation_class") != "retrospective-live-replay":
        raise ValueError("fixture must identify retrospective live replay")
    if not fixture.get("limitations"):
        raise ValueError("fixture must state evaluation limitations")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("fixture cases must be a non-empty list")
    for case in cases:
        sources = case.get("prompt_source_event_ids")
        if not isinstance(sources, list) or not sources or not all(
            isinstance(source, str) and source for source in sources
        ):
            raise ValueError("each live prompt needs source event IDs")
        for field in ("expected", "forbidden"):
            values = case.get(field)
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                raise ValueError(f"case {field} must be a string list")
        if not isinstance(case.get("repository_path"), str) or not case[
            "repository_path"
        ]:
            raise ValueError("each live case needs a repository path")
    return fixture


def _percentile(samples: list[float], proportion: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * proportion))]


def run(
    repeats: int = 10, fixture_path: str | Path = DEFAULT_FIXTURE
) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    fixture = load_fixture(fixture_path)
    outcomes: list[dict[str, Any]] = []
    latencies: list[float] = []
    recovered_checks = total_checks = source_recoveries = leaks = 0
    returned_tokens: list[int] = []

    for case in fixture["cases"]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MemoryStore(root / "memory.db")
            workspace = root / case["project"]
            workspace.mkdir()
            resolved = store.resolve_project(str(workspace))
            event = store.record_event(
                resolved["project"]["id"],
                case["handoff_kind"],
                case["handoff"],
                scope_id=resolved["scope_id"],
                metadata={"repository_path": case["repository_path"]},
            )
            result = None
            case_latencies: list[float] = []
            for _ in range(repeats):
                started = time.perf_counter()
                result = store.context_recall(str(workspace), case["prompt"])
                elapsed = (time.perf_counter() - started) * 1000
                case_latencies.append(elapsed)
                latencies.append(elapsed)
            assert result is not None
            rendered = json.dumps(result, ensure_ascii=False).casefold()
            checks = [value.casefold() in rendered for value in case["expected"]]
            case_leaks = [
                value
                for value in case["forbidden"]
                if value.casefold() in rendered
            ]
            source_ids = {
                source
                for item in result["items"]
                for source in item.get("source_event_ids", [])
            }
            source_recovered = event["id"] in source_ids
            tokens = estimate_tokens(json.dumps(result, ensure_ascii=False))
            recovered_checks += sum(checks)
            total_checks += len(checks)
            source_recoveries += int(source_recovered)
            leaks += len(case_leaks)
            returned_tokens.append(tokens)
            outcomes.append(
                {
                    "case": case["id"],
                    "prompt": case["prompt"],
                    "prompt_source_event_ids": case["prompt_source_event_ids"],
                    "expected_checks": checks,
                    "forbidden_leaks": case_leaks,
                    "source_recovered": source_recovered,
                    "selection_reason": result["retrieval"]["selection_reason"],
                    "returned_tokens": tokens,
                    "latency_ms": {
                        "p50": round(statistics.median(case_latencies), 3),
                        "p95": round(_percentile(case_latencies, 0.95), 3),
                    },
                }
            )
            store.close()

    return {
        "schema_version": 1,
        "contract_version": fixture["contract_version"],
        "evaluation_class": fixture["evaluation_class"],
        "limitations": fixture["limitations"],
        "fixture": {
            "source": Path(fixture_path).name,
            "cases": len(fixture["cases"]),
            "repeats": repeats,
        },
        "metrics": {
            "expected_recovery": recovered_checks / total_checks,
            "source_recovery": source_recoveries / len(fixture["cases"]),
            "forbidden_leakage": leaks,
            "returned_tokens": {
                "median": statistics.median(returned_tokens),
                "max": max(returned_tokens),
            },
            "latency_ms": {
                "p50": round(statistics.median(latencies), 3),
                "p95": round(_percentile(latencies, 0.95), 3),
            },
        },
        "cases": outcomes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--output")
    args = parser.parse_args()
    rendered = json.dumps(
        run(args.repeats, args.fixture), indent=2, ensure_ascii=False, sort_keys=True
    )
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
