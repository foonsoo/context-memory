"""Decision Brief retrieval and evidence assembly."""

import json
import re
from datetime import datetime
from typing import Any, Callable


class DecisionAssembler:
    """Compose cited Decision Briefs behind the stable store facade."""

    def __init__(
        self,
        store: Any,
        now: Callable[[], str],
        current_datetime: Callable[[], datetime],
    ):
        self.store = store
        self.now = now
        self.current_datetime = current_datetime

    def decision_context(
        self,
        project_id: str,
        question: str,
        char_budget: int = 6000,
        scope_id: str | None = None,
        discover_projects: bool = True,
    ) -> dict[str, Any]:
        """Compose a cited Decision Brief from existing retrieval."""
        context = self.store.get_context(
            project_id,
            question,
            char_budget,
            statuses=[
                "active",
                "disputed",
                "proposed",
                "superseded",
                "rejected",
                "expired",
            ],
            scope_id=scope_id,
            discover_projects=discover_projects,
            response_format="compact",
        )
        context["items"] = self._rerank_decision_candidates(
            question, context["items"]
        )
        context["decision_rerank"] = {
            "mode": "bounded_post_retrieval",
            "candidate_count": len(context["items"]),
            "general_search_unchanged": True,
        }
        self._expand_decision_seeds(project_id, context, scope_id)
        sections: dict[str, list[dict[str, Any]]] = {
            "current_decisions": [],
            "rationale": [],
            "constraints": [],
            "alternatives": [],
            "outcomes": [],
            "history": [],
            "disputes": [],
            "open_questions": [],
        }
        citations: dict[str, dict[str, Any]] = {}
        uncertain: list[dict[str, Any]] = []
        for memory in context["items"]:
            tags = {
                tag.casefold().replace("_", "-")
                for tag in memory.get("tags", [])
            }
            entry = {
                "claim": memory["content"],
                "title": memory["title"],
                "status": memory["status"],
                "memory_type": memory["type"],
                "observed_at": memory.get("observed_at"),
                "citations": {
                    "memory_id": memory["memory_id"],
                    "source_event_ids": memory["source_event_ids"],
                },
            }
            citations[memory["memory_id"]] = entry["citations"]
            if memory["status"] == "disputed":
                sections["disputes"].append(entry)
            if memory["status"] == "proposed":
                uncertain.append(
                    {
                        **entry,
                        "reason": "unreviewed_proposed_memory",
                        "kind": "evidence_state",
                    }
                )
            if "open-question" in tags or "question" in tags:
                sections["open_questions"].append(entry)
            elif "outcome" in tags or "observed-outcome" in tags:
                sections["outcomes"].append(entry)
            elif "alternative" in tags or memory["status"] == "rejected":
                sections["alternatives"].append(entry)
            elif "rationale" in tags or "reason" in tags:
                sections["rationale"].append(entry)
            elif memory["type"] == "constraint":
                sections["constraints"].append(entry)
            elif memory["type"] == "decision" and memory["status"] == "active":
                sections["current_decisions"].append(entry)
            if memory["type"] == "decision" and memory["status"] in {
                "active",
                "superseded",
                "rejected",
                "disputed",
            }:
                sections["history"].append(entry)
            if not memory["source_event_ids"]:
                uncertain.append(
                    {
                        **entry,
                        "reason": "missing_source_event",
                        "kind": "evidence_gap",
                    }
                )
        sections["history"].sort(
            key=lambda item: (
                item["observed_at"] or "",
                item["citations"]["memory_id"],
            )
        )
        if not sections["current_decisions"]:
            uncertain.append(
                {
                    "kind": "retrieval_gap",
                    "reason": "no_current_decision_retrieved",
                    "citations": None,
                }
            )
        elif not sections["rationale"]:
            uncertain.append(
                {
                    "kind": "evidence_gap",
                    "reason": "missing_rationale",
                    "citations": None,
                }
            )
        return {
            "contract_version": "decision-brief/v1",
            "question": question,
            **sections,
            "expected_vs_observed": self._decision_outcome_comparisons(
                [item["memory_id"] for item in context["items"]]
            ),
            "uncertainty": uncertain,
            "citation_index": citations,
            "retrieval": context,
            "recommendation": None,
        }

    def _rerank_decision_candidates(
        self, question: str, memories: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Rerank only a Decision Brief's bounded retrieval results."""
        question_tokens = set(
            re.findall(r"[\w-]+", question.casefold(), flags=re.UNICODE)
        )
        intent_terms = {
            "decision": {
                "choose",
                "choice",
                "decision",
                "decide",
                "selected",
                "선택",
                "결정",
            },
            "rationale": {
                "why",
                "reason",
                "rationale",
                "because",
                "근거",
                "이유",
            },
            "constraint": {
                "constraint",
                "requirement",
                "limit",
                "must",
                "제약",
                "요구사항",
            },
            "alternative": {
                "alternative",
                "option",
                "instead",
                "rejected",
                "대안",
                "후보",
            },
            "outcome": {
                "outcome",
                "result",
                "impact",
                "effect",
                "measured",
                "결과",
                "효과",
                "성과",
            },
        }
        requested_roles = {
            role
            for role, terms in intent_terms.items()
            if question_tokens & terms
        }
        current = self.current_datetime()
        ranked: list[dict[str, Any]] = []
        for base_rank, memory in enumerate(memories, 1):
            tags = {
                tag.casefold().replace("_", "-")
                for tag in memory.get("tags", [])
            }
            roles: set[str] = set()
            if memory["type"] == "decision" and memory["status"] == "active":
                roles.add("decision")
            if memory["type"] == "constraint":
                roles.add("constraint")
            if memory["status"] == "rejected" or "alternative" in tags:
                roles.add("alternative")
            if tags & {"rationale", "reason"}:
                roles.add("rationale")
            if tags & {"outcome", "observed-outcome"}:
                roles.add("outcome")
            components = {
                "base_reciprocal_rank": 1.0 / (60 + base_rank),
                "question_intent": 0.006 if requested_roles & roles else 0.0,
                "memory_type_status": (
                    0.005
                    if "decision" in roles
                    else (
                        0.003
                        if memory["status"] in {"active", "disputed"}
                        else 0.0
                    )
                ),
                "direct_provenance": (
                    0.004 if memory.get("source_event_ids") else 0.0
                ),
                "decision_role": 0.004 if roles else 0.0,
                "unsupported_penalty": (
                    -0.006 if not memory.get("source_event_ids") else 0.0
                ),
                "stale_proposed_penalty": 0.0,
                "repetitive_handoff_penalty": 0.0,
            }
            if memory["status"] == "proposed":
                confirmed = memory.get("last_confirmed_at") or memory.get(
                    "observed_at"
                )
                try:
                    stale = (
                        not confirmed
                        or (
                            current - datetime.fromisoformat(confirmed)
                        ).total_seconds()
                        > 180 * 86400
                    )
                except ValueError:
                    stale = True
                if stale:
                    components["stale_proposed_penalty"] = -0.005
            handoff_markers = {"handoff", "checkpoint", "summary", "next-step"}
            if memory["type"] in {"task", "summary"} and (
                tags & handoff_markers
                or any(
                    marker in memory["title"].casefold()
                    for marker in handoff_markers
                )
            ):
                components["repetitive_handoff_penalty"] = -0.004
            components["total"] = sum(
                value for name, value in components.items() if name != "total"
            )
            item = dict(memory)
            item["decision_rerank"] = {
                "score": components["total"],
                "components": components,
                "roles": sorted(roles),
                "base_rank": base_rank,
            }
            ranked.append(item)
        return sorted(
            ranked,
            key=lambda item: (
                -item["decision_rerank"]["score"],
                item["decision_rerank"]["base_rank"],
                item["memory_id"],
            ),
        )

    def _expand_decision_seeds(
        self, project_id: str, context: dict[str, Any], scope_id: str | None
    ) -> None:
        """Add one-hop evidence without escaping context budgets."""
        seed_limit = 3
        candidate_limit = 50
        seeds = [
            item
            for item in context["items"]
            if item["type"] == "decision" and item["status"] == "active"
        ][:seed_limit]
        seed_ids = [item["memory_id"] for item in seeds]
        diagnostics = {
            "mode": "one_hop",
            "seed_limit": seed_limit,
            "candidate_limit": candidate_limit,
            "seed_memory_ids": seed_ids,
            "considered": 0,
            "added": 0,
            "item_limit": context["max_items"],
            "depth": 1,
            "truncated": False,
        }
        context["decision_expansion"] = diagnostics
        if not seed_ids:
            diagnostics["reason"] = "no_current_decision_seeds"
            return
        placeholders = ",".join("?" for _ in seed_ids)
        candidates: dict[str, dict[str, Any]] = {}

        def add_candidate(
            memory_id: str, priority: int, path: dict[str, Any]
        ) -> None:
            if memory_id in seed_ids:
                return
            candidate = candidates.setdefault(
                memory_id, {"priority": priority, "paths": []}
            )
            candidate["priority"] = min(candidate["priority"], priority)
            if path not in candidate["paths"]:
                candidate["paths"].append(path)

        relation_priority = {"supports": 0, "depends_on": 1, "supersedes": 2}
        for edge in self.store.conn.execute(
            f"""SELECT * FROM edges WHERE project_id=?
          AND relation IN ('supports','depends_on','supersedes')
          AND (from_memory_id IN ({placeholders})
            OR to_memory_id IN ({placeholders}))
          ORDER BY relation,created_at,id LIMIT ?""",
            (project_id, *seed_ids, *seed_ids, candidate_limit + 1),
        ):
            seed_id = (
                edge["from_memory_id"]
                if edge["from_memory_id"] in seed_ids
                else edge["to_memory_id"]
            )
            other_id = (
                edge["to_memory_id"]
                if seed_id == edge["from_memory_id"]
                else edge["from_memory_id"]
            )
            add_candidate(
                other_id,
                relation_priority[edge["relation"]],
                {
                    "kind": "memory_relation",
                    "relation": edge["relation"],
                    "seed_memory_id": seed_id,
                    "direction": (
                        "outgoing"
                        if seed_id == edge["from_memory_id"]
                        else "incoming"
                    ),
                },
            )
        for row in self.store.conn.execute(
            f"""SELECT DISTINCT sc.memory_id seed_memory_id,oc.memory_id,
          i.id investigation_id,l.relation
          FROM investigation_claims sc
          JOIN investigations i ON i.id=sc.investigation_id
          JOIN investigation_claims oc
            ON oc.investigation_id=sc.investigation_id
            AND oc.memory_id<>sc.memory_id
          LEFT JOIN investigation_claim_links l ON
            (l.from_claim_id=sc.id AND l.to_claim_id=oc.id)
            OR (l.to_claim_id=sc.id AND l.from_claim_id=oc.id)
          WHERE i.project_id=? AND sc.memory_id IN ({placeholders})
          ORDER BY i.id,oc.created_at,oc.id LIMIT ?""",
            (project_id, *seed_ids, candidate_limit + 1),
        ):
            add_candidate(
                row["memory_id"],
                3 if row["relation"] else 4,
                {
                    "kind": (
                        "investigation_relation"
                        if row["relation"]
                        else "shared_investigation"
                    ),
                    "relation": row["relation"],
                    "seed_memory_id": row["seed_memory_id"],
                    "investigation_id": row["investigation_id"],
                },
            )
        ordered = sorted(
            candidates.items(), key=lambda item: (item[1]["priority"], item[0])
        )
        if len(ordered) > candidate_limit:
            diagnostics["truncated"] = True
            ordered = ordered[:candidate_limit]
        diagnostics["considered"] = len(ordered)
        existing_ids = {item["memory_id"] for item in context["items"]}
        existing_by_id = {item["memory_id"]: item for item in context["items"]}
        for memory_id, expansion in ordered:
            if memory_id in existing_by_id:
                existing_by_id[memory_id]["decision_expansion"] = {
                    "depth": 1,
                    "already_retrieved": True,
                    "paths": expansion["paths"],
                }
        remaining_ids = [
            memory_id
            for memory_id, _ in ordered
            if memory_id not in existing_ids
        ]
        rows: dict[str, dict[str, Any]] = {}
        sources: dict[str, list[str]] = {
            memory_id: [] for memory_id in remaining_ids
        }
        if remaining_ids:
            remaining_placeholders = ",".join("?" for _ in remaining_ids)
            scope_clause = (
                ""
                if scope_id is None
                else " AND (scope_id=? OR scope_id IS NULL)"
            )
            timestamp = self.now()
            params: list[Any] = [*remaining_ids, timestamp, timestamp]
            if scope_id is not None:
                params.append(scope_id)
            rows = {
                row["id"]: dict(row)
                for row in self.store.conn.execute(
                    f"""SELECT * FROM memories
                WHERE id IN ({remaining_placeholders})
                AND (valid_from IS NULL OR valid_from<=?)
                AND (valid_until IS NULL OR valid_until>?)
                {scope_clause}""",
                    params,
                )
            }
            for source in self.store.conn.execute(
                f"""SELECT s.memory_id,s.event_id FROM memory_sources s
              WHERE s.memory_id IN ({remaining_placeholders})
              ORDER BY s.memory_id,s.event_id""",
                remaining_ids,
            ):
                sources[source["memory_id"]].append(source["event_id"])
        path_by_id = dict(ordered)
        for memory_id in remaining_ids:
            row = rows.get(memory_id)
            if not row:
                continue
            block = (
                f"[{row['status']}/{row['type']}]"
                f" {row['title']}\n{row['content']}\nsource_events:"
                f" {', '.join(sources[memory_id]) or 'none'}"
            )
            if (
                len(context["items"]) >= context["max_items"]
                or context["memory_used"] + len(block) + 2
                > context["memory_budget"]
            ):
                diagnostics["truncated"] = True
                continue
            context["items"].append(
                {
                    "memory_id": row["id"],
                    "project_id": row["project_id"],
                    "visibility": row["visibility"],
                    "confidence": row["confidence"],
                    "importance": row["importance"],
                    "status": row["status"],
                    "type": row["type"],
                    "title": row["title"],
                    "content": row["content"],
                    "source_event_ids": sources[memory_id],
                    "tags": json.loads(row["tags_json"]),
                    "observed_at": row["observed_at"],
                    "valid_from": row["valid_from"],
                    "valid_until": row["valid_until"],
                    "last_confirmed_at": row["last_confirmed_at"],
                    "truncated": False,
                    "decision_expansion": {
                        "depth": 1,
                        "paths": path_by_id[memory_id]["paths"],
                    },
                }
            )
            context["memory_used"] += len(block) + 2
            context["used"] += len(block) + 2
            diagnostics["added"] += 1
        if diagnostics["truncated"]:
            context["has_more"] = context["truncated"] = True

    def _decision_outcome_comparisons(
        self, retrieved_memory_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not retrieved_memory_ids:
            return []
        placeholders = ",".join("?" for _ in retrieved_memory_ids)
        rows = self.store.conn.execute(
            f"""SELECT d.memory_id decision_memory_id,d.expected_outcome,
          o.memory_id outcome_memory_id,o.outcome_effect,
          om.content observed_outcome,
          d.event_id decision_event_id,o.event_id outcome_event_id
          FROM investigation_claim_links l
          JOIN investigation_claims d
            ON d.id=l.from_claim_id AND d.role='decision'
          JOIN investigation_claims o
            ON o.id=l.to_claim_id AND o.role='outcome'
          JOIN memories om ON om.id=o.memory_id
          WHERE (d.memory_id IN ({placeholders})
            OR o.memory_id IN ({placeholders}))
          ORDER BY o.created_at,o.id""",
            (*retrieved_memory_ids, *retrieved_memory_ids),
        )
        return [
            {
                "expected_outcome": row["expected_outcome"],
                "observed_outcome": row["observed_outcome"],
                "effect": row["outcome_effect"],
                "decision_citation": {
                    "memory_id": row["decision_memory_id"],
                    "source_event_ids": [row["decision_event_id"]],
                },
                "outcome_citation": {
                    "memory_id": row["outcome_memory_id"],
                    "source_event_ids": [row["outcome_event_id"]],
                },
            }
            for row in rows
        ]
