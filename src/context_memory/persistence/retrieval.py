"""Retrieval candidate persistence and ranking assembly."""

import json
import re
import time
from datetime import datetime
from typing import Any, Callable

from ..retrieval import (
    DISCOVERY_PROJECT_CANDIDATE_LIMIT,
    LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT,
    LOCAL_HASH_FALLBACK_TIME_LIMIT_MS,
)


class RetrievalRepository:
    """Own retrieval candidate SQL behind the stable facade."""

    def __init__(
        self,
        store: Any,
        now: Callable[[], str],
        current_datetime: Callable[[], datetime],
    ):
        self.store = store
        self.now = now
        self.current_datetime = current_datetime

    def search(
        self,
        project_id: str,
        query: str,
        limit: int = 10,
        statuses: list[str] | None = None,
        scope_id: str | None = None,
        discover_projects: bool = False,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        query_tokens = list(
            dict.fromkeys(
                re.findall(r"[\w-]+", query.casefold(), flags=re.UNICODE)
            )
        )
        if not query_tokens:
            return []
        token_alternatives: list[list[str]] = []
        for token in query_tokens:
            alternatives = [token]
            row = self.store._row(
                "SELECT aliases_json FROM search_aliases WHERE project_id=?"
                " AND term=?",
                (project_id, token),
            )
            if row:
                for alias in json.loads(row["aliases_json"]):
                    alternatives.extend(
                        re.findall(r"[\w-]+", alias, flags=re.UNICODE)
                    )
            token_alternatives.append(list(dict.fromkeys(alternatives)))

        def quote(value):
            return '"' + value.replace('"', '""') + '"'

        strict_match = " AND ".join(
            (
                (
                    "("
                    + " OR ".join(quote(value) for value in alternatives)
                    + ")"
                )
                if len(alternatives) > 1
                else quote(alternatives[0])
            )
            for alternatives in token_alternatives
        )
        tokens = list(
            dict.fromkeys(
                value
                for alternatives in token_alternatives
                for value in alternatives
            )
        )
        broad_match = " OR ".join(quote(token) for token in tokens)
        allowed = statuses or ["active", "proposed", "disputed"]
        placeholders = ",".join("?" for _ in allowed)
        timestamp = self.now()
        # Discovery is deliberately whole-database. Project identity
        # hints are a
        # later prior, not a candidate-generation boundary: filtering
        # here can
        # make the actually relevant project impossible to retrieve.
        boundary = (
            "1=1"
            if discover_projects
            else "(m.project_id=? OR m.visibility='global')"
        )
        boundary_args: list[Any] = [] if discover_projects else [project_id]
        lexical_sql = f"""SELECT m.*,
          bm25(memories_fts, 0, 5, 1, .5) AS fts_rank
          FROM memories_fts JOIN memories m ON m.id=memories_fts.memory_id
          WHERE memories_fts MATCH ? AND {boundary}
          AND m.status IN ({placeholders})
          AND (m.valid_from IS NULL OR m.valid_from<=?)
          AND (m.valid_until IS NULL OR m.valid_until>?)"""
        lexical_args: list[Any] = [
            *boundary_args,
            *allowed,
            timestamp,
            timestamp,
        ]
        if scope_id and not discover_projects:
            lexical_sql += " AND (m.scope_id=? OR m.scope_id IS NULL)"
            lexical_args.append(scope_id)
        candidate_limit = max(20, min(max(1, limit) * 4, 200))
        lexical_sql += " ORDER BY bm25(memories_fts,0,5,1,.5) ASC LIMIT ?"
        strict = [
            dict(r)
            for r in self.store.conn.execute(
                lexical_sql, [strict_match, *lexical_args, candidate_limit]
            )
        ]
        strict_target = min(max(1, limit), candidate_limit)
        lexical_strategy = "strict"
        if len(strict) >= strict_target or strict_match == broad_match:
            lexical = strict
        else:
            lexical = [
                dict(r)
                for r in self.store.conn.execute(
                    lexical_sql, [broad_match, *lexical_args, candidate_limit]
                )
            ]
            lexical_strategy = "broad_fallback"
        candidates = {row["id"]: row for row in lexical}
        components: dict[str, dict[str, float]] = {
            row["id"]: {"lexical_rrf": 1.0 / (60 + rank), "semantic_rrf": 0.0}
            for rank, row in enumerate(lexical, 1)
        }
        semantic_scores: dict[str, float] = {}
        semantic_scan = {
            "mode": "disabled",
            "candidate_limit": 0,
            "time_limit_ms": 0,
            "evaluated": 0,
            "truncated": False,
        }
        if self.store.embedding_provider:
            query_vector = self.store.embedding_provider.embed([query])[0]
            vector_only_threshold = getattr(
                self.store.embedding_provider, "vector_only_threshold", None
            )
            supplements_lexical = bool(
                getattr(
                    self.store.embedding_provider,
                    "supplements_lexical_results",
                    False,
                )
            )
            discovery_project_ids: list[str] | None = None
            sem_boundary = boundary
            if discover_projects:
                discovery_project_ids = (
                    self.store._discovery_project_candidates(
                        project_id, query_tokens, lexical
                    )
                )
                if discovery_project_ids:
                    sem_boundary = (
                        "(m.project_id IN ("
                        + ",".join("?" for _ in discovery_project_ids)
                        + ") OR m.visibility='global')"
                    )
                else:
                    sem_boundary = "m.visibility='global'"
            sem_sql = f"""SELECT m.id, e.vector_json
              FROM memory_embeddings e
              JOIN memories m ON m.id=e.memory_id
              WHERE {sem_boundary} AND m.status IN ({placeholders})
              AND e.provider=? AND e.dimensions=?
              AND (m.valid_from IS NULL OR m.valid_from<=?)
              AND (m.valid_until IS NULL OR m.valid_until>?)"""
            sem_boundary_args = (
                discovery_project_ids or []
                if discover_projects
                else boundary_args
            )
            sem_args: list[Any] = [
                *sem_boundary_args,
                *allowed,
                self.store._provider_name(),
                self.store.embedding_provider.dimensions,
                timestamp,
                timestamp,
            ]
            if scope_id and not discover_projects:
                sem_sql += " AND (m.scope_id=? OR m.scope_id IS NULL)"
                sem_args.append(scope_id)
            if lexical and not supplements_lexical:
                lexical_ids = sorted(candidates)
                sem_sql += (
                    " AND m.id IN (" + ",".join("?" for _ in lexical_ids) + ")"
                )
                sem_args.extend(lexical_ids)
                semantic_scan = {
                    "mode": "lexical_rerank",
                    "candidate_limit": len(lexical_ids),
                    "time_limit_ms": 0,
                    "evaluated": 0,
                    "truncated": False,
                }
                scan_deadline = None
            else:
                sem_sql += " ORDER BY m.id LIMIT ?"
                sem_args.append(LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT + 1)
                semantic_scan = {
                    "mode": "vector_fallback",
                    "candidate_limit": LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT,
                    "time_limit_ms": LOCAL_HASH_FALLBACK_TIME_LIMIT_MS,
                    "evaluated": 0,
                    "truncated": False,
                }
                scan_deadline = (
                    time.perf_counter()
                    + LOCAL_HASH_FALLBACK_TIME_LIMIT_MS / 1000
                )
            if discover_projects:
                semantic_scan.update(
                    {
                        "project_candidate_limit": (
                            DISCOVERY_PROJECT_CANDIDATE_LIMIT
                        ),
                        "project_candidate_count": len(
                            discovery_project_ids or []
                        ),
                        "project_candidate_ids": discovery_project_ids or [],
                    }
                )
            semantic: list[tuple[float, str]] = []
            for row in self.store.conn.execute(sem_sql, sem_args):
                if (
                    semantic_scan["evaluated"]
                    >= semantic_scan["candidate_limit"]
                ):
                    semantic_scan["truncated"] = True
                    break
                if (
                    scan_deadline is not None
                    and time.perf_counter() >= scan_deadline
                ):
                    semantic_scan["truncated"] = True
                    break
                semantic_scan["evaluated"] += 1
                vector = json.loads(row["vector_json"])
                similarity = sum(a * b for a, b in zip(query_vector, vector))
                # Weak similarities may rerank lexical hits. A provider
                # may also
                # opt into vector-only recall with an explicit
                # calibrated threshold;
                # this must remain available even when FTS returns an
                # unrelated
                # hit.
                if similarity > 0.05 and (
                    row["id"] in candidates
                    or (
                        vector_only_threshold is not None
                        and (not lexical or supplements_lexical)
                        and len(query_tokens) >= 2
                        and similarity >= vector_only_threshold
                    )
                ):
                    semantic.append((similarity, row["id"]))
            semantic.sort(key=lambda value: (-value[0], value[1]))
            selected_semantic = semantic[:candidate_limit]
            missing_ids = [
                memory_id
                for _, memory_id in selected_semantic
                if memory_id not in candidates
            ]
            if missing_ids:
                missing_placeholders = ",".join("?" for _ in missing_ids)
                candidates.update(
                    {
                        row["id"]: dict(row)
                        for row in self.store.conn.execute(
                            "SELECT * FROM memories WHERE id IN"
                            f" ({missing_placeholders})",
                            missing_ids,
                        )
                    }
                )
            for rank, (similarity, memory_id) in enumerate(
                selected_semantic, 1
            ):
                component = components.setdefault(
                    memory_id, {"lexical_rrf": 0.0, "semantic_rrf": 0.0}
                )
                component["semantic_rrf"] = 1.0 / (60 + rank)
                semantic_scores[memory_id] = similarity
        candidate_ids = list(candidates)
        usage: dict[str, dict[str, Any]] = {}
        sources: dict[str, list[dict[str, Any]]] = {
            memory_id: [] for memory_id in candidate_ids
        }
        if candidate_ids:
            candidate_placeholders = ",".join("?" for _ in candidate_ids)
            usage = {
                row["memory_id"]: dict(row)
                for row in self.store.conn.execute(
                    "SELECT * FROM memory_usage WHERE memory_id IN"
                    f" ({candidate_placeholders})",
                    candidate_ids,
                )
            }
            for source in self.store.conn.execute(
                f"""SELECT s.memory_id,e.id,e.kind,e.source_uri,e.created_at
              FROM memory_sources s JOIN events e ON e.id=s.event_id
              WHERE s.memory_id IN ({candidate_placeholders})
              ORDER BY s.memory_id,e.id""",
                candidate_ids,
            ):
                item = dict(source)
                sources[item.pop("memory_id")].append(item)
        current = self.current_datetime()
        for memory_id, row in candidates.items():
            confirmed = row.get("last_confirmed_at") or row.get("updated_at")
            try:
                age_days = (
                    max(
                        0.0,
                        (
                            current - datetime.fromisoformat(confirmed)
                        ).total_seconds()
                        / 86400,
                    )
                    if confirmed
                    else 3650.0
                )
            except ValueError:
                age_days = 3650.0
            freshness = 1.0 / (1.0 + age_days / 180.0)
            stats = usage.get(memory_id, {})
            helpful = (
                stats.get("helpful_count", 0)
                - stats.get("incorrect_count", 0) * 2
            )
            component = components.setdefault(
                memory_id, {"lexical_rrf": 0.0, "semantic_rrf": 0.0}
            )
            component.update(
                {
                    "importance": row["importance"] * 0.0015,
                    "confidence": row["confidence"] * 0.001,
                    "freshness": freshness * 0.0005,
                    "feedback": max(-5, min(5, helpful)) * 0.0002,
                }
            )
            component["total"] = sum(
                value for name, value in component.items() if name != "total"
            )
        rows = sorted(
            candidates.values(),
            key=lambda row: (-components[row["id"]]["total"], row["id"]),
        )[: max(1, min(limit, 100))]
        lexical_ranks = {
            row["id"]: rank for rank, row in enumerate(lexical, 1)
        }
        for r in rows:
            searchable_tokens = set(
                re.findall(
                    r"[\w-]+",
                    f"{r['title']} {r['content']} {r['tags_json']}".casefold(),
                    flags=re.UNICODE,
                )
            )
            query_coverage = sum(
                token in searchable_tokens for token in query_tokens
            ) / len(query_tokens)
            r["retrieval"] = {
                "score": components[r["id"]]["total"],
                "components": components[r["id"]],
                "lexical_rank": lexical_ranks.get(r["id"]),
                "lexical_strategy": lexical_strategy,
                "semantic_scan": semantic_scan,
                "query_coverage": query_coverage,
                "semantic_similarity": semantic_scores.get(r["id"]),
                "embedding_provider": self.store._provider_name(),
            }
            r["usage"] = usage.get(
                r["id"],
                {
                    "retrieved_count": 0,
                    "used_count": 0,
                    "helpful_count": 0,
                    "incorrect_count": 0,
                },
            )
            r["sources"] = sources[r["id"]]
        return rows
