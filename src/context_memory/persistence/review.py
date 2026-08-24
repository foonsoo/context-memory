"""Memory review and session extraction persistence."""

from typing import Any, Callable

from ..contracts import PROMOTABLE_EVENT_KINDS


class ReviewRepository:
    """Own review workflow persistence behind the stable facade."""

    def __init__(
        self,
        store: Any,
        now: Callable[[], str],
        uid: Callable[[], str],
    ):
        self.store = store
        self.now = now
        self.uid = uid

    def extract_session_candidates(self, session_id: str) -> dict[str, Any]:
        session = self.store._row(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        )
        if not session:
            raise KeyError("session not found")
        kinds = set(PROMOTABLE_EVENT_KINDS)
        created, conflicts = [], []
        events = self.store.conn.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY event_seq",
            (session_id,),
        )
        for event in events:
            if event["kind"] not in kinds:
                continue
            existing_source = self.store._row(
                "SELECT memory_id FROM memory_sources WHERE event_id=?",
                (event["id"],),
            )
            if existing_source:
                continue
            title = (
                event["content"].strip().splitlines()[0][:120]
                or event["kind"].title()
            )
            candidate = self.store.upsert_memory(
                session["project_id"],
                title,
                event["content"],
                event["kind"],
                "proposed",
                0.6,
                0.5,
                session["scope_id"],
                [event["id"]],
                idempotency_key=f"candidate:{event['id']}",
            )
            created.append(candidate)
            for active in self.store.conn.execute(
                "SELECT * FROM memories WHERE project_id=? AND status='active'"
                " AND id<>?",
                (session["project_id"], candidate["id"]),
            ):
                similarity = self.store._text_similarity(
                    f"{candidate['title']} {candidate['content']}",
                    f"{active['title']} {active['content']}",
                )
                if similarity < 0.35:
                    continue
                reason = (
                    "similar active memory; review for duplicate, replacement,"
                    " or dispute"
                )
                with self.store.tx() as cx:
                    cx.execute(
                        "INSERT OR IGNORE INTO review_conflicts"
                        " VALUES(?,?,?,?,?)",
                        (
                            candidate["id"],
                            active["id"],
                            similarity,
                            reason,
                            self.now(),
                        ),
                    )
                conflicts.append(
                    {
                        "candidate_memory_id": candidate["id"],
                        "existing_memory_id": active["id"],
                        "similarity": similarity,
                        "reason": reason,
                    }
                )
        return {"created": created, "conflicts": conflicts}

    def review_queue(self, project_id: str) -> list[dict[str, Any]]:
        if not self.store._row(
            "SELECT id FROM projects WHERE id=?", (project_id,)
        ):
            raise KeyError("project not found")
        rows = []
        for row in self.store.conn.execute(
            "SELECT * FROM memories WHERE project_id=? AND status='proposed'"
            " ORDER BY created_at,id",
            (project_id,),
        ):
            item = dict(row)
            item["review_kind"] = "memory_candidate"
            item["conflicts"] = [
                dict(x)
                for x in self.store.conn.execute(
                    "SELECT * FROM review_conflicts WHERE"
                    " candidate_memory_id=? ORDER BY similarity DESC",
                    (item["id"],),
                )
            ]
            item["sources"] = [
                dict(x)
                for x in self.store.conn.execute(
                    "SELECT e.id,e.kind,e.source_uri,e.created_at FROM"
                    " memory_sources s JOIN events e ON e.id=s.event_id WHERE"
                    " s.memory_id=?",
                    (item["id"],),
                )
            ]
            item["available_actions"] = ["approve", "reject"] + (
                ["supersede", "dispute"] if item["conflicts"] else []
            )
            item["queue_priority"] = 2
            rows.append(item)
        revisions = self.store.conn.execute(
            """SELECT r.id FROM wiki_revisions r
          JOIN wiki_pages p ON p.id=r.page_id
          WHERE p.project_id=? AND r.status<>'rejected' AND r.revision_no=(
            SELECT max(latest.revision_no) FROM wiki_revisions latest
            WHERE latest.page_id=r.page_id AND latest.status<>'rejected')
          ORDER BY p.created_at,p.id,r.revision_no,r.id""",
            (project_id,),
        )
        for revision_row in revisions:
            lint = self.store.lint_wiki_revision(revision_row["id"])
            revision = self.store.get_wiki_revision(revision_row["id"])
            if revision["status"] != "proposed" and not lint["findings"]:
                continue
            page = self.store._row(
                "SELECT title,topic FROM wiki_pages WHERE id=?",
                (revision["page_id"],),
            )
            actions = []
            if revision["status"] == "proposed":
                actions = [
                    {
                        "action": "approve",
                        "tool": "wiki_revision_transition",
                        "arguments": {"status": "published"},
                    },
                    {
                        "action": "reject",
                        "tool": "wiki_revision_transition",
                        "arguments": {"status": "rejected"},
                    },
                ]
            priority = (
                0
                if revision["status"] == "proposed"
                and lint["status"] == "fail"
                else 1
                if revision["status"] == "proposed"
                else 3
            )
            rows.append(
                {
                    "review_kind": "wiki_revision",
                    "id": revision["id"],
                    "page_id": revision["page_id"],
                    "page_title": page["title"],
                    "topic": page["topic"],
                    "revision_no": revision["revision_no"],
                    "status": revision["status"],
                    "created_at": revision["created_at"],
                    "queue_priority": priority,
                    "lint": lint,
                    "available_actions": actions,
                }
            )
        rows.sort(
            key=lambda item: (
                item["queue_priority"],
                (
                    ""
                    if item["review_kind"] == "memory_candidate"
                    else item["created_at"]
                ),
                item["id"],
            ),
            reverse=False,
        )
        return rows

    def propose_correction(
        self,
        project_id: str,
        memory_id: str,
        content: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        existing = self.store._row(
            "SELECT * FROM memories WHERE id=? AND project_id=?",
            (memory_id, project_id),
        )
        if not existing:
            raise KeyError("memory not found")
        event = self.store.record_event(
            project_id,
            "correction",
            content,
            scope_id=existing["scope_id"],
            metadata={"corrects_memory_id": memory_id},
        )
        candidate = self.store.upsert_memory(
            project_id,
            title or existing["title"],
            content,
            existing["type"],
            "proposed",
            existing["confidence"],
            existing["importance"],
            existing["scope_id"],
            [event["id"]],
            visibility=existing["visibility"],
        )
        with self.store.tx() as cx:
            cx.execute(
                "INSERT OR IGNORE INTO review_conflicts VALUES(?,?,?,?,?)",
                (
                    candidate["id"],
                    memory_id,
                    1.0,
                    "explicit correction",
                    self.now(),
                ),
            )
        return candidate

    def review_candidate(
        self,
        memory_id: str,
        action: str,
        related_memory_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        candidate = self.store.memories.get_proposed(memory_id)
        if not candidate:
            raise KeyError("proposed memory not found")
        if action == "approve":
            return self.store.transition(memory_id, "active", note=note)
        if action == "reject":
            return self.store.transition(memory_id, "rejected", note=note)
        if action not in {"supersede", "dispute"}:
            raise ValueError(
                "action must be approve, reject, supersede, or dispute"
            )
        target = related_memory_id
        if not target:
            row = self.store._row(
                "SELECT existing_memory_id FROM review_conflicts WHERE"
                " candidate_memory_id=? ORDER BY similarity DESC LIMIT 1",
                (memory_id,),
            )
            target = row["existing_memory_id"] if row else None
        if not target:
            raise ValueError("related_memory_id is required")
        self.store.transition(memory_id, "active", note=note)
        status = "superseded" if action == "supersede" else "disputed"
        self.store.transition(target, status, memory_id, note)
        return self.store.memories.get(memory_id)
