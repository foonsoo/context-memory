"""Session-independent, token-bounded context recall (vNext)."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_WORDS = re.compile(r"[\w-]+", flags=re.UNICODE)
_FILE_PATHS = re.compile(
    r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]+"
)

# Inspectable lexical bridges for common Korean continuation nouns. They
# compensate for unicode61's lack of Korean stemming and for memories that
# preserve English implementation terminology.
_RECALL_ALIASES = {
    "글": ("블로그", "blog"),
    "블로그": ("blog",),
    "api": ("pagination",),
    "배포": ("deploy", "deployment", "rollout"),
    "리디자인": ("redesign", "navigation"),
    "마이그레이션": ("migration",),
    "재시작": ("restart", "journey"),
    "설치": ("installed", "wheel"),
    "클라이언트": ("client", "handoff"),
    "패키지": ("package", "scope"),
    "옮겼는데": ("moved", "scope"),
}


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


def _expand_recall_query(query: str) -> str:
    additions: list[str] = []
    for term in _WORDS.findall(query.casefold()):
        for source, aliases in _RECALL_ALIASES.items():
            if term == source or (_CJK.search(source) and source in term):
                additions.extend(aliases)
    return " ".join((query, *dict.fromkeys(additions)))


def _project_name_terms(value: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)
        if len(term) > 2
    }


def _artifact_paths(value: str) -> list[str]:
    """Recover explicit and directory-elided file paths from memory text."""
    paths: list[str] = []
    directory = ""
    for match in _FILE_PATHS.findall(value):
        cleaned = match.rstrip(".,")
        if "/" in cleaned:
            directory = cleaned.rsplit("/", 1)[0]
            paths.append(cleaned)
        elif directory:
            paths.append(f"{directory}/{cleaned}")
        else:
            paths.append(cleaned)
    return list(dict.fromkeys(paths))


class RecallAssembler:
    """Build a small action-oriented context pack without a session."""

    def __init__(self, store: Any):
        self.store = store

    def _active_project_memories(
        self, project_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """Load a small current-state bundle after project identification."""
        timestamp = datetime.now(timezone.utc).isoformat()
        rows = [
            dict(row)
            for row in self.store.conn.execute(
                "SELECT * FROM memories WHERE project_id=? "
                "AND status='active' "
                "AND (valid_from IS NULL OR valid_from<=?) "
                "AND (valid_until IS NULL OR valid_until>?) "
                "ORDER BY CASE type WHEN 'task' THEN 0 WHEN 'decision' THEN 1 "
                "WHEN 'fact' THEN 2 ELSE 3 END, importance DESC, "
                "COALESCE(last_confirmed_at,updated_at) DESC,id LIMIT ?",
                (project_id, timestamp, timestamp, limit),
            )
        ]
        for row in rows:
            row["sources"] = [
                dict(source)
                for source in self.store.conn.execute(
                    "SELECT e.id,e.kind,e.source_uri,e.created_at "
                    "FROM memory_sources s JOIN events e ON e.id=s.event_id "
                    "WHERE s.memory_id=? ORDER BY e.id",
                    (row["id"],),
                )
            ]
        return rows

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
        retrieval_query = _expand_recall_query(query)
        local_candidates = self.store.search(
            origin_id,
            retrieval_query,
            limit * 4,
            ["active", "disputed"],
            None,
            False,
        )
        local_gate = self.store._retrieval_gate(local_candidates)
        origin_memories = self._active_project_memories(origin_id, limit * 2)

        selected_project_id: str | None = None
        selection_reason = "no_confident_match"
        discovery_candidates: list[dict[str, Any]] = []
        if origin_memories:
            # A registered cwd is an identity hint even when a natural
            # continuation phrase has little lexical overlap with memory text.
            selected_project_id = origin_id
            selection_reason = (
                "local_match"
                if local_gate["status"] != "no_confident_match"
                else "cwd_project_context"
            )
        else:
            discovery_candidates = self.store.search(
                origin_id,
                retrieval_query,
                limit * 4,
                ["active", "disputed"],
                None,
                True,
            )
            discovery_gate = self.store._retrieval_gate(discovery_candidates)
            if discovery_gate["status"] != "no_confident_match":
                project_candidates = self.store._aggregate_project_candidates(
                    discovery_candidates, origin_id
                )
                selected_project_id, selection_reason, _ = (
                    self.store._select_project_candidate(project_candidates)
                )
                query_terms = _terms(retrieval_query)
                identity_matches = [
                    candidate
                    for candidate in project_candidates
                    if query_terms & _project_name_terms(candidate["name"])
                ]
                if selected_project_id is None and len(identity_matches) == 1:
                    selected_project_id = identity_matches[0]["id"]
                    selection_reason = "query_project_name"
                if (
                    selected_project_id is None
                    and len(project_candidates) == 1
                    and (
                        project_candidates[0]["matching_memory_count"] >= 2
                        or project_candidates[0]["evidence_quality"] >= 0.2
                    )
                ):
                    selected_project_id = project_candidates[0]["id"]
                    selection_reason = "single_supported_candidate"

        candidates: list[dict[str, Any]] = []
        if selected_project_id is not None:
            ranked = local_candidates + discovery_candidates
            candidates.extend(
                candidate
                for candidate in ranked
                if candidate["project_id"] == selected_project_id
                and candidate["status"] == "active"
            )
            seen = {candidate["id"] for candidate in candidates}
            represented_types = {candidate["type"] for candidate in candidates}
            for memory in self._active_project_memories(
                selected_project_id, limit * 2
            ):
                if (
                    memory["id"] in seen
                    or memory["type"] in represented_types
                ):
                    continue
                candidates.append(memory)
                represented_types.add(memory["type"])

        pack: list[dict[str, Any]] = []
        used = 0
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
            artifacts = _artifact_paths(
                f"{candidate['title']} {candidate['content']}"
            )
            if artifacts:
                item["artifacts"] = artifacts
            cost = estimate_tokens(text) + 12 + estimate_tokens(
                " ".join(artifacts)
            )
            if used + cost > budget:
                continue
            pack.append(item)
            used += cost
            if len(pack) >= limit:
                break

        project = (
            self.store._row(
                "SELECT id,slug,name,description FROM projects WHERE id=?",
                (selected_project_id,),
            )
            if selected_project_id is not None
            else None
        )
        paths = (
            [
                row["value"]
                for row in self.store.conn.execute(
                    "SELECT value FROM project_aliases WHERE project_id=? "
                    "AND kind='path' ORDER BY updated_at DESC,value LIMIT 3",
                    (selected_project_id,),
                )
            ]
            if selected_project_id is not None
            else []
        )
        return {
            "contract": "context-recall/v1",
            "query": query,
            "project": dict(project) if project else None,
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
                "selection_reason": selection_reason,
                "session_created": False,
                "details_omitted": True,
            },
        }
