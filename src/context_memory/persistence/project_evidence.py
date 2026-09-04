"""Project and immutable-evidence persistence queries."""

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..clock import utc_datetime, utc_now
from ..retrieval import DISCOVERY_PROJECT_CANDIDATE_LIMIT
from ..serialization import canonical
from .primitives import row_dict, row_exists


class ProjectEvidenceRepository:
    """Own bounded project, alias, event, and audit read queries."""

    def __init__(
        self,
        owner: Any,
        now: Callable[[], str] | None = None,
        current_datetime: Callable[[], datetime] | None = None,
        uid: Callable[[], str] | None = None,
    ):
        self.store = None if isinstance(owner, sqlite3.Connection) else owner
        self.connection: sqlite3.Connection = (
            owner if self.store is None else owner.conn
        )
        self.now = now or utc_now
        self.current_datetime = current_datetime or utc_datetime
        self.uid = uid or (lambda: str(uuid.uuid4()))

    def project_exists(self, project_id: str) -> bool:
        return row_exists(
            self.connection,
            "SELECT 1 FROM projects WHERE id=?",
            (project_id,),
        )

    @staticmethod
    def insert_scope(
        connection: sqlite3.Connection, item: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO scopes"
            " VALUES(:id,:project_id,:name,:path,:created_at)",
            item,
        )

    def find_session(
        self, project_id: str, client: str, external_id: str
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM sessions WHERE project_id=? AND client=? AND"
            " external_id=?",
            (project_id, client, external_id),
        ).fetchone()
        return row_dict(row)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return row_dict(row)

    @staticmethod
    def insert_session(
        connection: sqlite3.Connection, item: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO sessions"
            " VALUES(:id,:project_id,:scope_id,:client,:external_id,"
            ":started_at,:ended_at,:metadata_json)",
            item,
        )

    @staticmethod
    def set_session_ended(
        connection: sqlite3.Connection, session_id: str, ended_at: str
    ) -> None:
        connection.execute(
            "UPDATE sessions SET ended_at=? WHERE id=?",
            (ended_at, session_id),
        )

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM projects ORDER BY slug"
            )
        ]

    def list_project_aliases(self, project_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM project_aliases WHERE project_id=? ORDER BY"
                " kind,normalized",
                (project_id,),
            )
        ]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM events WHERE id=?", (event_id,)
        ).fetchone()
        return row_dict(row)

    @staticmethod
    def message_ttl_seconds(
        connection: sqlite3.Connection, project_id: str
    ) -> int | None:
        row = connection.execute(
            "SELECT message_ttl_seconds FROM project_policies WHERE"
            " project_id=?",
            (project_id,),
        ).fetchone()
        return row["message_ttl_seconds"] if row else None

    @staticmethod
    def allocate_event_sequence(
        connection: sqlite3.Connection, project_id: str
    ) -> int | None:
        row = connection.execute(
            "UPDATE project_event_cursors SET next_seq=next_seq+1 WHERE"
            " project_id=? RETURNING next_seq-1",
            (project_id,),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def insert_event(
        connection: sqlite3.Connection, item: dict[str, Any]
    ) -> None:
        connection.execute(
            """INSERT INTO events(id,project_id,scope_id,session_id,
          kind,content,source_uri,metadata_json,content_hash,created_at,
          event_seq) VALUES(:id,:project_id,:scope_id,:session_id,
          :kind,:content,:source_uri,:metadata_json,:content_hash,
          :created_at,:event_seq)""",
            item,
        )

    def audit_entries(
        self, entity_type: str, entity_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM audit_log WHERE entity_type=? AND entity_id=?"
                " ORDER BY seq",
                (entity_type, entity_id),
            )
        ]

    def read_events_since(
        self,
        project_id: str,
        cursor: int = 0,
        kinds: list[str] | None = None,
        scope_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read project events after a cursor without ranking them."""
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        if kinds is not None and (
            not kinds or any(not kind.strip() for kind in kinds)
        ):
            raise ValueError("kinds must contain non-empty values")
        state = self.store._row(
            "SELECT next_seq-1 AS snapshot_cursor FROM project_event_cursors"
            " WHERE project_id=?",
            (project_id,),
        )
        if not state:
            raise KeyError("project not found")
        snapshot = state["snapshot_cursor"]
        sql = (
            "SELECT * FROM events WHERE project_id=? AND event_seq>? AND"
            " event_seq<=?"
        )
        args: list[Any] = [project_id, cursor, snapshot]
        if kinds:
            unique_kinds = list(dict.fromkeys(kinds))
            sql += " AND kind IN (" + ",".join("?" for _ in unique_kinds) + ")"
            args.extend(unique_kinds)
        if scope_id:
            sql += " AND (scope_id=? OR scope_id IS NULL)"
            args.append(scope_id)
        sql += " ORDER BY event_seq LIMIT ?"
        args.append(limit + 1)
        rows = [dict(row) for row in self.store.conn.execute(sql, args)]
        has_more = len(rows) > limit
        rows = rows[:limit]
        page_cursor = rows[-1]["event_seq"] if has_more and rows else snapshot
        visible = []
        current = self.current_datetime()
        for row in rows:
            row["metadata"] = json.loads(row.pop("metadata_json"))
            expires_at = (
                row["metadata"].get("expires_at")
                if row["kind"] == "message"
                else None
            )
            if expires_at:
                try:
                    expired = (
                        datetime.fromisoformat(
                            expires_at.replace("Z", "+00:00")
                        )
                        <= current
                    )
                except (TypeError, ValueError):
                    expired = False
                if expired:
                    continue
            visible.append(row)
        rows = visible
        next_cursor = page_cursor if has_more else snapshot
        return {
            "project_id": project_id,
            "cursor": cursor,
            "snapshot_cursor": snapshot,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "events": rows,
        }

    def cursor_for_recent_events(
        self,
        project_id: str,
        kinds: list[str] | None = None,
        scope_id: str | None = None,
        limit: int = 6,
    ) -> int:
        """Return a cursor that exposes the newest matching event tail."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        if kinds is not None and (
            not kinds or any(not kind.strip() for kind in kinds)
        ):
            raise ValueError("kinds must contain non-empty values")
        sql = "SELECT event_seq FROM events WHERE project_id=?"
        args: list[Any] = [project_id]
        if kinds:
            unique_kinds = list(dict.fromkeys(kinds))
            sql += " AND kind IN (" + ",".join("?" for _ in unique_kinds) + ")"
            args.extend(unique_kinds)
        if scope_id:
            sql += " AND (scope_id=? OR scope_id IS NULL)"
            args.append(scope_id)
        sql += " ORDER BY event_seq DESC LIMIT 1 OFFSET ?"
        args.append(limit - 1)
        row = self.store.conn.execute(sql, args).fetchone()
        return max(0, int(row["event_seq"]) - 1) if row else 0

    @staticmethod
    def _receipt_stream(
        kinds: list[str] | None, scope_id: str | None
    ) -> tuple[str, str, list[str] | None]:
        normalized = sorted(set(kinds)) if kinds else None
        return scope_id or "", canonical(normalized or []), normalized

    def poll_events(
        self,
        project_id: str,
        consumer_id: str,
        kinds: list[str] | None = None,
        scope_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read a consumer receipt without acknowledging delivery."""
        consumer_id = consumer_id.strip()
        if not consumer_id:
            raise ValueError("consumer_id cannot be empty")
        scope_key, kinds_json, normalized = self._receipt_stream(
            kinds, scope_id
        )
        receipt = self.store._row(
            """SELECT * FROM event_receipts
          WHERE project_id=? AND consumer_id=?
          AND scope_key=? AND kinds_json=?""",
            (project_id, consumer_id, scope_key, kinds_json),
        )
        cursor = receipt["acknowledged_cursor"] if receipt else 0
        result = self.read_events_since(
            project_id, cursor, normalized, scope_id, limit
        )
        delivered = max(cursor, result["next_cursor"])
        ts = self.now()
        with self.store.tx() as cx:
            cx.execute(
                """INSERT INTO event_receipts(project_id,consumer_id,
              scope_key,kinds_json,acknowledged_cursor,delivered_cursor,
              created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(project_id,consumer_id,scope_key,kinds_json)
              DO UPDATE SET delivered_cursor=max(
                event_receipts.delivered_cursor,excluded.delivered_cursor),
                updated_at=excluded.updated_at""",
                (
                    project_id,
                    consumer_id,
                    scope_key,
                    kinds_json,
                    cursor,
                    delivered,
                    ts,
                    ts,
                ),
            )
        result.update(
            {
                "consumer_id": consumer_id,
                "acknowledged_cursor": cursor,
                "delivered_cursor": delivered,
            }
        )
        return result

    def acknowledge_events(
        self,
        project_id: str,
        consumer_id: str,
        cursor: int,
        kinds: list[str] | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        """Acknowledge a cursor delivered for this exact stream."""
        consumer_id = consumer_id.strip()
        if not consumer_id:
            raise ValueError("consumer_id cannot be empty")
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        scope_key, kinds_json, _ = self._receipt_stream(kinds, scope_id)
        with self.store.tx() as cx:
            row = cx.execute(
                """SELECT * FROM event_receipts
              WHERE project_id=? AND consumer_id=?
              AND scope_key=? AND kinds_json=?""",
                (project_id, consumer_id, scope_key, kinds_json),
            ).fetchone()
            if not row:
                raise KeyError(
                    "event receipt not found; poll this stream before"
                    " acknowledging"
                )
            if cursor < row["acknowledged_cursor"]:
                raise ValueError("acknowledged cursor cannot move backwards")
            if cursor > row["delivered_cursor"]:
                raise ValueError(
                    "cannot acknowledge beyond the delivered cursor"
                )
            ts = self.now()
            cx.execute(
                """UPDATE event_receipts
              SET acknowledged_cursor=?,updated_at=? WHERE project_id=?
              AND consumer_id=? AND scope_key=? AND kinds_json=?""",
                (cursor, ts, project_id, consumer_id, scope_key, kinds_json),
            )
            item = dict(
                cx.execute(
                    """SELECT * FROM event_receipts
              WHERE project_id=? AND consumer_id=?
              AND scope_key=? AND kinds_json=?""",
                    (project_id, consumer_id, scope_key, kinds_json),
                ).fetchone()
            )
            item["kinds"] = json.loads(item.pop("kinds_json"))
            item["scope_id"] = item.pop("scope_key") or None
            self.store._audit(
                cx,
                project_id,
                "event_receipt",
                f"{consumer_id}:{scope_key}:{kinds_json}",
                "acknowledged",
                item,
            )
        return item

    def record_event(
        self,
        project_id: str,
        kind: str,
        content: str,
        session_id: str | None = None,
        scope_id: str | None = None,
        source_uri: str | None = None,
        metadata: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request = locals().copy()
        request.pop("self")
        request.pop("idempotency_key")
        if hit := self.store._idem("record_event", idempotency_key, request):
            if "event_seq" not in hit:
                migrated = self.store._row(
                    "SELECT event_seq FROM events WHERE id=?", (hit["id"],)
                )
                if migrated:
                    hit["event_seq"] = migrated["event_seq"]
            self.store._add_promotion_advisory(hit)
            return hit
        if not content.strip():
            raise ValueError("event content cannot be empty")
        with self.store.tx() as cx:
            stored_metadata = dict(metadata or {})
            if kind == "message" and "expires_at" not in stored_metadata:
                ttl_seconds = self.message_ttl_seconds(cx, project_id)
                if ttl_seconds:
                    stored_metadata["expires_at"] = (
                        self.current_datetime()
                        + timedelta(seconds=ttl_seconds)
                    ).isoformat()
            event_seq = self.allocate_event_sequence(cx, project_id)
            if event_seq is None:
                raise KeyError("project not found")
            item = {
                "id": self.uid(),
                "project_id": project_id,
                "scope_id": scope_id,
                "session_id": session_id,
                "kind": kind,
                "content": content,
                "source_uri": source_uri,
                "metadata_json": canonical(stored_metadata),
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "created_at": self.now(),
                "event_seq": event_seq,
            }
            self.insert_event(cx, item)
            self.store._audit(
                cx, project_id, "event", item["id"], "recorded", item
            )
            self.store._add_promotion_advisory(item)
            self.store._save_idem(
                cx, "record_event", idempotency_key, request, item
            )
        return item

    def create_project(
        self,
        slug: str,
        name: str | None = None,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request = {"slug": slug, "name": name, "description": description}
        if hit := self.store._idem("create_project", idempotency_key, request):
            return hit
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", slug):
            raise ValueError("invalid project slug")
        item = {
            "id": self.uid(),
            "slug": slug,
            "name": name or slug,
            "description": description,
            "created_at": self.now(),
        }
        with self.store.tx() as cx:
            cx.execute(
                "INSERT INTO projects"
                " VALUES(:id,:slug,:name,:description,:created_at)",
                item,
            )
            normalized_name = self._normalize_project_alias(
                "name", item["name"]
            )
            cx.execute(
                "INSERT INTO project_aliases VALUES(?,?,?,?,?,?)",
                (
                    item["id"],
                    "name",
                    item["name"],
                    normalized_name,
                    item["created_at"],
                    item["created_at"],
                ),
            )
            self.store._audit(
                cx, item["id"], "project", item["id"], "created", item
            )
            self.store._save_idem(
                cx, "create_project", idempotency_key, request, item
            )
        return item

    @staticmethod
    def _normalize_project_alias(kind: str, value: str) -> str:
        value = value.strip()
        if kind == "path":
            return str(Path(value).expanduser().resolve())
        return value.casefold()

    def set_project_alias(
        self, project_id: str, kind: str, value: str
    ) -> dict[str, Any]:
        if kind not in {"path", "name"}:
            raise ValueError("invalid project alias kind")
        if not self.store._row(
            "SELECT id FROM projects WHERE id=?", (project_id,)
        ):
            raise KeyError("project not found")
        normalized = self._normalize_project_alias(kind, value)
        if not normalized:
            raise ValueError("project alias cannot be empty")
        ts = self.now()
        item = {
            "project_id": project_id,
            "kind": kind,
            "value": value,
            "normalized": normalized,
            "created_at": ts,
            "updated_at": ts,
        }
        current = self.store._row(
            "SELECT * FROM project_aliases WHERE project_id=? AND kind=? AND"
            " normalized=?",
            (project_id, kind, normalized),
        )
        if current and current["value"] == value:
            return current
        with self.store.tx() as cx:
            existing = cx.execute(
                "SELECT created_at FROM project_aliases WHERE project_id=? AND"
                " kind=? AND normalized=?",
                (project_id, kind, normalized),
            ).fetchone()
            if existing:
                item["created_at"] = existing["created_at"]
            cx.execute(
                """INSERT INTO project_aliases(
                project_id,kind,value,normalized,created_at,updated_at)
              VALUES(:project_id,:kind,:value,:normalized,:created_at,
                :updated_at)
              ON CONFLICT(project_id,kind,normalized) DO UPDATE SET
                value=excluded.value,updated_at=excluded.updated_at""",
                item,
            )
            self.store._audit(
                cx,
                project_id,
                "project_alias",
                f"{kind}:{normalized}",
                "updated" if existing else "created",
                item,
            )
        return item

    def _workspace_identities(self, path: str) -> dict[str, str]:
        return {"path": path, "name": Path(path).name}

    def _register_project_identities(
        self, project_id: str, identities: dict[str, str]
    ) -> None:
        for kind, value in identities.items():
            self.set_project_alias(project_id, kind, value)

    def _related_project_ids(self, project_id: str) -> list[str]:
        """Find projects sharing the hinted workspace name."""
        rows = self.store.conn.execute(
            """SELECT DISTINCT candidate.project_id
          FROM project_aliases source JOIN project_aliases candidate
            ON candidate.kind=source.kind
            AND candidate.normalized=source.normalized
          WHERE source.project_id=? AND candidate.project_id<>?
            AND source.kind='name'
          ORDER BY candidate.project_id""",
            (project_id, project_id),
        )
        return list(dict.fromkeys(row["project_id"] for row in rows))

    def _discovery_project_candidates(
        self,
        project_id: str,
        query_tokens: list[str],
        lexical: list[dict[str, Any]],
    ) -> list[str]:
        """Bound discovery using lexical or identity evidence."""
        ordered: list[str] = []

        def add(candidate_id: str) -> None:
            if candidate_id != project_id and candidate_id not in ordered:
                ordered.append(candidate_id)

        for memory in lexical:
            if memory["visibility"] == "project":
                add(memory["project_id"])
        for candidate_id in self._related_project_ids(project_id):
            add(candidate_id)
        # Registry identity matching is the fallback when memory FTS
        # supplied no
        # project evidence. Avoid an alias join on the common lexical
        # path.
        if (
            not lexical
            and query_tokens
            and len(ordered) < DISCOVERY_PROJECT_CANDIDATE_LIMIT
        ):
            clauses = []
            args: list[Any] = []
            for token in query_tokens:
                pattern = f"%{token}%"
                clauses.append(
                    "(lower(p.slug) LIKE ? OR lower(p.name) LIKE ? OR"
                    " lower(COALESCE(p.description,'')) LIKE ? OR"
                    " lower(a.normalized) LIKE ?)"
                )
                args.extend([pattern] * 4)
            rows = self.store.conn.execute(
                f"""SELECT DISTINCT p.id FROM projects p
              LEFT JOIN project_aliases a ON a.project_id=p.id
              WHERE p.id<>? AND ({" OR ".join(clauses)})
              ORDER BY p.id LIMIT ?""",
                [project_id, *args, DISCOVERY_PROJECT_CANDIDATE_LIMIT + 1],
            )
            for row in rows:
                add(row["id"])
        return ordered[:DISCOVERY_PROJECT_CANDIDATE_LIMIT]

    def create_scope(
        self, project_id: str, name: str, path: str | None = None
    ) -> dict[str, Any]:
        item = {
            "id": self.uid(),
            "project_id": project_id,
            "name": name,
            "path": path,
            "created_at": self.now(),
        }
        with self.store.tx() as cx:
            self.insert_scope(cx, item)
            self.store._audit(
                cx, project_id, "scope", item["id"], "created", item
            )
        return item

    def resolve_project(self, cwd: str) -> dict[str, Any]:
        """Resolve a workspace by path, then repository identity."""
        path = str(Path(cwd).expanduser().resolve())
        identities = self._workspace_identities(path)
        row = self.store.conn.execute(
            """SELECT p.*, s.id AS scope_id FROM scopes s
          JOIN projects p ON p.id=s.project_id WHERE s.path=?""",
            (path,),
        ).fetchone()
        if row:
            item = dict(row)
            scope_id = item.pop("scope_id")
            self._register_project_identities(item["id"], identities)
            return {"project": item, "scope_id": scope_id, "created": False}
        path_matches = list(
            self.store.conn.execute(
                "SELECT DISTINCT project_id FROM project_aliases WHERE "
                "kind='path' AND normalized=?",
                (path,),
            )
        )
        if len(path_matches) == 1:
            project = self.store._row(
                "SELECT * FROM projects WHERE id=?",
                (path_matches[0]["project_id"],),
            )
            path_digest = hashlib.sha256(path.encode()).hexdigest()[:12]
            scope = self.create_scope(
                project["id"], f"__workspace__:{path_digest}", path
            )
            self._register_project_identities(project["id"], identities)
            return {
                "project": project,
                "scope_id": scope["id"],
                "created": False,
                "matched_by": "path",
            }
        # A repository name resolves ownership only when it identifies
        # one project.
        # Ambiguous names remain separate and are handled by retrieval
        # discovery.
        for kind in ("name",):
            if kind not in identities:
                continue
            normalized = self._normalize_project_alias(kind, identities[kind])
            matches = list(
                self.store.conn.execute(
                    "SELECT DISTINCT project_id FROM project_aliases WHERE"
                    " kind=? AND normalized=?",
                    (kind, normalized),
                )
            )
            if len(matches) != 1:
                continue
            project = self.store._row(
                "SELECT * FROM projects WHERE id=?",
                (matches[0]["project_id"],),
            )
            path_digest = hashlib.sha256(path.encode()).hexdigest()[:12]
            scope = self.create_scope(
                project["id"],
                f"__workspace__:{path_digest}",
                path,
            )
            self._register_project_identities(project["id"], identities)
            return {
                "project": project,
                "scope_id": scope["id"],
                "created": False,
                "matched_by": kind,
            }
        base = (
            re.sub(r"[^a-z0-9._-]+", "-", Path(path).name.lower()).strip("-._")
            or "workspace"
        )
        slug = base[:54]
        existing = self.store._row(
            "SELECT * FROM projects WHERE slug=?", (slug,)
        )
        if existing:
            has_root = row_exists(
                self.store.conn,
                "SELECT 1 FROM scopes WHERE project_id=? AND path IS NOT NULL",
                (existing["id"],),
            )
            if not has_root:
                scope = self.create_scope(existing["id"], "__root__", path)
                return {
                    "project": existing,
                    "scope_id": scope["id"],
                    "created": False,
                }
            slug = f"{slug}-{hashlib.sha256(path.encode()).hexdigest()[:8]}"
        project = self.create_project(
            slug,
            Path(path).name,
            f"Automatically mapped from agent workspace: {path}",
        )
        scope = self.create_scope(project["id"], "__root__", path)
        self._register_project_identities(project["id"], identities)
        return {"project": project, "scope_id": scope["id"], "created": True}

    def start_session(
        self,
        project_id: str,
        client: str = "codex",
        scope_id: str | None = None,
        external_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        if external_id:
            hit = self.find_session(project_id, client, external_id)
            if hit:
                return hit
        item = {
            "id": self.uid(),
            "project_id": project_id,
            "scope_id": scope_id,
            "client": client,
            "external_id": external_id,
            "started_at": self.now(),
            "ended_at": None,
            "metadata_json": canonical(metadata or {}),
        }
        with self.store.tx() as cx:
            self.insert_session(cx, item)
            self.store._audit(
                cx, project_id, "session", item["id"], "started", item
            )
        return item

    def end_session(
        self,
        session_id: str,
        summary: str | None = None,
        extract_candidates: bool = True,
    ) -> dict[str, Any]:
        with self.store.tx() as cx:
            row = self.get_session(session_id)
            if not row:
                raise KeyError("session not found")
            ended = row["ended_at"] or self.now()
            self.set_session_ended(cx, session_id, ended)
            result = dict(row)
            result["ended_at"] = ended
            self.store._audit(
                cx,
                row["project_id"],
                "session",
                session_id,
                "ended",
                {"summary": summary, **result},
            )
        result["review"] = (
            self.store.extract_session_candidates(session_id)
            if extract_candidates
            else {"created": [], "conflicts": []}
        )
        return result
