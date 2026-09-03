"""Session-independent, token-bounded context recall (vNext)."""

from __future__ import annotations

import math
import re
from typing import Any


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_WORDS = re.compile(r"[\w-]+", flags=re.UNICODE)


def estimate_tokens(value: str) -> int:
    """Return a conservative dependency-free token estimate.

    Exact tokenization belongs at the client/model boundary.  The server only
    needs a stable upper-bound-like measure to avoid returning large payloads.
    """
    cjk = len(_CJK.findall(value))
    remainder = _CJK.sub(" ", value)
    word_cost = sum(
        max(1, math.ceil(len(word) / 4))
        for word in _WORDS.findall(remainder)
    )
    punctuation = len(re.findall(r"[^\w\s]", remainder, flags=re.UNICODE))
    return cjk + word_cost + math.ceil(punctuation / 2)


def _terms(value: str) -> set[str]:
    return {
        token
        for token in _WORDS.findall(value.casefold())
        if len(token) > 1
    }


def _compact_text(value: str, query: str, limit: int = 240) -> str:
    """Select query-bearing sentences before falling back to the lead."""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    terms = _terms(query)
    sentences = re.split(r"(?<=[.!?。]|다\.)\s+", normalized)
    relevant = [sentence for sentence in sentences if terms & _terms(sentence)]
    selected = " ".join(relevant[:2]) if relevant else normalized
    return selected[: limit - 1].rstrip() + "…"


class RecallAssembler:
    """Build a small action-oriented context pack without a session."""

    def __init__(self, store: Any):
        self.store = store

    def recall(
        self,
        cwd: str,
        query: str,
        token_budget: int = 350,
        max_items: int = 6,
    ) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query must not be empty")
        budget = max(64, min(token_budget, 2048))
        limit = max(1, min(max_items, 12))
        resolved = self.store.resolve_project(cwd)
        origin_id = resolved["project"]["id"]
        candidates = self.store.search(
            origin_id,
            query,
            limit * 4,
            ["active", "disputed"],
            None,
            True,
        )
        gate = self.store._retrieval_gate(candidates)
        if gate["status"] == "no_confident_match":
            candidates = []

        pack: list[dict[str, Any]] = []
        used = 0
        selected_project_id = origin_id
        for candidate in candidates:
            if candidate["status"] != "active":
                continue
            text = _compact_text(
                f"{candidate['title']}: {candidate['content']}", query
            )
            item = {
                "kind": candidate["type"],
                "text": text,
                "memory_id": candidate["id"],
                "source_event_ids": [
                    source["id"] for source in candidate["sources"]
                ],
            }
            cost = estimate_tokens(text) + 12
            if used + cost > budget:
                continue
            if not pack:
                selected_project_id = candidate["project_id"]
            if candidate["project_id"] != selected_project_id:
                continue
            pack.append(item)
            used += cost
            if len(pack) >= limit:
                break

        project = self.store._row(
            "SELECT id,slug,name,description FROM projects WHERE id=?",
            (selected_project_id,),
        )
        paths = [
            row["value"]
            for row in self.store.conn.execute(
                "SELECT value FROM project_aliases WHERE project_id=? "
                "AND kind='path' ORDER BY updated_at DESC,value LIMIT 3",
                (selected_project_id,),
            )
        ]
        return {
            "contract": "context-recall/v1",
            "query": query,
            "project": dict(project) if project else resolved["project"],
            "repository_path": paths[0] if paths else None,
            "items": pack,
            "budget": {
                "unit": "estimated_tokens",
                "limit": budget,
                "used": used,
                "exact": False,
            },
            "retrieval": {
                "status": "found" if pack else "no_confident_match",
                "candidate_count": len(candidates),
                "returned_count": len(pack),
                "session_created": False,
                "details_omitted": True,
            },
        }
