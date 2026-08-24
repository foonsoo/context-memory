"""Portable project export and import persistence."""

import sqlite3
from typing import Any

EXPORT_QUERIES = (
    (
        "scope",
        "SELECT * FROM scopes WHERE project_id=? ORDER BY created_at,id",
    ),
    (
        "session",
        "SELECT * FROM sessions WHERE project_id=? ORDER BY started_at,id",
    ),
    ("event", "SELECT * FROM events WHERE project_id=? ORDER BY event_seq"),
    (
        "memory",
        "SELECT * FROM memories WHERE project_id=? ORDER BY created_at,id",
    ),
    (
        "memory_source",
        "SELECT ms.* FROM memory_sources ms JOIN memories m ON"
        " m.id=ms.memory_id WHERE m.project_id=? ORDER BY"
        " ms.created_at,ms.memory_id,ms.event_id",
    ),
    (
        "investigation",
        "SELECT * FROM investigations WHERE project_id=? ORDER BY"
        " started_at,id",
    ),
    (
        "source_analysis",
        "SELECT s.* FROM source_analyses s JOIN investigations i"
        " ON i.id=s.investigation_id WHERE i.project_id=? ORDER BY"
        " s.created_at,s.id",
    ),
    (
        "source_reinspection_request",
        "SELECT r.* FROM source_reinspection_requests r JOIN"
        " source_analyses s ON s.id=r.source_analysis_id JOIN"
        " investigations i ON i.id=s.investigation_id WHERE"
        " i.project_id=? ORDER BY r.requested_at,r.id",
    ),
    (
        "investigation_claim",
        "SELECT c.* FROM investigation_claims c JOIN investigations i"
        " ON i.id=c.investigation_id WHERE i.project_id=? ORDER BY"
        " c.created_at,c.source_analysis_id,c.ordinal",
    ),
    (
        "investigation_claim_link",
        "SELECT l.* FROM investigation_claim_links l JOIN"
        " investigation_claims c ON c.id=l.from_claim_id JOIN"
        " investigations i ON i.id=c.investigation_id WHERE"
        " i.project_id=? ORDER BY"
        " l.created_at,l.from_claim_id,l.to_claim_id",
    ),
    (
        "wiki_page",
        "SELECT * FROM wiki_pages WHERE project_id=? ORDER BY created_at,id",
    ),
    (
        "wiki_revision",
        "SELECT r.* FROM wiki_revisions r JOIN wiki_pages p ON"
        " p.id=r.page_id WHERE p.project_id=? ORDER BY r.created_at,r.id",
    ),
    (
        "wiki_revision_citation",
        "SELECT c.* FROM wiki_revision_citations c JOIN wiki_revisions r"
        " ON r.id=c.revision_id JOIN wiki_pages p ON p.id=r.page_id"
        " WHERE p.project_id=? ORDER BY c.revision_id,c.section_name,"
        " c.ordinal,c.memory_id,c.event_id",
    ),
    (
        "memory_usage",
        "SELECT u.* FROM memory_usage u JOIN memories m ON"
        " m.id=u.memory_id WHERE m.project_id=? ORDER BY u.memory_id",
    ),
    (
        "review_conflict",
        "SELECT c.* FROM review_conflicts c JOIN memories m ON"
        " m.id=c.candidate_memory_id WHERE m.project_id=? ORDER BY"
        " c.created_at,c.candidate_memory_id,c.existing_memory_id",
    ),
    ("edge", "SELECT * FROM edges WHERE project_id=? ORDER BY created_at,id"),
    (
        "search_alias",
        "SELECT * FROM search_aliases WHERE project_id=? ORDER BY term",
    ),
    (
        "project_alias",
        "SELECT * FROM project_aliases WHERE project_id=? ORDER BY"
        " kind,normalized",
    ),
    (
        "event_receipt",
        "SELECT * FROM event_receipts WHERE project_id=? ORDER BY"
        " consumer_id,scope_key,kinds_json",
    ),
    ("policy", "SELECT * FROM project_policies WHERE project_id=?"),
    (
        "audit_checkpoint",
        "SELECT * FROM audit_checkpoints WHERE project_id=? ORDER BY"
        " through_seq",
    ),
    ("audit", "SELECT * FROM audit_log WHERE project_id=? ORDER BY seq"),
)


class TransferRepository:
    """Own portable project transfer persistence behind the facade."""

    def __init__(self, store: Any):
        self.store = store
        self.connection: sqlite3.Connection = store.conn

    def export_project(self, project_id: str) -> list[dict[str, Any]]:
        """Return a portable snapshot without SQLite internals."""
        row = self.connection.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if not row:
            raise KeyError("project not found")
        records: list[dict[str, Any]] = [
            {"record_type": "project", "data": dict(row)}
        ]
        for record_type, sql in EXPORT_QUERIES:
            records.extend(
                {"record_type": record_type, "data": dict(item)}
                for item in self.connection.execute(sql, (project_id,))
            )
        return records

    def import_project(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Restore a project without overwriting project IDs."""
        if not records or records[0].get("record_type") != "project":
            raise ValueError("export must begin with a project record")
        allowed = {
            "project",
            "scope",
            "session",
            "event",
            "memory",
            "memory_source",
            "investigation",
            "source_analysis",
            "source_reinspection_request",
            "investigation_claim",
            "investigation_claim_link",
            "wiki_page",
            "wiki_revision",
            "wiki_revision_citation",
            "memory_usage",
            "review_conflict",
            "edge",
            "search_alias",
            "project_alias",
            "event_receipt",
            "policy",
            "audit_checkpoint",
            "audit",
        }
        if any(
            record.get("record_type") not in allowed
            or not isinstance(record.get("data"), dict)
            for record in records
        ):
            raise ValueError("invalid export record")
        project = records[0]["data"]
        if self.store._row(
            "SELECT id FROM projects WHERE id=? OR slug=?",
            (project.get("id"), project.get("slug")),
        ):
            raise ValueError("project id or slug already exists")
        columns = {
            "project": (
                "projects",
                ["id", "slug", "name", "description", "created_at"],
            ),
            "scope": (
                "scopes",
                ["id", "project_id", "name", "path", "created_at"],
            ),
            "session": (
                "sessions",
                [
                    "id",
                    "project_id",
                    "scope_id",
                    "client",
                    "external_id",
                    "started_at",
                    "ended_at",
                    "metadata_json",
                ],
            ),
            "event": (
                "events",
                [
                    "id",
                    "project_id",
                    "scope_id",
                    "session_id",
                    "kind",
                    "content",
                    "source_uri",
                    "metadata_json",
                    "content_hash",
                    "created_at",
                    "event_seq",
                ],
            ),
            "memory": (
                "memories",
                [
                    "id",
                    "project_id",
                    "scope_id",
                    "type",
                    "status",
                    "title",
                    "content",
                    "confidence",
                    "importance",
                    "valid_from",
                    "valid_until",
                    "tags_json",
                    "created_at",
                    "updated_at",
                    "observed_at",
                    "last_confirmed_at",
                    "visibility",
                ],
            ),
            "memory_source": (
                "memory_sources",
                ["memory_id", "event_id", "note", "created_at"],
            ),
            "investigation": (
                "investigations",
                [
                    "id",
                    "project_id",
                    "scope_id",
                    "question",
                    "reason",
                    "decision_to_inform",
                    "constraints_json",
                    "initiator",
                    "status",
                    "started_at",
                    "completed_at",
                ],
            ),
            "source_analysis": (
                "source_analyses",
                [
                    "id",
                    "investigation_id",
                    "source_type",
                    "stable_source_id",
                    "canonical_uri",
                    "source_version",
                    "source_updated_at",
                    "retrieved_at",
                    "section_anchor",
                    "access_reason",
                    "analysis_method",
                    "content_fingerprint",
                    "identity_key",
                    "created_at",
                ],
            ),
            "source_reinspection_request": (
                "source_reinspection_requests",
                [
                    "id",
                    "source_analysis_id",
                    "reason",
                    "details",
                    "known_source_version",
                    "requested_at",
                ],
            ),
            "investigation_claim": (
                "investigation_claims",
                [
                    "id",
                    "investigation_id",
                    "source_analysis_id",
                    "claim_key",
                    "ordinal",
                    "role",
                    "event_id",
                    "memory_id",
                    "created_at",
                    "expected_outcome",
                    "outcome_effect",
                ],
            ),
            "investigation_claim_link": (
                "investigation_claim_links",
                ["from_claim_id", "to_claim_id", "relation", "created_at"],
            ),
            "wiki_page": (
                "wiki_pages",
                [
                    "id",
                    "project_id",
                    "scope_id",
                    "topic",
                    "title",
                    "manual_notes",
                    "created_at",
                    "updated_at",
                ],
            ),
            "wiki_revision": (
                "wiki_revisions",
                [
                    "id",
                    "page_id",
                    "revision_no",
                    "status",
                    "question",
                    "sections_json",
                    "generation_json",
                    "created_at",
                    "published_at",
                    "stale_reason",
                ],
            ),
            "wiki_revision_citation": (
                "wiki_revision_citations",
                [
                    "revision_id",
                    "section_name",
                    "ordinal",
                    "memory_id",
                    "event_id",
                ],
            ),
            "memory_usage": (
                "memory_usage",
                [
                    "memory_id",
                    "retrieved_count",
                    "used_count",
                    "helpful_count",
                    "incorrect_count",
                    "last_retrieved_at",
                    "last_used_at",
                    "updated_at",
                ],
            ),
            "review_conflict": (
                "review_conflicts",
                [
                    "candidate_memory_id",
                    "existing_memory_id",
                    "similarity",
                    "reason",
                    "created_at",
                ],
            ),
            "edge": (
                "edges",
                [
                    "id",
                    "project_id",
                    "from_memory_id",
                    "to_memory_id",
                    "relation",
                    "note",
                    "created_at",
                ],
            ),
            "search_alias": (
                "search_aliases",
                [
                    "project_id",
                    "term",
                    "aliases_json",
                    "created_at",
                    "updated_at",
                ],
            ),
            "project_alias": (
                "project_aliases",
                [
                    "project_id",
                    "kind",
                    "value",
                    "normalized",
                    "created_at",
                    "updated_at",
                ],
            ),
            "event_receipt": (
                "event_receipts",
                [
                    "project_id",
                    "consumer_id",
                    "scope_key",
                    "kinds_json",
                    "acknowledged_cursor",
                    "delivered_cursor",
                    "created_at",
                    "updated_at",
                ],
            ),
            "audit_checkpoint": (
                "audit_checkpoints",
                [
                    "id",
                    "project_id",
                    "from_seq",
                    "through_seq",
                    "entry_count",
                    "previous_digest",
                    "digest",
                    "created_at",
                ],
            ),
        }
        counts: dict[str, int] = {}
        imported_event_seq = 0
        with self.store.tx() as cx:
            for record in records:
                kind, data = record["record_type"], dict(record["data"])
                if kind == "event":
                    imported_event_seq += 1
                    data["event_seq"] = (
                        data.get("event_seq") or imported_event_seq
                    )
                if kind == "memory":
                    data.setdefault("visibility", "project")
                if kind == "investigation_claim":
                    data.setdefault("expected_outcome", None)
                    data.setdefault("outcome_effect", None)
                if kind == "audit":
                    names = [
                        "project_id",
                        "entity_type",
                        "entity_id",
                        "action",
                        "snapshot_json",
                        "created_at",
                    ]
                    columns_sql = ",".join(names)
                    placeholders = ",".join("?" for _ in names)
                    cx.execute(
                        f"INSERT INTO audit_log({columns_sql}) "
                        f"VALUES({placeholders})",
                        tuple(data[name] for name in names),
                    )
                elif kind == "policy":
                    defaults = {
                        "checkpoint_soft_usage": 0.60,
                        "checkpoint_hard_usage": 0.75,
                        "checkpoint_elapsed_seconds": 1800,
                        "checkpoint_event_count": 25,
                        "checkpoint_max_age_seconds": 3600,
                        "checkpoint_cooldown_seconds": 300,
                        "checkpoint_hysteresis": 0.05,
                        "maintenance_interval_seconds": 0,
                        "message_ttl_seconds": 0,
                    }
                    for name, value in defaults.items():
                        data.setdefault(name, value)
                    names = [
                        "max_context_chars",
                        "max_context_items",
                        "audit_keep_entries",
                        "terminal_memory_days",
                        "checkpoint_soft_usage",
                        "checkpoint_hard_usage",
                        "checkpoint_elapsed_seconds",
                        "checkpoint_event_count",
                        "checkpoint_max_age_seconds",
                        "checkpoint_cooldown_seconds",
                        "checkpoint_hysteresis",
                        "maintenance_interval_seconds",
                        "message_ttl_seconds",
                        "updated_at",
                        "project_id",
                    ]
                    cx.execute(
                        """UPDATE project_policies SET max_context_chars=?,
                      max_context_items=?,audit_keep_entries=?,
                      terminal_memory_days=?,checkpoint_soft_usage=?,
                      checkpoint_hard_usage=?,checkpoint_elapsed_seconds=?,
                      checkpoint_event_count=?,checkpoint_max_age_seconds=?,
                      checkpoint_cooldown_seconds=?,checkpoint_hysteresis=?,
                      maintenance_interval_seconds=?,message_ttl_seconds=?,
                      updated_at=? WHERE project_id=?""",
                        tuple(data[name] for name in names),
                    )
                else:
                    table, names = columns[kind]
                    cx.execute(
                        f"INSERT INTO {table}({','.join(names)})"
                        f" VALUES({','.join('?' for _ in names)})",
                        tuple(data[name] for name in names),
                    )
                counts[kind] = counts.get(kind, 0) + 1
            cx.execute(
                "UPDATE project_event_cursors SET next_seq=? WHERE"
                " project_id=?",
                (imported_event_seq + 1, project["id"]),
            )
        return {
            "project_id": project["id"],
            "slug": project["slug"],
            "records": len(records),
            "counts": counts,
        }
