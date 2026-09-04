#!/usr/bin/env python3
"""Explain vNext project selection and context-pack failures per prompt."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

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
                origin = store.resolve_project(str(cwd))["project"]
                result = store.context_recall(str(cwd), prompt)
                selected = result.get("project")
                retrieval = result["retrieval"]
                report.append(
                    {
                        "case": case["id"],
                        "prompt": prompt,
                        "cwd": "placeholder" if cwd == placeholder else "canonical",
                        "origin_project": origin["name"],
                        "expected_project": target["project"]["name"],
                        "retrieval_status": retrieval["status"],
                        "selected_project_id": (
                            selected["id"] if selected else None
                        ),
                        "selection_reason": retrieval["selection_reason"],
                        "repository_path": result["repository_path"],
                        "items": result["items"],
                    }
                )
        store.close()
        return report


if __name__ == "__main__":
    print(json.dumps(analyze(), ensure_ascii=False, indent=2))
