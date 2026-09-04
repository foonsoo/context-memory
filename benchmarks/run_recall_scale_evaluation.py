#!/usr/bin/env python3
"""Measure bounded repository artifact recall on a large synthetic tree."""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from unittest import mock

from context_memory.recall import _repository_artifacts


TARGET = "zz-orchid-release/continuation-target.md"


def percentile(samples: list[float], proportion: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * proportion))]


def _seed(root: Path, distractors: int) -> None:
    target = root / TARGET
    target.parent.mkdir(parents=True)
    target.write_text(
        "Orchid recovery uses the cobalt checkpoint and resumes validation.",
        encoding="utf-8",
    )
    bulk = root / "aa-bulk"
    bulk.mkdir()
    for index in range(distractors):
        group = bulk / f"group-{index // 100:03d}"
        group.mkdir(exist_ok=True)
        (group / f"noise-{index:05d}.md").write_text(
            "Generic archived implementation notes.", encoding="utf-8"
        )


def run(repeats: int = 20, distractors: int = 2500) -> dict[str, object]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    if distractors < 1000:
        raise ValueError("distractors must be at least 1000")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _seed(root, distractors)
        original = Path.read_text
        latencies: list[float] = []
        read_counts: list[int] = []
        results: list[list[str]] = []

        for _ in range(repeats):
            reads = 0

            def counted_read(path: Path, *args: object, **kwargs: object) -> str:
                nonlocal reads
                reads += 1
                return original(path, *args, **kwargs)

            started = time.perf_counter()
            with mock.patch.object(Path, "read_text", counted_read):
                artifacts = _repository_artifacts(
                    str(root),
                    "resume Orchid cobalt validation",
                    "checkpoint recovery",
                )
            latencies.append((time.perf_counter() - started) * 1000)
            read_counts.append(reads)
            results.append(artifacts)

    recovered = sum(TARGET in artifacts for artifacts in results)
    return {
        "schema_version": 1,
        "contract_version": "context-recall-scale/v1",
        "fixture": {
            "kind": "synthetic-large-repository",
            "distractor_files": distractors,
            "total_files": distractors + 1,
            "target": TARGET,
            "target_is_after_bulk_lexically": TARGET > "aa-bulk",
            "repeats": repeats,
        },
        "limits": {
            "max_entries": 512,
            "max_files": 128,
            "max_bytes": 256 * 1024,
        },
        "metrics": {
            "target_recovery": recovered / repeats,
            "max_files_read": max(read_counts),
            "latency_ms": {
                "p50": round(statistics.median(latencies), 3),
                "p95": round(percentile(latencies, 0.95), 3),
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--distractors", type=int, default=2500)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run(args.repeats, args.distractors)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
