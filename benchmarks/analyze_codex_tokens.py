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
    if payload.get("type") == "mcp_tool_call_end":
        invocation = payload.get("invocation", {})
        if invocation.get("server") != "context_memory":
            return []
        tool = invocation.get("tool")
        return [tool] if isinstance(tool, str) else []
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
                if payload.get("type") != "mcp_tool_call_end" or not all(name in pending for name in names):
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


def analyze_manifest(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load paired-run metadata without copying prompts or tool results."""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    runs = manifest.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("manifest.runs must be a non-empty array")
    reports: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    seen_sessions: set[str] = set()
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ValueError(f"manifest.runs[{index}] must be an object")
        workflow, pair, stratum = run.get("workflow"), run.get("pair"), run.get("stratum")
        if workflow not in {"bootstrap", "legacy"} or not isinstance(pair, str) or not pair:
            raise ValueError(f"manifest.runs[{index}] requires workflow bootstrap|legacy and pair")
        if not isinstance(stratum, str) or not stratum:
            raise ValueError(f"manifest.runs[{index}].stratum must be a non-empty string")
        key = (pair, workflow)
        if key in seen:
            raise ValueError(f"duplicate run for pair={pair!r}, workflow={workflow!r}")
        seen.add(key)
        session_path = Path(run.get("session", ""))
        if not session_path.is_absolute():
            session_path = path.parent / session_path
        report = analyze(session_path)
        if report["session"] in seen_sessions:
            raise ValueError(f"duplicate session filename in manifest: {report['session']!r}")
        seen_sessions.add(report["session"])
        report.update({"pair": pair, "stratum": stratum, "declared_workflow": workflow})
        reports.append(report)
    snapshot = manifest.get("snapshot_sha256")
    if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", snapshot):
        raise ValueError("manifest.snapshot_sha256 must be a 64-character hexadecimal digest")
    return reports, {"snapshot_sha256": snapshot.lower(), "manifest": path.name}


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
                **({"pair": report["pair"], "stratum": report["stratum"]}
                   if "pair" in report else {}),
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
            "cached_input_tokens_median": statistics.median(x["cached_input_tokens"] for x in items),
            "uncached_input_tokens_median": statistics.median(x["uncached_input_tokens"] for x in items),
            "items": items,
        }
    if all(workflow in controlled_summary for workflow in expected):
        comparison: dict[str, Any] = {}
        for metric in ("input_tokens", "cached_input_tokens", "uncached_input_tokens"):
            bootstrap = controlled_summary["bootstrap"][f"{metric}_median"]
            legacy = controlled_summary["legacy"][f"{metric}_median"]
            comparison[f"{metric}_median_delta"] = bootstrap - legacy
            comparison[f"{metric}_median_change_percent"] = (
                round((bootstrap / legacy - 1) * 100, 1) if legacy else None
            )
        controlled_summary["comparison"] = comparison
    paired: dict[str, Any] = {}
    paired_reports = [report for report in reports if "pair" in report]
    if paired_reports:
        controlled_by_session = {item["session"]: (workflow, item)
                                 for workflow, items in controlled.items() for item in items}
        pairs: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        for report in paired_reports:
            found = controlled_by_session.get(report["session"])
            if found is None or found[0] != report["declared_workflow"]:
                raise ValueError(f"completed startup sequence does not match manifest for {report['session']}")
            pairs.setdefault(report["pair"], {})[found[0]] = (report["stratum"], found[1])
        strata: dict[str, list[dict[str, Any]]] = {}
        for pair, workflows in pairs.items():
            if set(workflows) != set(expected):
                raise ValueError(f"pair {pair!r} must contain exactly one bootstrap and one legacy run")
            bootstrap_stratum, bootstrap = workflows["bootstrap"]
            legacy_stratum, legacy = workflows["legacy"]
            if bootstrap_stratum != legacy_stratum:
                raise ValueError(f"pair {pair!r} has mismatched strata")
            delta = {"pair": pair}
            for metric in ("input_tokens", "cached_input_tokens", "uncached_input_tokens"):
                delta[f"{metric}_delta"] = bootstrap[metric] - legacy[metric]
            strata.setdefault(bootstrap_stratum, []).append(delta)
        for stratum, items in strata.items():
            metrics: dict[str, Any] = {"pairs": len(items), "items": items}
            for metric in ("input_tokens", "cached_input_tokens", "uncached_input_tokens"):
                values = [item[f"{metric}_delta"] for item in items]
                metrics[f"{metric}_delta_min"] = min(values)
                metrics[f"{metric}_delta_median"] = statistics.median(values)
                metrics[f"{metric}_delta_max"] = max(values)
            paired[stratum] = metrics
    return {"schema_version": 4, "sessions": reports, "summary": summary,
            "session_summary": session_summary, "controlled_summary": controlled_summary,
            "paired_summary": paired,
            "caveat": "Observed model-turn totals include the full Codex prompt; they are not isolated marginal tool costs."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", nargs="*", type=Path, help="Explicit Codex rollout JSONL paths")
    parser.add_argument("--manifest", type=Path, help="Paired experiment manifest with snapshot digest and strata")
    parser.add_argument("--output", type=Path, help="Write aggregate JSON to this path")
    args = parser.parse_args()
    if bool(args.manifest) == bool(args.sessions):
        parser.error("provide either --manifest or one or more session paths")
    if args.manifest:
        reports, experiment = analyze_manifest(args.manifest)
        result = summarize(reports)
        result["experiment"] = experiment
    else:
        result = summarize([analyze(path) for path in args.sessions])
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
