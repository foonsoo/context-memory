#!/usr/bin/env python3
"""Measure observed Codex input-token usage around Context Memory startup calls.

Only aggregate token counters and tool names are emitted. Session text, tool
arguments, and tool results are never copied into the report.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path
from typing import Any


STARTUP_TOOLS = {
    "context_bootstrap": "bootstrap",
    "project_resolve": "legacy",
    "session_start": "legacy",
    "get_context": "legacy",
}


def _tool_names(payload: dict[str, Any]) -> list[str]:
    if payload.get("type") == "custom_tool_call" and payload.get("name") == "exec":
        source = payload.get("input", "")
        if not isinstance(source, str):
            return []
        found = re.findall(r"mcp__context_memory__(context_bootstrap|project_resolve|session_start|get_context)\b", source)
        return list(dict.fromkeys(found))
    if payload.get("type") != "function_call":
        return []
    name = payload.get("name")
    if not isinstance(name, str):
        return []
    return [name.rsplit("__", 1)[-1]]


def analyze(path: Path) -> dict[str, Any]:
    pending: list[str] = []
    observations: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            names = [name for name in _tool_names(payload) if name in STARTUP_TOOLS]
            if names:
                pending.extend(names)
                continue
            if row.get("type") != "event_msg" or payload.get("type") != "token_count" or not pending:
                continue
            usage = payload.get("info", {}).get("last_token_usage", {})
            if not isinstance(usage, dict) or not isinstance(usage.get("input_tokens"), int):
                continue
            observations.append({
                "tools_since_previous_model_turn": pending,
                "workflow": "bootstrap" if pending == ["context_bootstrap"] else "legacy",
                "input_tokens": usage["input_tokens"],
                "cached_input_tokens": int(usage.get("cached_input_tokens", 0)),
                "uncached_input_tokens": usage["input_tokens"] - int(usage.get("cached_input_tokens", 0)),
            })
            pending = []
    return {"session": path.name, "observations": observations}


def summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {"bootstrap": [], "legacy": []}
    for report in reports:
        for item in report["observations"]:
            groups[item["workflow"]].append(item)
    summary: dict[str, Any] = {}
    for workflow, items in groups.items():
        if not items:
            continue
        summary[workflow] = {
            "observations": len(items),
            "input_tokens_min": min(x["input_tokens"] for x in items),
            "input_tokens_max": max(x["input_tokens"] for x in items),
            "uncached_input_tokens_min": min(x["uncached_input_tokens"] for x in items),
            "uncached_input_tokens_max": max(x["uncached_input_tokens"] for x in items),
        }
    session_groups: dict[str, list[dict[str, Any]]] = {"bootstrap": [], "legacy": []}
    for report in reports:
        by_workflow: dict[str, list[dict[str, Any]]] = {"bootstrap": [], "legacy": []}
        for item in report["observations"]:
            by_workflow[item["workflow"]].append(item)
        for workflow, items in by_workflow.items():
            if not items:
                continue
            session_groups[workflow].append({
                "session": report["session"],
                "model_turns": len(items),
                "input_tokens": sum(x["input_tokens"] for x in items),
                "cached_input_tokens": sum(x["cached_input_tokens"] for x in items),
                "uncached_input_tokens": sum(x["uncached_input_tokens"] for x in items),
            })
    session_summary: dict[str, Any] = {}
    for workflow, items in session_groups.items():
        if not items:
            continue
        session_summary[workflow] = {
            "sessions": len(items),
            "input_tokens_min": min(x["input_tokens"] for x in items),
            "input_tokens_max": max(x["input_tokens"] for x in items),
            "uncached_input_tokens_min": min(x["uncached_input_tokens"] for x in items),
            "uncached_input_tokens_max": max(x["uncached_input_tokens"] for x in items),
            "items": items,
        }
    controlled: dict[str, list[dict[str, Any]]] = {"bootstrap": [], "legacy": []}
    expected = {
        "bootstrap": ["context_bootstrap"],
        "legacy": ["project_resolve", "session_start", "get_context"],
    }
    for report in reports:
        flattened = [name for item in report["observations"]
                     for name in item["tools_since_previous_model_turn"]]
        for workflow, sequence in expected.items():
            if flattened != sequence:
                continue
            items = report["observations"]
            controlled[workflow].append({
                "session": report["session"],
                "model_turns": len(items),
                "input_tokens": sum(x["input_tokens"] for x in items),
                "cached_input_tokens": sum(x["cached_input_tokens"] for x in items),
                "uncached_input_tokens": sum(x["uncached_input_tokens"] for x in items),
            })
    controlled_summary: dict[str, Any] = {}
    for workflow, items in controlled.items():
        if not items:
            continue
        controlled_summary[workflow] = {
            "sessions": len(items),
            "input_tokens_median": statistics.median(x["input_tokens"] for x in items),
            "uncached_input_tokens_median": statistics.median(x["uncached_input_tokens"] for x in items),
            "items": items,
        }
    if all(workflow in controlled_summary for workflow in expected):
        bootstrap = controlled_summary["bootstrap"]["uncached_input_tokens_median"]
        legacy = controlled_summary["legacy"]["uncached_input_tokens_median"]
        controlled_summary["comparison"] = {
            "uncached_input_tokens_median_delta": bootstrap - legacy,
            "uncached_input_tokens_median_change_percent": (
                round((bootstrap / legacy - 1) * 100, 1) if legacy else None
            ),
        }
    return {"schema_version": 3, "sessions": reports, "summary": summary,
            "session_summary": session_summary, "controlled_summary": controlled_summary,
            "caveat": "Observed model-turn totals include the full Codex prompt; they are not isolated marginal tool costs."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", nargs="+", type=Path, help="Explicit Codex rollout JSONL paths")
    parser.add_argument("--output", type=Path, help="Write aggregate JSON to this path")
    args = parser.parse_args()
    result = summarize([analyze(path) for path in args.sessions])
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
