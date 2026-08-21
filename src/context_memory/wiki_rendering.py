from __future__ import annotations

import json
from typing import Any

SECTION_LABELS = {
    "current_position": "Current position",
    "why_it_exists": "Why it exists",
    "governing_constraints": "Governing constraints",
    "considered_alternatives": "Considered alternatives",
    "trade_offs": "Trade-offs",
    "decision_timeline": "Decision timeline",
    "observed_outcomes": "Observed outcomes",
    "open_questions": "Open questions",
}


def render_wiki_markdown(
    title: str,
    manual_notes: str | None,
    revision: dict[str, Any],
) -> str:
    """Render one already-loaded Wiki revision as Markdown."""
    lines = [
        f"# {title}",
        "",
        (f"Status: {revision['status']} · Revision {revision['revision_no']}"),
        "",
    ]
    for key, label in SECTION_LABELS.items():
        lines.extend([f"## {label}", ""])
        entries = revision["sections"].get(key, [])
        if not entries:
            lines.extend(["_No cited material._", ""])
            continue
        for entry in entries:
            claim = (
                entry.get("claim")
                or entry.get("observed_outcome")
                or _canonical(entry)
            )
            refs = []
            for citation_key in (
                "citations",
                "decision_citation",
                "outcome_citation",
            ):
                citation = entry.get(citation_key)
                if citation:
                    source_events = ",".join(citation["source_event_ids"])
                    refs.append(
                        f"memory:{citation['memory_id']} "
                        f"events:{source_events}"
                    )
            lines.append(f"- {claim} ({'; '.join(refs)})")
        lines.append("")
    lines.extend(["## Manual notes", "", manual_notes or "_None._", ""])
    return "\n".join(lines)


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
