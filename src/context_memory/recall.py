"""Session-independent, token-bounded context recall (vNext)."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import PROMOTABLE_EVENT_KINDS


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_WORDS = re.compile(r"[\w-]+", flags=re.UNICODE)
_FILE_PATHS = re.compile(
    r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]+"
)
_REPOSITORY_TEXT_SUFFIXES = {
    ".c",
    ".css",
    ".go",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
_KOREAN_ACTION_GLOSSES = {
    "업데이트": "update",
}

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


def _cross_language_glosses(value: str, limit: int = 3) -> list[str]:
    """Expose a few deterministic English action terms from mixed KO/EN text."""
    glosses = [
        f"same {match.group(1)}"
        for match in re.finditer(
            r"같은\s+([A-Za-z][A-Za-z0-9_-]*)", value, flags=re.IGNORECASE
        )
    ]
    glosses.extend(
        gloss
        for source, gloss in _KOREAN_ACTION_GLOSSES.items()
        if source in value
    )
    return list(dict.fromkeys(glosses))[:limit]


def _repository_artifacts(
    repository_path: str,
    query: str,
    context: str,
    *,
    limit: int = 3,
    max_files: int = 128,
    max_bytes: int = 256 * 1024,
    max_entries: int = 512,
) -> list[str]:
    """Find a few relevant artifact paths within a bounded repository scan."""
    root = Path(repository_path)
    if not root.is_dir():
        return []
    terms = _terms(f"{query} {context}")
    ranked: list[tuple[int, str]] = []
    inspected = total_bytes = enumerated = 0
    pending = [root]
    while pending and inspected < max_files and total_bytes < max_bytes:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                enumerated += 1
                if enumerated > max_entries:
                    pending.clear()
                    break
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                path = Path(entry.path)
                if path.suffix.casefold() not in _REPOSITORY_TEXT_SUFFIXES:
                    continue
                if inspected >= max_files or total_bytes >= max_bytes:
                    pending.clear()
                    break
                inspected += 1
                relative_path = path.relative_to(root)
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                    if size > max_bytes - total_bytes:
                        continue
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                total_bytes += size
                relative = relative_path.as_posix()
                overlap = len(terms & _terms(f"{relative} {content}"))
                if overlap:
                    ranked.append((overlap, relative))
                    ranked.extend(
                        (overlap, artifact)
                        for artifact in _artifact_paths(content)
                    )
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return list(dict.fromkeys(relative for _, relative in ranked))[:limit]


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

    def _recent_project_events(
        self, project_id: str, scope_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """Load a bounded newest-first tail when review has not made memory yet."""
        cursor = self.store.cursor_for_recent_events(
            project_id,
            list(PROMOTABLE_EVENT_KINDS),
            scope_id,
            limit,
        )
        events = self.store.read_events_since(
            project_id,
            cursor,
            list(PROMOTABLE_EVENT_KINDS),
            scope_id,
            limit,
        )["events"]
        return list(reversed(events))

    def _discover_recent_events(
        self, query: str, limit: int
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Select one unambiguous project from a bounded raw-event window."""
        placeholders = ",".join("?" for _ in PROMOTABLE_EVENT_KINDS)
        rows = [
            dict(row)
            for row in self.store.conn.execute(
                "SELECT e.*,p.name AS project_name FROM events e "
                "JOIN projects p ON p.id=e.project_id "
                f"WHERE e.kind IN ({placeholders}) "
                "ORDER BY e.created_at DESC,e.id LIMIT 96",
                tuple(PROMOTABLE_EVENT_KINDS),
            )
        ]
        query_terms = _terms(query)
        grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for row in rows:
            overlap = len(
                query_terms
                & _terms(f"{row['project_name']} {row['content']}")
            )
            if overlap:
                grouped.setdefault(row["project_id"], []).append((overlap, row))
        ranked = sorted(
            (
                (max(score for score, _ in events), len(events), project_id)
                for project_id, events in grouped.items()
            ),
            reverse=True,
        )
        if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
            return None, []
        project_id = ranked[0][2]
        events = [
            event
            for _, event in sorted(
                grouped[project_id],
                key=lambda item: (-item[0], -item[1]["event_seq"]),
            )
        ]
        for event in events:
            event["metadata"] = json.loads(event.pop("metadata_json"))
        return project_id, events[:limit]

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
        origin_scope_id = resolved.get("scope_id")
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

        recent_events: list[dict[str, Any]] = []
        if selected_project_id is None:
            recent_events = self._recent_project_events(
                origin_id, origin_scope_id, limit * 2
            )
            if recent_events:
                selected_project_id = origin_id
                selection_reason = "cwd_recent_events"
            else:
                selected_project_id, recent_events = (
                    self._discover_recent_events(retrieval_query, limit * 2)
                )
                if selected_project_id is not None:
                    selection_reason = "unambiguous_recent_events"

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
            glosses = _cross_language_glosses(text)
            gloss_cost = estimate_tokens(" ".join(glosses)) + 2
            if glosses and used + cost + gloss_cost <= budget:
                item["glosses"] = glosses
                cost += gloss_cost
            pack.append(item)
            used += cost
            if len(pack) >= limit:
                break

        if not pack and recent_events:
            for event in recent_events:
                text = _compact_text(event["content"], query)
                item = {
                    "kind": event["kind"],
                    "text": text,
                    "source_event_ids": [event["id"]],
                }
                artifacts = _artifact_paths(event["content"])
                if artifacts:
                    item["artifacts"] = artifacts
                cost = estimate_tokens(text) + 10 + estimate_tokens(
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
        if not paths and recent_events:
            for event in recent_events:
                repository_path = event.get("metadata", {}).get(
                    "repository_path"
                )
                if isinstance(repository_path, str) and repository_path:
                    paths.append(repository_path)
                    break
        known = {
            artifact
            for item in pack
            for artifact in item.get("artifacts", [])
        }
        if paths and pack and len(known) < 3:
            packed_context = " ".join(item["text"] for item in pack)
            discovered = _repository_artifacts(paths[0], query, packed_context)
            for artifact in discovered:
                cost = estimate_tokens(artifact) + 2
                if artifact in known or used + cost > budget:
                    continue
                pack[0].setdefault("artifacts", []).append(artifact)
                known.add(artifact)
                used += cost
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
