#!/usr/bin/env python3
"""Compare continuation context baselines on a frozen prompt fixture."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from context_memory.recall import estimate_tokens
from context_memory.store import MemoryStore


DEFAULT_FIXTURE = (
    Path(__file__).with_name("fixtures") / "continuation-scenarios-v1.json"
)
BASELINES = ("repository-only", "context-memory", "llm-wiki-snapshot", "context-recall-vnext")
CATEGORIES = ("artifacts", "decisions", "next_steps")


def percentile(samples: list[float], proportion: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * proportion))]


def load_fixture(path: str | Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if fixture.get("schema_version") != 1:
        raise ValueError("fixture must use schema_version 1")
    if fixture.get("contract_version") != "continuation-eval/v1":
        raise ValueError("fixture must use continuation-eval/v1")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("fixture cases must be a non-empty list")
    ids: set[str] = set()
    prompt_count = 0
    for case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError("case ids must be unique non-empty strings")
        ids.add(case_id)
        prompts = case.get("prompts")
        if not isinstance(prompts, list) or not prompts or not all(
            isinstance(prompt, str) and prompt.strip() for prompt in prompts
        ):
            raise ValueError(f"case {case_id} prompts must be non-empty strings")
        prompt_count += len(prompts)
        expected = case.get("expected", {})
        for category in (*CATEGORIES, "forbidden"):
            values = expected.get(category)
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"case {case_id} expected {category} must be a string list")
    if not 20 <= prompt_count <= 30:
        raise ValueError("fixture must contain 20..30 continuation prompts")
    return fixture


def _terms(value: str) -> set[str]:
    return {term for term in value.casefold().replace("/", " ").replace("-", " ").split() if len(term) > 1}


def _render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _source_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "source_event_ids" and isinstance(child, list):
                found.extend(item for item in child if isinstance(item, str))
            else:
                found.extend(_source_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_source_ids(child))
    return list(dict.fromkeys(found))


def _repository_only(cwd: Path, query: str, _store: MemoryStore, _project_id: str, _wiki: list[dict[str, str]]) -> dict[str, Any]:
    if not cwd.exists():
        return {"items": [], "retrieval": {"status": "not_found"}}
    query_terms = _terms(query)
    ranked: list[tuple[int, str, str]] = []
    for path in cwd.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        relative = str(path.relative_to(cwd))
        score = len(query_terms & _terms(relative + " " + content))
        if score:
            ranked.append((score, relative, content[:600]))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return {"repository_path": str(cwd), "items": [{"path": path, "text": text} for _, path, text in ranked[:6]], "retrieval": {"status": "found" if ranked else "not_found"}}


def _legacy_context(_cwd: Path, query: str, store: MemoryStore, project_id: str, _wiki: list[dict[str, str]]) -> dict[str, Any]:
    return store.get_context(project_id, query, 6000, discover_projects=True, response_format="compact")


def _wiki_snapshot(_cwd: Path, query: str, _store: MemoryStore, _project_id: str, wiki: list[dict[str, str]]) -> dict[str, Any]:
    terms = _terms(query)
    ranked = sorted(
        ((len(terms & _terms(page["text"])), page) for page in wiki),
        key=lambda row: (-row[0], row[1]["project"]),
    )
    if not ranked or ranked[0][0] == 0:
        return {"items": [], "retrieval": {"status": "not_found"}}
    page = ranked[0][1]
    return {"project": page["project"], "repository_path": page["path"], "items": [page["text"]], "retrieval": {"status": "found"}}


def _vnext(cwd: Path, query: str, store: MemoryStore, _project_id: str, _wiki: list[dict[str, str]]) -> dict[str, Any]:
    return store.context_recall(str(cwd), query)


RUNNERS: dict[str, Callable[..., dict[str, Any]]] = {
    "repository-only": _repository_only,
    "context-memory": _legacy_context,
    "llm-wiki-snapshot": _wiki_snapshot,
    "context-recall-vnext": _vnext,
}


def _seed(fixture: dict[str, Any], root: Path) -> tuple[MemoryStore, list[dict[str, str]], dict[str, dict[str, Any]]]:
    previous = os.environ.get("CONTEXT_MEMORY_EMBEDDINGS")
    os.environ["CONTEXT_MEMORY_EMBEDDINGS"] = "off"
    try:
        store = MemoryStore(root / "evaluation.db")
    finally:
        if previous is None:
            os.environ.pop("CONTEXT_MEMORY_EMBEDDINGS", None)
        else:
            os.environ["CONTEXT_MEMORY_EMBEDDINGS"] = previous
    wiki: list[dict[str, str]] = []
    seeded: dict[str, dict[str, Any]] = {}
    for case in fixture["cases"]:
        canonical = root / case["canonical_path"]
        for relative, content in case["repository_files"].items():
            path = canonical / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content + "\n", encoding="utf-8")
        project = store.create_project(case["project"])
        store.set_project_alias(project["id"], "path", str(canonical))
        event_ids: list[str] = []
        for item in case["memories"]:
            event = store.record_event(project["id"], item["type"], item["content"])
            store.upsert_memory(
                project["id"], item["title"], item["content"], item["type"], item["status"],
                source_event_ids=[event["id"]],
            )
            event_ids.append(event["id"])
        for page in case["wiki_pages"]:
            wiki.append({"project": case["project"], "path": str(canonical), "text": page})
        seeded[case["id"]] = {"project": project, "canonical": canonical, "event_ids": event_ids}
    return store, wiki, seeded


def _is_absent(result: dict[str, Any]) -> bool:
    retrieval = result.get("retrieval") or {}
    status = retrieval.get("status") or retrieval.get("retrieval_gate", {}).get("status")
    return status in {"not_found", "no_confident_match"} or not result.get("items")


def run(repeats: int = 5, fixture_path: str | Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    fixture = load_fixture(fixture_path)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store, wiki, seeded = _seed(fixture, root)
        placeholder = root / "empty-placeholder"
        placeholder.mkdir()
        modes: dict[str, Any] = {}
        for mode in BASELINES:
            outcomes: list[dict[str, Any]] = []
            latencies: list[float] = []
            category_hits = {category: 0 for category in CATEGORIES}
            category_total = {category: 0 for category in CATEGORIES}
            full_recalls = wrong_projects = false_absences = stale_leaks = 0
            sources_expected = sources_recovered = 0
            returned_tokens: list[int] = []
            total_input_tokens: list[int] = []
            for case in fixture["cases"]:
                target = seeded[case["id"]]
                for prompt_index, prompt in enumerate(case["prompts"]):
                    # The first phrasing always represents a cold continuation from an
                    # unrelated/empty cwd. Blog exercises all three phrasings this way.
                    cwd = placeholder if prompt_index == 0 or case["id"] == "blog-episode-four" else target["canonical"]
                    result = None
                    prompt_latencies: list[float] = []
                    origin = store.resolve_project(str(cwd))["project"]["id"]
                    for _ in range(repeats):
                        started = time.perf_counter()
                        result = RUNNERS[mode](cwd, prompt, store, origin, wiki)
                        elapsed = (time.perf_counter() - started) * 1000
                        latencies.append(elapsed)
                        prompt_latencies.append(elapsed)
                    assert result is not None
                    rendered = _render(result)
                    normalized = rendered.casefold()
                    checks: dict[str, list[bool]] = {}
                    for category in CATEGORIES:
                        checks[category] = [value.casefold() in normalized for value in case["expected"][category]]
                        category_hits[category] += sum(checks[category])
                        category_total[category] += len(checks[category])
                    complete = all(all(values) for values in checks.values())
                    full_recalls += int(complete)
                    absent = _is_absent(result)
                    false_absences += int(absent)
                    leaks = [value for value in case["expected"]["forbidden"] if value.casefold() in normalized]
                    stale_leaks += len(leaks)
                    selected = result.get("project")
                    if isinstance(selected, dict):
                        selected = selected.get("id") or selected.get("name")
                    expected_ids = {target["project"]["id"], target["project"]["name"]}
                    wrong_projects += int(selected is not None and selected not in expected_ids)
                    source_ids = _source_ids(result)
                    if not absent:
                        sources_expected += 1
                        valid = False
                        for event_id in source_ids:
                            try:
                                valid = valid or store.get_source(event_id)["id"] == event_id
                            except KeyError:
                                pass
                        sources_recovered += int(valid)
                    tokens = estimate_tokens(rendered)
                    returned_tokens.append(tokens)
                    total_input_tokens.append(tokens + estimate_tokens(prompt))
                    outcomes.append({
                        "case": case["id"], "prompt": prompt, "cwd": "placeholder" if cwd == placeholder else "canonical",
                        "complete": complete, "false_absence": absent, "wrong_project": selected is not None and selected not in expected_ids,
                        "stale_leaks": leaks, "category_checks": checks, "returned_tokens": tokens,
                        "selected_project": selected,
                        "retrieval_status": (result.get("retrieval") or {}).get("status"),
                        "selection_reason": (result.get("retrieval") or {}).get("selection_reason"),
                        "latency_ms": {"p50": round(statistics.median(prompt_latencies), 3), "p95": round(percentile(prompt_latencies, .95), 3)},
                    })
            count = len(outcomes)
            modes[mode] = {
                "metrics": {
                    "continuation_recall": full_recalls / count,
                    "artifact_recovery": category_hits["artifacts"] / category_total["artifacts"],
                    "decision_recovery": category_hits["decisions"] / category_total["decisions"],
                    "next_step_recovery": category_hits["next_steps"] / category_total["next_steps"],
                    "wrong_project_rate": wrong_projects / count,
                    "false_absence_rate": false_absences / count,
                    "stale_error_leakage": stale_leaks / sum(len(case["expected"]["forbidden"]) * len(case["prompts"]) for case in fixture["cases"]),
                    "source_recovery": sources_recovered / sources_expected if sources_expected else 1.0,
                    "returned_tokens": {"median": statistics.median(returned_tokens), "max": max(returned_tokens)},
                    "total_llm_input_tokens": {"median": statistics.median(total_input_tokens), "max": max(total_input_tokens)},
                    "latency_ms": {"p50": round(statistics.median(latencies), 3), "p95": round(percentile(latencies, .95), 3)},
                },
                "prompts": outcomes,
            }
        store.close()
    return {
        "schema_version": 1,
        "contract_version": fixture["contract_version"],
        "fixture": {"source": Path(fixture_path).name, "cases": len(fixture["cases"]), "prompts": sum(len(case["prompts"]) for case in fixture["cases"]), "repeats": repeats},
        "baseline_notes": {"llm-wiki-snapshot": "Deterministic exported-page proxy; not a live LLM Wiki service."},
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
