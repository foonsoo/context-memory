#!/usr/bin/env python3
"""Explain vNext project selection and context-pack failures per prompt."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from context_memory.recall import _expand_recall_query

try:
    from .run_continuation_evaluation import _seed, load_fixture
except ImportError:  # Direct script execution.
    from run_continuation_evaluation import _seed, load_fixture


def analyze() -> list[dict[str, object]]:
    fixture = load_fixture()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store, _, seeded = _seed(fixture, root)
        placeholder = root / "empty-placeholder"
        placeholder.mkdir()
        report: list[dict[str, object]] = []
        for case in fixture["cases"]:
            target = seeded[case["id"]]
            for prompt_index, prompt in enumerate(case["prompts"]):
                cwd = (
                    placeholder
                    if prompt_index == 0 or case["id"] == "blog-episode-four"
                    else target["canonical"]
                )
                origin_id = store.resolve_project(str(cwd))["project"]["id"]
                memories = store.search(
                    origin_id,
                    _expand_recall_query(prompt),
                    24,
                    ["active", "disputed"],
                    None,
                    True,
                )
                gate = store._retrieval_gate(memories)
                candidates = store._aggregate_project_candidates(
                    memories, origin_id
                )
                selected, reason, confidence = store._select_project_candidate(
                    candidates
                )
                report.append(
                    {
                        "case": case["id"],
                        "prompt": prompt,
                        "cwd": "placeholder" if cwd == placeholder else "canonical",
                        "expected_project": target["project"]["name"],
                        "retrieval_gate": {
                            "status": gate["status"],
                            "reason": gate["reason"],
                        },
                        "selected_project_id": selected,
                        "selection_reason": reason,
                        "selection_confidence": round(confidence, 6),
                        "project_candidates": [
                            {
                                "name": candidate["name"],
                                "confidence": round(candidate["confidence"], 6),
                                "matching_memories": candidate[
                                    "matching_memory_count"
                                ],
                                "evidence_quality": round(
                                    candidate["evidence_quality"], 6
                                ),
                            }
                            for candidate in candidates[:4]
                        ],
                    }
                )
        store.close()
        return report


if __name__ == "__main__":
    print(json.dumps(analyze(), ensure_ascii=False, indent=2))
