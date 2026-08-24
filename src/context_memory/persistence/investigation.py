"""Research investigation persistence and read-model assembly."""

import hashlib
import json
import sqlite3
from typing import Any, Callable

from ..contracts import (
    INVESTIGATION_ROLES,
    MEMORY_TYPES,
    OUTCOME_EFFECTS,
    SOURCE_REINSPECTION_REASONS,
)
from ..serialization import canonical

TYPES = MEMORY_TYPES


class InvestigationRepository:
    """Own investigation provenance read queries."""

    def __init__(
        self,
        store: Any,
        now: Callable[[], str],
        uid: Callable[[], str],
    ):
        self.store = store
        self.connection: sqlite3.Connection = store.conn
        self.now = now
        self.uid = uid

    def get_investigation(
        self, investigation_id: str, source_analysis_id: str | None = None
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM investigations WHERE id=?", (investigation_id,)
        ).fetchone()
        if not row:
            return None
        investigation = dict(row)
        investigation["constraints"] = json.loads(
            investigation.pop("constraints_json")
        )
        condition, arguments = (
            (" AND id=?", (investigation_id, source_analysis_id))
            if source_analysis_id
            else ("", (investigation_id,))
        )
        analyses = []
        for analysis_row in self.connection.execute(
            "SELECT * FROM source_analyses WHERE investigation_id=?"
            + condition
            + " ORDER BY created_at,id",
            arguments,
        ):
            analysis = dict(analysis_row)
            analysis["reinspection_requests"] = [
                dict(item)
                for item in self.connection.execute(
                    "SELECT * FROM source_reinspection_requests WHERE"
                    " source_analysis_id=? ORDER BY requested_at,id",
                    (analysis["id"],),
                )
            ]
            analysis["claims"] = []
            for claim_row in self.connection.execute(
                "SELECT * FROM investigation_claims WHERE"
                " source_analysis_id=? ORDER BY ordinal",
                (analysis["id"],),
            ):
                claim = dict(claim_row)
                claim["evidence"] = [
                    dict(link)
                    for link in self.connection.execute(
                        """SELECT l.relation,c.source_analysis_id,
                    c.claim_key,c.event_id,c.memory_id
                    FROM investigation_claim_links l
                    JOIN investigation_claims c ON c.id=l.from_claim_id
                    WHERE l.to_claim_id=? ORDER BY c.claim_key""",
                        (claim["id"],),
                    )
                ]
                analysis["claims"].append(claim)
            analyses.append(analysis)
        return {
            "contract_version": "research-provenance/v1",
            "investigation": investigation,
            "source_analyses": analyses,
            "idempotent": source_analysis_id is not None,
        }

    def create_investigation(
        self,
        project_id: str,
        question: str,
        reason: str,
        decision_to_inform: str,
        constraints: list[str] | None = None,
        initiator: str = "unknown",
        scope_id: str | None = None,
        investigation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create durable investigation intent without browsing."""
        request = locals().copy()
        request.pop("self")
        request.pop("idempotency_key")
        if hit := self.store._idem(
            "create_investigation", idempotency_key, request
        ):
            return hit
        if not all(
            value.strip()
            for value in (question, reason, decision_to_inform, initiator)
        ):
            raise ValueError(
                "question, reason, decision_to_inform, and initiator cannot be"
                " empty"
            )
        if not self.store._row(
            "SELECT id FROM projects WHERE id=?", (project_id,)
        ):
            raise KeyError("project not found")
        if scope_id and not self.store.wiki.scope_belongs_to_project(
            scope_id, project_id
        ):
            raise ValueError("scope must belong to project")
        item = {
            "id": investigation_id or self.uid(),
            "project_id": project_id,
            "scope_id": scope_id,
            "question": question.strip(),
            "reason": reason.strip(),
            "decision_to_inform": decision_to_inform.strip(),
            "constraints_json": canonical(constraints or []),
            "initiator": initiator.strip(),
            "status": "open",
            "started_at": self.now(),
            "completed_at": None,
        }
        with self.store.tx() as cx:
            cx.execute(
                """INSERT INTO investigations(id,project_id,scope_id,
              question,reason,decision_to_inform,constraints_json,initiator,
              status,started_at,completed_at)
              VALUES(:id,:project_id,:scope_id,:question,:reason,
              :decision_to_inform,:constraints_json,
              :initiator,:status,:started_at,:completed_at)""",
                item,
            )
            self.store._audit(
                cx, project_id, "investigation", item["id"], "created", item
            )
            result = {
                **item,
                "constraints": json.loads(item["constraints_json"]),
            }
            result.pop("constraints_json")
            self.store._save_idem(
                cx, "create_investigation", idempotency_key, request, result
            )
        return result

    def record_source_analysis(
        self,
        investigation_id: str,
        source: dict[str, Any],
        claims: list[dict[str, Any]],
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Record a source version and consequential typed claims."""
        request = {
            "investigation_id": investigation_id,
            "source": source,
            "claims": claims,
            "session_id": session_id,
        }
        if hit := self.store._idem(
            "record_source_analysis", idempotency_key, request
        ):
            return hit
        investigation = self.store._row(
            "SELECT * FROM investigations WHERE id=?", (investigation_id,)
        )
        if not investigation:
            raise KeyError("investigation not found")
        if investigation["status"] != "open":
            raise ValueError("investigation is completed")
        required = (
            "source_type",
            "stable_source_id",
            "access_reason",
            "analysis_method",
        )
        if any(
            not isinstance(source.get(key), str) or not source[key].strip()
            for key in required
        ):
            raise ValueError(
                f"source requires non-empty {', '.join(required)}"
            )
        version, fingerprint = (
            source.get("source_version"),
            source.get("content_fingerprint"),
        )
        if not (isinstance(version, str) and version.strip()) and not (
            isinstance(fingerprint, str) and fingerprint.strip()
        ):
            raise ValueError(
                "source_version or content_fingerprint is required for change"
                " detection"
            )
        if not claims:
            raise ValueError("claims must be non-empty")
        keys = [claim.get("key") for claim in claims]
        if any(
            not isinstance(key, str) or not key.strip() for key in keys
        ) or len(set(keys)) != len(keys):
            raise ValueError("claim keys must be unique non-empty strings")
        version_key = (
            version or ""
        ).strip() or f"fingerprint:{(fingerprint or '').strip()}"
        identity = canonical(
            [
                source["source_type"].strip(),
                source["stable_source_id"].strip(),
                version_key,
            ]
        )
        identity_key = hashlib.sha256(identity.encode()).hexdigest()
        existing = self.store._row(
            "SELECT id FROM source_analyses WHERE investigation_id=? AND"
            " identity_key=?",
            (investigation_id, identity_key),
        )
        if existing:
            chain = self.store.get_investigation(
                investigation_id, existing["id"]
            )["source_analyses"][0]
            repeated_claims = []
            for claim in chain["claims"]:
                memory = self.store._row(
                    "SELECT status FROM memories WHERE id=?",
                    (claim["memory_id"],),
                )
                repeated_claims.append(
                    {
                        **claim,
                        "evidence_claim_keys": [
                            item["claim_key"] for item in claim["evidence"]
                        ],
                        "memory_status": memory["status"] if memory else None,
                    }
                )
            return {
                "contract_version": "research-provenance/v1",
                "investigation_id": investigation_id,
                "source_analysis_id": chain["id"],
                "identity_key": chain["identity_key"],
                "claims": repeated_claims,
                "idempotent": True,
            }
        ts, analysis_id = self.now(), self.uid()
        source_item = {
            "id": analysis_id,
            "investigation_id": investigation_id,
            "source_type": source["source_type"].strip(),
            "stable_source_id": source["stable_source_id"].strip(),
            "canonical_uri": source.get("canonical_uri"),
            "source_version": version,
            "source_updated_at": source.get("source_updated_at"),
            "retrieved_at": source.get("retrieved_at") or ts,
            "section_anchor": source.get("section_anchor"),
            "access_reason": source["access_reason"].strip(),
            "analysis_method": source["analysis_method"].strip(),
            "content_fingerprint": fingerprint,
            "identity_key": identity_key,
            "created_at": ts,
        }
        result_claims = []
        with self.store.tx() as cx:
            cx.execute(
                """INSERT INTO source_analyses VALUES(:id,
              :investigation_id,:source_type,:stable_source_id,
              :canonical_uri,:source_version,:source_updated_at,
              :retrieved_at,:section_anchor,:access_reason,
              :analysis_method,:content_fingerprint,:identity_key,:created_at)""",
                source_item,
            )
            created: dict[str, dict[str, Any]] = {}
            for ordinal, claim in enumerate(claims):
                role, content = claim.get("role"), claim.get("content")
                if (
                    role not in INVESTIGATION_ROLES
                    or not isinstance(content, str)
                    or not content.strip()
                ):
                    raise ValueError(
                        "each claim requires a valid role and non-empty"
                        " content"
                    )
                refs = claim.get("evidence_claim_keys", [])
                external_refs = claim.get("evidence_claim_refs", [])
                if not isinstance(refs, list) or any(
                    ref not in created for ref in refs
                ):
                    raise ValueError(
                        "evidence_claim_keys must reference earlier claims in"
                        " this analysis"
                    )
                if not isinstance(external_refs, list):
                    raise ValueError("evidence_claim_refs must be a list")
                resolved_external = []
                for ref in external_refs:
                    if (
                        not isinstance(ref, dict)
                        or not isinstance(ref.get("source_analysis_id"), str)
                        or not isinstance(ref.get("claim_key"), str)
                    ):
                        raise ValueError(
                            "evidence_claim_refs require source_analysis_id"
                            " and claim_key"
                        )
                    prior = cx.execute(
                        """SELECT c.* FROM investigation_claims c
                      WHERE c.investigation_id=?
                        AND c.source_analysis_id=? AND c.claim_key=?""",
                        (
                            investigation_id,
                            ref["source_analysis_id"],
                            ref["claim_key"],
                        ),
                    ).fetchone()
                    if not prior:
                        raise ValueError(
                            "evidence_claim_refs must reference an existing"
                            " claim in this investigation"
                        )
                    resolved_external.append(dict(prior))
                if role in {
                    "inference",
                    "action",
                    "decision",
                    "rationale",
                    "outcome",
                } and not (refs or resolved_external):
                    raise ValueError(
                        f"{role} claims require evidence claim references"
                    )
                expected_outcome, outcome_effect = (
                    claim.get("expected_outcome"),
                    claim.get("outcome_effect"),
                )
                if expected_outcome is not None and (
                    role != "decision"
                    or not isinstance(expected_outcome, str)
                    or not expected_outcome.strip()
                ):
                    raise ValueError(
                        "expected_outcome is only valid as non-empty text on"
                        " decision claims"
                    )
                if outcome_effect is not None and (
                    role != "outcome" or outcome_effect not in OUTCOME_EFFECTS
                ):
                    raise ValueError(
                        "outcome_effect is only valid on outcome claims"
                    )
                event_id, claim_id = self.uid(), self.uid()
                cursor = cx.execute(
                    "UPDATE project_event_cursors SET next_seq=next_seq+1"
                    " WHERE project_id=? RETURNING next_seq-1",
                    (investigation["project_id"],),
                ).fetchone()
                evidence_events = [
                    created[key]["event_id"] for key in refs
                ] + [item["event_id"] for item in resolved_external]
                metadata = {
                    "investigation_id": investigation_id,
                    "source_analysis_id": analysis_id,
                    "claim_key": claim["key"],
                    "role": role,
                    "evidence_event_ids": evidence_events,
                    "expected_outcome": expected_outcome,
                    "outcome_effect": outcome_effect,
                }
                event = {
                    "id": event_id,
                    "project_id": investigation["project_id"],
                    "scope_id": investigation["scope_id"],
                    "session_id": session_id,
                    "kind": (
                        "fact"
                        if role in {"evidence", "rationale", "outcome"}
                        else role
                    ),
                    "content": content.strip(),
                    "source_uri": source_item["canonical_uri"],
                    "metadata_json": canonical(metadata),
                    "content_hash": (
                        hashlib.sha256(content.strip().encode()).hexdigest()
                    ),
                    "created_at": ts,
                    "event_seq": cursor[0],
                }
                cx.execute(
                    """INSERT INTO events(id,project_id,scope_id,session_id,
                  kind,content,source_uri,metadata_json,content_hash,
                  created_at,event_seq) VALUES(:id,:project_id,:scope_id,
                  :session_id,:kind,:content,
                  :source_uri,:metadata_json,:content_hash,:created_at,:event_seq)""",
                    event,
                )
                self.store._audit(
                    cx,
                    investigation["project_id"],
                    "event",
                    event_id,
                    "recorded",
                    event,
                )
                status = claim.get("memory_status", "proposed")
                if status not in {"proposed", "active"} or (
                    role == "inference" and status != "proposed"
                ):
                    raise ValueError(
                        "memory_status must be proposed or active; inference"
                        " must remain proposed"
                    )
                memory_id = self.uid()
                memory_type = claim.get("memory_type") or (
                    {
                        "decision": "decision",
                        "action": "task",
                        "rationale": "fact",
                        "outcome": "fact",
                    }.get(role, "fact")
                )
                if memory_type not in TYPES:
                    raise ValueError("invalid claim memory_type")
                memory = {
                    "id": memory_id,
                    "project_id": investigation["project_id"],
                    "scope_id": investigation["scope_id"],
                    "type": memory_type,
                    "status": status,
                    "title": claim.get("title") or content.strip()[:120],
                    "content": content.strip(),
                    "confidence": float(claim.get("confidence", 0.6)),
                    "importance": float(claim.get("importance", 0.5)),
                    "valid_from": None,
                    "valid_until": None,
                    "tags_json": canonical(["investigation", role]),
                    "created_at": ts,
                    "updated_at": ts,
                    "observed_at": ts,
                    "last_confirmed_at": ts if status == "active" else None,
                    "visibility": "project",
                }
                if not (
                    0 <= memory["confidence"] <= 1
                    and 0 <= memory["importance"] <= 1
                ):
                    raise ValueError("confidence and importance must be 0..1")
                cx.execute(
                    """INSERT INTO memories(id,project_id,scope_id,type,
                  status,title,content,confidence,importance,valid_from,
                  valid_until,tags_json,created_at,updated_at,observed_at,
                  last_confirmed_at,visibility) VALUES(:id,:project_id,
                  :scope_id,:type,:status,:title,:content,:confidence,
                  :importance,:valid_from,:valid_until,:tags_json,
                  :created_at,:updated_at,:observed_at,:last_confirmed_at,
                  :visibility)""",
                    memory,
                )
                cx.execute(
                    "INSERT INTO memory_sources VALUES(?,?,?,?)",
                    (memory_id, event_id, "investigation claim", ts),
                )
                self.store._index_embedding(cx, memory)
                self.store._audit(
                    cx,
                    investigation["project_id"],
                    "memory",
                    memory_id,
                    "created",
                    memory,
                )
                claim_item = {
                    "id": claim_id,
                    "investigation_id": investigation_id,
                    "source_analysis_id": analysis_id,
                    "claim_key": claim["key"],
                    "ordinal": ordinal,
                    "role": role,
                    "event_id": event_id,
                    "memory_id": memory_id,
                    "created_at": ts,
                    "expected_outcome": (
                        expected_outcome.strip() if expected_outcome else None
                    ),
                    "outcome_effect": outcome_effect,
                }
                cx.execute(
                    """INSERT INTO investigation_claims(id,
                  investigation_id,source_analysis_id,claim_key,ordinal,role,
                  event_id,memory_id,created_at,expected_outcome,
                  outcome_effect) VALUES(:id,:investigation_id,
                  :source_analysis_id,:claim_key,:ordinal,:role,:event_id,
                  :memory_id,
                  :created_at,:expected_outcome,:outcome_effect)""",
                    claim_item,
                )
                relation = (
                    "derived_from"
                    if role == "inference"
                    else (
                        "informed"
                        if role in {"action", "decision"}
                        else "supports"
                    )
                )
                for ref in refs:
                    cx.execute(
                        "INSERT INTO investigation_claim_links"
                        " VALUES(?,?,?,?)",
                        (created[ref]["id"], claim_id, relation, ts),
                    )
                for prior in resolved_external:
                    cx.execute(
                        "INSERT INTO investigation_claim_links"
                        " VALUES(?,?,?,?)",
                        (prior["id"], claim_id, relation, ts),
                    )
                created[claim["key"]] = claim_item
                result_claims.append(
                    {
                        **claim_item,
                        "evidence_claim_keys": refs,
                        "evidence_claim_refs": external_refs,
                        "memory_status": status,
                    }
                )
            response = {
                "contract_version": "research-provenance/v1",
                "investigation_id": investigation_id,
                "source_analysis_id": analysis_id,
                "identity_key": identity_key,
                "claims": result_claims,
                "idempotent": False,
            }
            self.store._audit(
                cx,
                investigation["project_id"],
                "source_analysis",
                analysis_id,
                "recorded",
                response,
            )
            self.store._save_idem(
                cx,
                "record_source_analysis",
                idempotency_key,
                request,
                response,
            )
        return response

    def request_source_reinspection(
        self,
        source_analysis_id: str,
        reason: str,
        details: str | None = None,
        known_source_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Request client reinspection of an external source."""
        request = {
            "source_analysis_id": source_analysis_id,
            "reason": reason,
            "details": details,
            "known_source_version": known_source_version,
        }
        if hit := self.store._idem(
            "request_source_reinspection", idempotency_key, request
        ):
            return hit
        source = self.store._row(
            """SELECT s.*,i.project_id FROM source_analyses s
          JOIN investigations i ON i.id=s.investigation_id WHERE s.id=?""",
            (source_analysis_id,),
        )
        if not source:
            raise KeyError("source analysis not found")
        if reason not in SOURCE_REINSPECTION_REASONS:
            raise ValueError(
                "reason must be old, unavailable, or newer_version_known"
            )
        normalized_details = (
            details.strip()
            if isinstance(details, str) and details.strip()
            else None
        )
        normalized_version = (
            known_source_version.strip()
            if isinstance(known_source_version, str)
            and known_source_version.strip()
            else None
        )
        if reason == "newer_version_known" and not normalized_version:
            raise ValueError(
                "known_source_version is required when a newer version is"
                " known"
            )
        if (
            reason != "newer_version_known"
            and known_source_version is not None
        ):
            raise ValueError(
                "known_source_version is only valid when a newer version is"
                " known"
            )
        item = {
            "id": self.uid(),
            "source_analysis_id": source_analysis_id,
            "reason": reason,
            "details": normalized_details,
            "known_source_version": normalized_version,
            "requested_at": self.now(),
        }
        response = {
            "contract_version": "source-reinspection/v1",
            **item,
            "source": {
                "source_type": source["source_type"],
                "stable_source_id": source["stable_source_id"],
                "canonical_uri": source["canonical_uri"],
                "inspected_source_version": source["source_version"],
            },
            "execution": {
                "owner": "client",
                "core_fetch_performed": False,
                "state": "requested",
            },
        }
        with self.store.tx() as cx:
            cx.execute(
                """INSERT INTO source_reinspection_requests
              (id,source_analysis_id,reason,details,known_source_version,requested_at)
              VALUES(:id,:source_analysis_id,:reason,:details,:known_source_version,:requested_at)""",
                item,
            )
            self.store._audit(
                cx,
                source["project_id"],
                "source_reinspection_request",
                item["id"],
                "requested",
                response,
            )
            self.store._save_idem(
                cx,
                "request_source_reinspection",
                idempotency_key,
                request,
                response,
            )
        return response

    def complete_investigation(self, investigation_id: str) -> dict[str, Any]:
        investigation = self.store._row(
            "SELECT * FROM investigations WHERE id=?", (investigation_id,)
        )
        if not investigation:
            raise KeyError("investigation not found")
        if investigation["status"] == "completed":
            return self.store.get_investigation(investigation_id)[
                "investigation"
            ]
        completed_at = self.now()
        with self.store.tx() as cx:
            cx.execute(
                "UPDATE investigations SET status='completed',completed_at=?"
                " WHERE id=?",
                (completed_at, investigation_id),
            )
            result = {
                **investigation,
                "status": "completed",
                "completed_at": completed_at,
            }
            self.store._audit(
                cx,
                investigation["project_id"],
                "investigation",
                investigation_id,
                "completed",
                result,
            )
        result["constraints"] = json.loads(result.pop("constraints_json"))
        return result
