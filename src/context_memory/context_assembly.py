"""Bounded context response assembly."""

import json
from typing import Any


class ContextAssembler:
    """Compose retrieval and event results behind the store facade."""

    def __init__(self, store: Any):
        self.store = store

    def get_context(
        self,
        project_id: str,
        query: str,
        char_budget: int = 6000,
        statuses: list[str] | None = None,
        scope_id: str | None = None,
        event_cursor: int | None = None,
        event_kinds: list[str] | None = None,
        event_limit: int = 20,
        event_char_budget: int = 2000,
        discover_projects: bool = True,
        response_format: str = "legacy",
        prefer_latest_events: bool = False,
        exclude_event_ids: list[str] | None = None,
        compact_events: bool = False,
    ) -> dict[str, Any]:
        if response_format not in {"legacy", "compact"}:
            raise ValueError("response_format must be legacy or compact")
        policy = self.store.get_policy(project_id)
        requested = max(0, char_budget)
        budget = min(requested, policy["max_context_chars"])
        selected, used = [], 0
        recent_events: list[dict[str, Any]] = []
        event_used = 0
        event_result = None
        reserved = 0
        if event_cursor is not None:
            reserved = min(max(0, event_char_budget), 4000, budget)
            selected_kinds = (
                ["message"] if event_kinds is None else event_kinds
            )
            event_result = self.store.read_events_since(
                project_id, event_cursor, selected_kinds, scope_id, event_limit
            )
            event_rows = event_result["events"]
            excluded = set(exclude_event_ids or [])
            event_rows = [
                event for event in event_rows if event["id"] not in excluded
            ]
            if prefer_latest_events:
                event_rows = list(reversed(event_rows))
            for event in event_rows:
                prefix = f"[{event['event_seq']}/{event['kind']}] "
                remaining = reserved - event_used - len(prefix)
                if remaining <= 0:
                    break
                content = event["content"]
                truncated = len(content) > remaining
                text = prefix + (
                    content[: max(0, remaining - 1)] + "…"
                    if truncated and remaining
                    else content
                )
                rendered_event = {
                    "event_id": event["id"],
                    "event_seq": event["event_seq"],
                    "kind": event["kind"],
                    "text": text,
                    "created_at": event["created_at"],
                    "session_id": event["session_id"],
                    "scope_id": event["scope_id"],
                    "metadata": event["metadata"],
                    "content_truncated": truncated,
                }
                if compact_events:
                    for key in ("session_id", "scope_id", "metadata"):
                        rendered_event.pop(key)
                if prefer_latest_events:
                    recent_events.insert(0, rendered_event)
                else:
                    recent_events.append(rendered_event)
                event_used += len(text)
            fully_consumed = len(recent_events) == len(event_rows)
            event_result["next_cursor"] = (
                event_result["next_cursor"]
                if fully_consumed
                else (
                    recent_events[-1]["event_seq"]
                    if recent_events
                    else event_cursor
                )
            )
            event_result["has_more"] = (
                event_result["has_more"] or not fully_consumed
            )
        memory_budget = budget - event_used
        selected_texts: list[str] = []
        candidates = self.store.search(
            project_id,
            query,
            policy["max_context_items"] * 3,
            statuses or ["active", "disputed"],
            scope_id,
        )
        retrieval_gate = self.store._retrieval_gate(candidates)
        if retrieval_gate["status"] == "no_confident_match":
            candidates = []
        local_matches = [
            m for m in candidates if m["project_id"] == project_id
        ]
        discovery_used = bool(discover_projects and not local_matches)
        discovery_candidates: list[dict[str, Any]] = []
        if discovery_used:
            discovery_candidates = self.store.search(
                project_id,
                query,
                policy["max_context_items"] * 3,
                statuses or ["active", "disputed"],
                None,
                True,
            )
            discovery_gate = self.store._retrieval_gate(discovery_candidates)
            if discovery_gate["status"] == "no_confident_match":
                discovery_candidates = []
            seen = {m["id"] for m in candidates}
            candidates.extend(
                m for m in discovery_candidates if m["id"] not in seen
            )
        project_candidates = self.store._aggregate_project_candidates(
            discovery_candidates, project_id
        )
        selected_project_id, selection_reason, discovery_confidence = (
            self.store._select_project_candidate(project_candidates)
        )
        discovery_ambiguous = selection_reason == "ambiguous_candidates"
        if discovery_used:
            candidates = [
                m
                for m in candidates
                if m["project_id"] == project_id
                or m["visibility"] == "global"
                or m["project_id"] == selected_project_id
            ]
        eligible = 0
        for m in candidates:
            block = (
                f"[{m['status']}/{m['type']}]"
                f" {m['title']}\n{m['content']}\nsource_events:"
                f" {', '.join(s['id'] for s in m['sources']) or 'none'}"
            )
            comparable = f"{m['title']} {m['content']}"
            if any(
                self.store._text_similarity(comparable, previous) >= 0.8
                for previous in selected_texts
            ):
                continue
            eligible += 1
            if len(selected) >= policy["max_context_items"]:
                continue
            if used + len(block) + 2 > memory_budget:
                continue
            item = {
                "memory_id": m["id"],
                "project_id": m["project_id"],
                "visibility": m["visibility"],
                "confidence": m["confidence"],
                "importance": m["importance"],
            }
            if response_format == "legacy":
                item["text"] = block
            else:
                item.update(
                    {
                        "status": m["status"],
                        "type": m["type"],
                        "title": m["title"],
                        "content": m["content"],
                        "source_event_ids": [s["id"] for s in m["sources"]],
                        "tags": json.loads(m["tags_json"]),
                        "observed_at": m["observed_at"],
                        "valid_from": m["valid_from"],
                        "valid_until": m["valid_until"],
                        "last_confirmed_at": m["last_confirmed_at"],
                        "truncated": False,
                    }
                )
            selected.append(item)
            selected_texts.append(comparable)
            used += len(block) + 2
        result = {
            "query": query,
            "requested_budget": requested,
            "budget": budget,
            "budget_capped": requested > budget,
            "max_items": policy["max_context_items"],
            "memory_budget": memory_budget,
            "event_budget": reserved,
            "used": used + event_used,
            "memory_used": used,
            "event_used": event_used,
            "items": selected,
            "recent_events": recent_events,
            "retrieval_gate": retrieval_gate,
            "project_discovery": {
                "enabled": discover_projects,
                "used": discovery_used,
                "ambiguous": discovery_ambiguous,
                "project_ids": list(
                    dict.fromkeys(
                        i["project_id"]
                        for i in selected
                        if i["project_id"] != project_id
                    )
                ),
                "selected_project_id": selected_project_id,
                "confidence": discovery_confidence,
                "selection_reason": selection_reason,
                "candidates": project_candidates,
            },
            "event_cursor": event_cursor,
            "next_event_cursor": (
                event_result["next_cursor"] if event_result else None
            ),
            "event_snapshot_cursor": (
                event_result["snapshot_cursor"] if event_result else None
            ),
            "has_more_events": (
                event_result["has_more"] if event_result else False
            ),
            "response_format": response_format,
            "truncated": eligible > len(selected),
            "has_more": eligible > len(selected),
        }
        if response_format == "legacy":
            result["context"] = "\n\n".join(i["text"] for i in selected)
        return result
