"""Decision Wiki persistence queries."""

import json
import sqlite3
from datetime import datetime
from typing import Any, Callable

from ..serialization import canonical
from ..wiki_lint import (
    embedded_citation_findings,
    embedded_memory_ids,
    finish_lint_result,
    memory_citation_findings,
    source_reinspection_finding,
)
from ..wiki_rendering import render_wiki_markdown
from .primitives import row_dict, row_exists


class WikiRepository:
    """Own page and revision identity persistence."""

    def __init__(
        self,
        store: Any,
        now: Callable[[], str],
        uid: Callable[[], str],
        current_datetime: Callable[[], datetime],
    ):
        self.store = store
        self.connection: sqlite3.Connection = store.conn
        self.now = now
        self.uid = uid
        self.current_datetime = current_datetime

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM wiki_pages WHERE id=?", (page_id,)
        ).fetchone()
        return row_dict(row)

    def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM wiki_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        return row_dict(row)

    def scope_belongs_to_project(self, scope_id: str, project_id: str) -> bool:
        return row_exists(
            self.connection,
            "SELECT 1 FROM scopes WHERE id=? AND project_id=?",
            (scope_id, project_id),
        )

    @staticmethod
    def insert_page(
        connection: sqlite3.Connection, item: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO wiki_pages"
            " VALUES(:id,:project_id,:scope_id,:topic,:title,"
            ":manual_notes,:created_at,:updated_at)",
            item,
        )

    @staticmethod
    def update_notes(
        connection: sqlite3.Connection,
        page_id: str,
        manual_notes: str,
        updated_at: str,
    ) -> None:
        connection.execute(
            "UPDATE wiki_pages SET manual_notes=?,updated_at=? WHERE id=?",
            (manual_notes, updated_at, page_id),
        )

    def create_wiki_page(
        self,
        project_id: str,
        topic: str,
        title: str,
        scope_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request = {
            "project_id": project_id,
            "topic": topic,
            "title": title,
            "scope_id": scope_id,
        }
        if hit := self.store._idem(
            "create_wiki_page", idempotency_key, request
        ):
            return hit
        if not topic.strip() or not title.strip():
            raise ValueError("topic and title cannot be empty")
        if not self.store._row(
            "SELECT id FROM projects WHERE id=?", (project_id,)
        ):
            raise KeyError("project not found")
        if scope_id and not self.store._row(
            "SELECT id FROM scopes WHERE id=? AND project_id=?",
            (scope_id, project_id),
        ):
            raise ValueError("scope must belong to project")
        ts = self.now()
        item = {
            "id": self.uid(),
            "project_id": project_id,
            "scope_id": scope_id,
            "topic": topic.strip(),
            "title": title.strip(),
            "manual_notes": "",
            "created_at": ts,
            "updated_at": ts,
        }
        with self.store.tx() as cx:
            self.store.wiki.insert_page(cx, item)
            self.store._audit(
                cx, project_id, "wiki_page", item["id"], "created", item
            )
            self.store._save_idem(
                cx, "create_wiki_page", idempotency_key, request, item
            )
        return item

    def set_wiki_notes(
        self, page_id: str, manual_notes: str
    ) -> dict[str, Any]:
        page = self.store.wiki.get_page(page_id)
        if not page:
            raise KeyError("wiki page not found")
        ts = self.now()
        with self.store.tx() as cx:
            self.store.wiki.update_notes(cx, page_id, manual_notes, ts)
            result = {**page, "manual_notes": manual_notes, "updated_at": ts}
            self.store._audit(
                cx,
                page["project_id"],
                "wiki_page",
                page_id,
                "manual_notes_updated",
                {"length": len(manual_notes), "updated_at": ts},
            )
        return result

    def generate_wiki_revision(
        self,
        page_id: str,
        question: str,
        char_budget: int = 6000,
        generation_metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request = {
            "page_id": page_id,
            "question": question,
            "char_budget": char_budget,
            "generation_metadata": generation_metadata or {},
        }
        if hit := self.store._idem(
            "generate_wiki_revision", idempotency_key, request
        ):
            return hit
        page = self.store.wiki.get_page(page_id)
        if not page:
            raise KeyError("wiki page not found")
        if not question.strip():
            raise ValueError("question cannot be empty")
        brief = self.store.decision_context(
            page["project_id"],
            question.strip(),
            char_budget,
            page["scope_id"],
            False,
        )
        sections = self.store._wiki_sections(brief)
        cited_entries: list[tuple[str, int, str, str]] = []
        for section_name, entries in sections.items():
            for ordinal, entry in enumerate(entries):
                citation_groups = []
                if isinstance(entry, dict) and entry.get("citations"):
                    citation_groups.append(entry["citations"])
                if isinstance(entry, dict):
                    citation_groups.extend(
                        entry[key]
                        for key in ("decision_citation", "outcome_citation")
                        if entry.get(key)
                    )
                for citation in citation_groups:
                    for event_id in citation.get("source_event_ids", []):
                        cited_entries.append(
                            (
                                section_name,
                                ordinal,
                                citation["memory_id"],
                                event_id,
                            )
                        )
        if not cited_entries:
            section_counts = {
                name: len(entries) for name, entries in sections.items()
            }
            gate = brief["retrieval"].get("retrieval_gate", {})
            raise ValueError(
                "wiki revision requires at least one cited memory event;"
                f" retrieval_gate={gate.get('status', 'unknown')};"
                f" retrieved_items={len(brief['retrieval'].get('items', []))};"
                f" section_counts={canonical(section_counts)}; hint=use a"
                " decision-, rationale-, constraint-, alternative-, outcome-,"
                " or open-question-focused query"
            )
        metadata = {
            "contract_version": "topic-wiki/v1",
            "generator": "decision_context",
            "decision_brief_contract": brief["contract_version"],
            "retrieval_used": brief["retrieval"]["used"],
            "caller": generation_metadata or {},
        }
        ts = self.now()
        revision_id = self.uid()
        with self.store.tx() as cx:
            revision_no = cx.execute(
                "SELECT coalesce(max(revision_no),0)+1 FROM wiki_revisions"
                " WHERE page_id=?",
                (page_id,),
            ).fetchone()[0]
            item = {
                "id": revision_id,
                "page_id": page_id,
                "revision_no": revision_no,
                "status": "proposed",
                "question": question.strip(),
                "sections_json": canonical(sections),
                "generation_json": canonical(metadata),
                "created_at": ts,
                "published_at": None,
                "stale_reason": None,
            }
            cx.execute(
                """INSERT INTO wiki_revisions VALUES(:id,:page_id,
              :revision_no,:status,:question,:sections_json,
              :generation_json,:created_at,:published_at,:stale_reason)""",
                item,
            )
            for section_name, ordinal, memory_id, event_id in cited_entries:
                source = cx.execute(
                    """SELECT m.project_id FROM memory_sources s
                  JOIN memories m ON m.id=s.memory_id
                  JOIN events e ON e.id=s.event_id
                  WHERE s.memory_id=? AND s.event_id=?
                    AND m.project_id=e.project_id""",
                    (memory_id, event_id),
                ).fetchone()
                if not source or source["project_id"] != page["project_id"]:
                    raise ValueError(
                        "wiki citations must be exact memory sources in the"
                        " page project"
                    )
                cx.execute(
                    "INSERT OR IGNORE INTO wiki_revision_citations"
                    " VALUES(?,?,?,?,?)",
                    (revision_id, section_name, ordinal, memory_id, event_id),
                )
            response = self.store._wiki_revision_result(item, cited_entries)
            self.store._audit(
                cx,
                page["project_id"],
                "wiki_revision",
                revision_id,
                "created",
                response,
            )
            self.store._save_idem(
                cx,
                "generate_wiki_revision",
                idempotency_key,
                request,
                response,
            )
        return response

    def transition_wiki_revision(
        self, revision_id: str, status: str, reason: str = ""
    ) -> dict[str, Any]:
        if status not in {"published", "stale", "rejected"}:
            raise ValueError("invalid wiki revision status")
        row = self.store._row(
            "SELECT r.*,p.project_id FROM wiki_revisions r JOIN wiki_pages p"
            " ON p.id=r.page_id WHERE r.id=?",
            (revision_id,),
        )
        if not row:
            raise KeyError("wiki revision not found")
        allowed = {
            "proposed": {"published", "rejected"},
            "published": {"stale"},
            "stale": set(),
            "rejected": set(),
        }
        if status not in allowed[row["status"]]:
            raise ValueError(
                f"cannot transition {row['status']} revision to {status}"
            )
        ts = self.now()
        with self.store.tx() as cx:
            if status == "published":
                replaced = [
                    item[0]
                    for item in cx.execute(
                        "SELECT id FROM wiki_revisions WHERE page_id=? AND"
                        " status='published'",
                        (row["page_id"],),
                    )
                ]
                replacement_reason = (
                    f"replaced by revision {row['revision_no']}"
                )
                cx.execute(
                    "UPDATE wiki_revisions SET status='stale',stale_reason=?"
                    " WHERE page_id=? AND status='published'",
                    (replacement_reason, row["page_id"]),
                )
                for replaced_id in replaced:
                    self.store._audit(
                        cx,
                        row["project_id"],
                        "wiki_revision",
                        replaced_id,
                        "status:stale",
                        {"reason": replacement_reason, "at": ts},
                    )
            cx.execute(
                "UPDATE wiki_revisions SET"
                " status=?,published_at=?,stale_reason=? WHERE id=?",
                (
                    status,
                    ts if status == "published" else row["published_at"],
                    reason or None if status == "stale" else None,
                    revision_id,
                ),
            )
            self.store._audit(
                cx,
                row["project_id"],
                "wiki_revision",
                revision_id,
                f"status:{status}",
                {"reason": reason, "at": ts},
            )
        return self.store.get_wiki_revision(revision_id)

    def get_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        row = self.store.wiki.get_revision(revision_id)
        if not row:
            raise KeyError("wiki revision not found")
        citations = [
            (r["section_name"], r["ordinal"], r["memory_id"], r["event_id"])
            for r in self.store.conn.execute(
                "SELECT * FROM wiki_revision_citations WHERE revision_id=?"
                " ORDER BY section_name,ordinal,memory_id,event_id",
                (revision_id,),
            )
        ]
        return self.store._wiki_revision_result(row, citations)

    def lint_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        """Surface evidence and lifecycle gaps in a Wiki revision."""
        revision = self.store.get_wiki_revision(revision_id)
        page = self.store.wiki.get_page(revision["page_id"])
        if not page:
            raise KeyError("wiki page not found")
        findings: list[dict[str, Any]] = []

        def claim_provenance(memory_id: str) -> tuple[str, bool]:
            claim = self.store._row(
                "SELECT id,role FROM investigation_claims WHERE memory_id=?",
                (memory_id,),
            )
            if claim:
                supported = bool(
                    self.store._row(
                        "SELECT 1 AS found FROM investigation_claim_links"
                        " WHERE to_claim_id=? LIMIT 1",
                        (claim["id"],),
                    )
                )
                return claim["role"], supported
            memory = self.store._row(
                "SELECT type FROM memories WHERE id=?", (memory_id,)
            )
            role = (
                "decision"
                if memory and memory["type"] == "decision"
                else "evidence"
            )
            supported = bool(
                self.store._row(
                    """SELECT 1 AS found FROM edges WHERE to_memory_id=?
              AND relation IN ('supports','depends_on') LIMIT 1""",
                    (memory_id,),
                )
            )
            return role, supported

        cited_memories = {item["memory_id"] for item in revision["citations"]}
        provenance = {
            memory_id: claim_provenance(memory_id)
            for memory_id in embedded_memory_ids(revision)
        }
        findings.extend(embedded_citation_findings(revision, provenance))
        citation_sections: dict[str, set[str]] = {}
        for citation in revision["citations"]:
            citation_sections.setdefault(citation["memory_id"], set()).add(
                citation["section"]
            )

        for memory_id in sorted(cited_memories):
            memory = self.store.memories.get(memory_id)
            exact_sources = 0
            indexed = 0
            if memory:
                exact_sources = self.store.conn.execute(
                    """SELECT count(*) FROM wiki_revision_citations c
                  JOIN memory_sources s ON s.memory_id=c.memory_id
                    AND s.event_id=c.event_id
                  JOIN events e ON e.id=s.event_id AND e.project_id=?
                  WHERE c.revision_id=? AND c.memory_id=?""",
                    (page["project_id"], revision_id, memory_id),
                ).fetchone()[0]
                indexed = self.store.conn.execute(
                    "SELECT count(*) FROM wiki_revision_citations WHERE"
                    " revision_id=? AND memory_id=?",
                    (revision_id, memory_id),
                ).fetchone()[0]
            findings.extend(
                memory_citation_findings(
                    memory_id,
                    memory,
                    citation_sections.get(memory_id, set()),
                    indexed,
                    exact_sources,
                )
            )

        inspected_at = self.current_datetime()
        source_versions = self.store.conn.execute(
            """SELECT DISTINCT c.memory_id,s.id AS source_analysis_id,
          s.source_type,s.stable_source_id,s.canonical_uri,s.source_version,
          s.source_updated_at,s.retrieved_at FROM investigation_claims c
          JOIN source_analyses s ON s.id=c.source_analysis_id
          WHERE c.memory_id IN (SELECT memory_id
            FROM wiki_revision_citations WHERE revision_id=?)
          ORDER BY c.memory_id,s.retrieved_at,s.id""",
            (revision_id,),
        )
        for source in source_versions:
            finding = source_reinspection_finding(dict(source), inspected_at)
            if finding:
                findings.append(finding)

        if revision["status"] == "stale":
            findings.append(
                {
                    "code": "stale_revision",
                    "severity": "error",
                    "message": "Wiki revision is marked stale.",
                    "reason": revision["stale_reason"],
                }
            )

        query = " ".join(
            part for part in (page["topic"], revision["question"]) if part
        ).strip()
        relevant = self.store.search(
            page["project_id"],
            query,
            limit=20,
            statuses=["active"],
            scope_id=page["scope_id"],
        )
        for memory in relevant:
            # Tasks are workflow state, not claims in the standard
            # Decision Wiki page shape. In particular,
            # accumulated checkpoint/handoff tasks otherwise dominate
            # omission lint despite being intentionally
            # absent from generated sections.
            if memory["type"] == "task":
                continue
            # Broad OR-based retrieval is useful for recall, but
            # omission lint is an attention-demanding review signal.
            # Limit it to the leading direct lexical candidates so a
            # shared product or repository name does not turn loosely
            # related operational memories into warnings. Vector-only
            # and deep lexical matches remain available to generation
            # without being presented as deterministic omissions.
            lexical_rank = memory.get("retrieval", {}).get("lexical_rank")
            if lexical_rank is None or lexical_rank > 10:
                continue
            if memory["id"] not in cited_memories:
                findings.append(
                    {
                        "code": "omitted_current_memory",
                        "severity": "warning",
                        "message": (
                            "Relevant active memory is omitted from the"
                            " revision."
                        ),
                        "memory_id": memory["id"],
                        "memory_type": memory["type"],
                        "title": memory["title"],
                    }
                )
        return finish_lint_result(revision, findings)

    def get_wiki_page(self, page_id: str) -> dict[str, Any]:
        page = self.store.wiki.get_page(page_id)
        if not page:
            raise KeyError("wiki page not found")
        page["contract_version"] = "topic-wiki/v1"
        page["revisions"] = [
            self.store.get_wiki_revision(row["id"])
            for row in self.store.conn.execute(
                "SELECT id FROM wiki_revisions WHERE page_id=? ORDER BY"
                " revision_no",
                (page_id,),
            )
        ]
        return page

    def browse_wiki(
        self,
        project_id: str,
        page_id: str | None = None,
        scope_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Browse the Wiki index and reverse citation links."""
        if not self.store._row(
            "SELECT id FROM projects WHERE id=?", (project_id,)
        ):
            raise KeyError("project not found")
        if scope_id and not self.store._row(
            "SELECT id FROM scopes WHERE id=? AND project_id=?",
            (scope_id, project_id),
        ):
            raise ValueError("scope must belong to project")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        where = "project_id=?" + (" AND scope_id=?" if scope_id else "")
        params: list[Any] = [project_id] + ([scope_id] if scope_id else [])
        rows = list(
            self.store.conn.execute(
                f"SELECT * FROM wiki_pages WHERE {where} ORDER BY topic"
                " COLLATE NOCASE,title COLLATE NOCASE,id LIMIT ? OFFSET ?",
                (*params, limit + 1, offset),
            )
        )
        has_more = len(rows) > limit
        rows = rows[:limit]

        def current_revision(page: dict[str, Any]) -> dict[str, Any] | None:
            row = self.store._row(
                """SELECT * FROM wiki_revisions
              WHERE page_id=? AND status<>'rejected'
              ORDER BY CASE status WHEN 'published' THEN 0
                WHEN 'proposed' THEN 1 ELSE 2 END,
              revision_no DESC LIMIT 1""",
                (page["id"],),
            )
            return row

        page_ids = [row["id"] for row in rows]
        current_by_page: dict[str, dict[str, Any]] = {}
        counts_by_page: dict[str, dict[str, int]] = {}
        citations_by_revision: dict[str, int] = {}
        if page_ids:
            placeholders = ",".join("?" for _ in page_ids)
            for row in self.store.conn.execute(
                f"""SELECT * FROM (
                SELECT r.*,row_number() OVER (PARTITION BY r.page_id ORDER BY
                  CASE r.status WHEN 'published' THEN 0
                    WHEN 'proposed' THEN 1 ELSE 2 END,
                  r.revision_no DESC) AS rank
                FROM wiki_revisions r
                WHERE r.page_id IN ({placeholders})
                  AND r.status<>'rejected')
              WHERE rank=1""",
                page_ids,
            ):
                current_by_page[row["page_id"]] = dict(row)
            for row in self.store.conn.execute(
                f"""SELECT page_id,status,count(*) AS count FROM wiki_revisions
              WHERE page_id IN ({placeholders}) GROUP BY page_id,status""",
                page_ids,
            ):
                counts_by_page.setdefault(row["page_id"], {})[
                    row["status"]
                ] = row["count"]
            revision_ids = [row["id"] for row in current_by_page.values()]
            if revision_ids:
                revision_placeholders = ",".join("?" for _ in revision_ids)
                for row in self.store.conn.execute(
                    f"""SELECT revision_id,count(DISTINCT memory_id) AS count
                  FROM wiki_revision_citations
                  WHERE revision_id IN ({revision_placeholders})
                  GROUP BY revision_id""",
                    revision_ids,
                ):
                    citations_by_revision[row["revision_id"]] = row["count"]

        pages = []
        for raw in rows:
            page = dict(raw)
            current = current_by_page.get(page["id"])
            counts = counts_by_page.get(page["id"], {})
            citation_count = (
                citations_by_revision.get(current["id"], 0) if current else 0
            )
            reader_state = (
                current["status"] if current else "no_current_revision"
            )
            pages.append(
                {
                    "id": page["id"],
                    "topic": page["topic"],
                    "title": page["title"],
                    "scope_id": page["scope_id"],
                    "updated_at": page["updated_at"],
                    "reader_state": reader_state,
                    "renderable": current is not None,
                    "current_revision": (
                        {
                            "id": current["id"],
                            "revision_no": current["revision_no"],
                            "status": current["status"],
                            "created_at": current["created_at"],
                        }
                        if current
                        else None
                    ),
                    "revision_counts": {
                        status: counts.get(status, 0)
                        for status in (
                            "proposed",
                            "published",
                            "stale",
                            "rejected",
                        )
                    },
                    "cited_memory_count": citation_count,
                }
            )

        selected = None
        if page_id:
            selected_page = self.store._row(
                "SELECT * FROM wiki_pages WHERE id=? AND project_id=?",
                (page_id, project_id),
            )
            if not selected_page:
                raise KeyError("wiki page not found in project")
            if scope_id and selected_page["scope_id"] != scope_id:
                raise ValueError("wiki page is outside requested scope")
            selected_revision = current_revision(selected_page)
            backlinks = []
            if selected_revision:
                cited = list(
                    self.store.conn.execute(
                        """SELECT DISTINCT c.memory_id,m.title,m.status
                  FROM wiki_revision_citations c
                  JOIN memories m ON m.id=c.memory_id
                  WHERE c.revision_id=?
                  ORDER BY m.title COLLATE NOCASE,c.memory_id""",
                        (selected_revision["id"],),
                    )
                )
                links_by_memory: dict[str, list[dict[str, Any]]] = {
                    memory["memory_id"]: [] for memory in cited
                }
                if cited:
                    cited_ids = [memory["memory_id"] for memory in cited]
                    cited_placeholders = ",".join("?" for _ in cited_ids)
                    for row in self.store.conn.execute(
                        f"""WITH current AS (
                        SELECT r.*,row_number() OVER (
                          PARTITION BY r.page_id ORDER BY
                          CASE r.status WHEN 'published' THEN 0
                            WHEN 'proposed' THEN 1 ELSE 2 END,
                          r.revision_no DESC) AS rank
                        FROM wiki_revisions r
                        JOIN wiki_pages p ON p.id=r.page_id
                        WHERE p.project_id=?
                          AND (? IS NULL OR p.scope_id=?)
                          AND r.status<>'rejected'),
                      links AS (
                        SELECT DISTINCT c.memory_id,p.id AS page_id,
                          p.topic,p.title,current.id AS revision_id,
                          current.revision_no,current.status
                        FROM current
                        JOIN wiki_pages p ON p.id=current.page_id
                        JOIN wiki_revision_citations c
                          ON c.revision_id=current.id
                        WHERE current.rank=1
                          AND c.memory_id IN ({cited_placeholders})
                          AND p.id<>?),
                      ranked AS (
                        SELECT links.*,row_number() OVER (
                          PARTITION BY memory_id ORDER BY topic COLLATE NOCASE,
                          title COLLATE NOCASE,page_id) AS backlink_rank
                        FROM links)
                      SELECT * FROM ranked WHERE backlink_rank<=101
                      ORDER BY memory_id,backlink_rank""",
                        (project_id, scope_id, scope_id, *cited_ids, page_id),
                    ):
                        item = dict(row)
                        item.pop("backlink_rank")
                        item.pop("memory_id")
                        links_by_memory[row["memory_id"]].append(item)
                for memory in cited:
                    linked = links_by_memory[memory["memory_id"]]
                    backlinks.append(
                        {
                            "memory_id": memory["memory_id"],
                            "memory_title": memory["title"],
                            "memory_status": memory["status"],
                            "pages": linked[:100],
                            "has_more": len(linked) > 100,
                        }
                    )
            selected = {
                "page_id": page_id,
                "current_revision_id": (
                    selected_revision["id"] if selected_revision else None
                ),
                "backlinks": backlinks,
            }
        return {
            "contract_version": "topic-wiki-navigation/v1",
            "project_id": project_id,
            "scope_id": scope_id,
            "pages": pages,
            "topic_index": [
                {
                    "topic": item["topic"],
                    "page_id": item["id"],
                    "title": item["title"],
                }
                for item in pages
            ],
            "renderable_page_count": sum(
                1 for item in pages if item["renderable"]
            ),
            "unrenderable_page_count": sum(
                1 for item in pages if not item["renderable"]
            ),
            "page_count": len(pages),
            "offset": offset,
            "next_offset": offset + len(pages) if has_more else None,
            "has_more": has_more,
            "selected": selected,
            "search_index_duplicated": False,
            "state_changed": False,
        }

    def render_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        revision = self.store.get_wiki_revision(revision_id)
        page = self.store.wiki.get_page(revision["page_id"])
        return {
            "contract_version": "topic-wiki-markdown/v1",
            "revision_id": revision_id,
            "markdown": render_wiki_markdown(
                page["title"], page["manual_notes"], revision
            ),
        }

    def export_wiki_markdown(
        self,
        project_id: str,
        scope_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Render a bounded, non-authoritative Wiki snapshot."""
        navigation = self.store.browse_wiki(
            project_id, scope_id=scope_id, limit=limit, offset=offset
        )
        pages = [
            page for page in navigation["pages"] if page["current_revision"]
        ]
        paths = {page["id"]: f"pages/{page['id']}.md" for page in pages}
        revision_ids = [page["current_revision"]["id"] for page in pages]
        related: dict[str, set[str]] = {page["id"]: set() for page in pages}
        if revision_ids:
            placeholders = ",".join("?" for _ in revision_ids)
            rows = list(
                self.store.conn.execute(
                    f"""SELECT DISTINCT a.page_id AS from_page,
                b.page_id AS to_page
              FROM wiki_revision_citations ca
              JOIN wiki_revisions a ON a.id=ca.revision_id
              JOIN wiki_revision_citations cb ON cb.memory_id=ca.memory_id
              JOIN wiki_revisions b ON b.id=cb.revision_id
              WHERE a.id IN ({placeholders})
                AND b.id IN ({placeholders})
                AND a.page_id<>b.page_id
              ORDER BY from_page,to_page""",
                    (*revision_ids, *revision_ids),
                )
            )
            for row in rows:
                related[row["from_page"]].add(row["to_page"])

        documents = []
        for index, page in enumerate(pages):
            current = page["current_revision"]
            body = self.store.render_wiki_revision(current["id"])[
                "markdown"
            ].splitlines()
            metadata = [
                "---",
                f"page_id: {page['id']}",
                f"revision_id: {current['id']}",
                f"revision_no: {current['revision_no']}",
                f"status: {current['status']}",
                f"topic: {json.dumps(page['topic'], ensure_ascii=False)}",
                "---",
                "",
            ]
            links = ["[Wiki index](../index.md)"]
            if index:
                links.append(f"[Previous](../{paths[pages[index - 1]['id']]})")
            if index + 1 < len(pages):
                links.append(f"[Next](../{paths[pages[index + 1]['id']]})")
            lines = (
                metadata + body + ["", "## Navigation", "", " · ".join(links)]
            )
            related_ids = sorted(
                related[page["id"]], key=lambda item: paths[item]
            )
            if related_ids:
                lines.extend(["", "### Related pages", ""])
                by_id = {item["id"]: item for item in pages}
                lines.extend(
                    f"- [{by_id[item]['title']}](../{paths[item]})"
                    for item in related_ids
                )
            lines.append("")
            documents.append(
                {
                    "path": paths[page["id"]],
                    "page_id": page["id"],
                    "revision_id": current["id"],
                    "markdown": "\n".join(lines),
                }
            )

        index_lines = ["# Decision Wiki", "", f"Project: `{project_id}`", ""]
        if scope_id:
            index_lines.extend([f"Scope: `{scope_id}`", ""])
        index_lines.extend(["## Pages", ""])
        if documents:
            by_id = {item["id"]: item for item in pages}
            index_lines.extend(
                f"- [{by_id[doc['page_id']]['title']}]({doc['path']}) —"
                f" {by_id[doc['page_id']]['topic']}"
                for doc in documents
            )
        else:
            index_lines.append(
                "_No renderable current revisions in this export window._"
            )
        index_lines.append("")
        return {
            "contract_version": "topic-wiki-export/v1",
            "project_id": project_id,
            "scope_id": scope_id,
            "offset": offset,
            "next_offset": navigation["next_offset"],
            "has_more": navigation["has_more"],
            "page_count": len(documents),
            "source_page_count": navigation["page_count"],
            "skipped_page_count": navigation["unrenderable_page_count"],
            "index": {"path": "index.md", "markdown": "\n".join(index_lines)},
            "documents": documents,
            "authoritative_source": "sqlite",
            "markdown_writable_authority": False,
            "state_changed": False,
        }

    def _stale_wiki_revisions_for_memory(
        self, cx: sqlite3.Connection, memory_id: str, reason: str
    ) -> list[str]:
        rows = list(
            cx.execute(
                """SELECT DISTINCT r.id,p.project_id FROM wiki_revisions r
          JOIN wiki_pages p ON p.id=r.page_id
          JOIN wiki_revision_citations c ON c.revision_id=r.id
          WHERE c.memory_id=? AND r.status='published'""",
                (memory_id,),
            )
        )
        ids = [row["id"] for row in rows]
        if ids:
            cx.execute(
                "UPDATE wiki_revisions SET status='stale',stale_reason=?"
                " WHERE id IN"
                f" ({','.join('?' for _ in ids)})",
                (reason, *ids),
            )
            for row in rows:
                self.store._audit(
                    cx,
                    row["project_id"],
                    "wiki_revision",
                    row["id"],
                    "status:stale",
                    {
                        "reason": reason,
                        "memory_id": memory_id,
                        "at": self.now(),
                    },
                )
        return ids
