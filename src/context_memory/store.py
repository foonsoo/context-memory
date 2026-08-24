from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import retrieval, wiki_lint
from .audit_serialization import (
    build_audit_checkpoint,
    serialize_audit_chain,
    verify_audit_chain_bundle,
)
from .contracts import (
    MEMORY_TYPES,
    PROMOTABLE_EVENT_KINDS,
)
from .embeddings import (
    EmbeddingProvider,
    LocalHashEmbedding,
    SentenceTransformerEmbedding,
)
from .persistence import (
    CheckpointRepository,
    InvestigationRepository,
    MaintenanceRepository,
    MemoryRepository,
    ProjectEvidenceRepository,
    WikiRepository,
)
from .retrieval import (
    DISCOVERY_PROJECT_CANDIDATE_LIMIT,
    LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT,
    LOCAL_HASH_FALLBACK_TIME_LIMIT_MS,
    retrieval_gate,
    select_project_candidate,
)
from .serialization import canonical
from .validation import normalize_test_results

DISCOVERY_MIN_CONFIDENCE = retrieval.DISCOVERY_MIN_CONFIDENCE
DISCOVERY_AUTO_SELECT_CONFIDENCE = retrieval.DISCOVERY_AUTO_SELECT_CONFIDENCE
DISCOVERY_MIN_MARGIN = retrieval.DISCOVERY_MIN_MARGIN
NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY = (
    retrieval.NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY
)
NEGATIVE_VECTOR_ONLY_MIN_SEPARATION = (
    retrieval.NEGATIVE_VECTOR_ONLY_MIN_SEPARATION
)
SOURCE_REINSPECTION_AGE_DAYS = wiki_lint.SOURCE_REINSPECTION_AGE_DAYS

TYPES = MEMORY_TYPES
STATUSES = {
    "proposed",
    "active",
    "superseded",
    "disputed",
    "expired",
    "rejected",
}
RELATIONS = {"supersedes", "disputes", "supports", "depends_on", "related_to"}
CHECKPOINT_MODES = {"interim", "final"}
CHECKPOINT_REASONS = {
    "context_budget",
    "elapsed",
    "material_change",
    "completed",
    "manual",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_datetime() -> datetime:
    """Return time through the patchable timestamp contract."""
    return datetime.fromisoformat(now())


def uid() -> str:
    return str(uuid.uuid4())


class MemoryStore:
    def __init__(
        self,
        db_path: str | Path,
        embedding_provider: EmbeddingProvider | None = None,
    ):
        self.path = Path(db_path).expanduser().resolve()
        self._secure_directory()
        self.conn = sqlite3.connect(
            self.path, isolation_level=None, timeout=10
        )
        self.conn.row_factory = sqlite3.Row
        self.project_evidence = ProjectEvidenceRepository(self.conn)
        self.checkpoints = CheckpointRepository(
            self, now, uid, current_datetime
        )
        self.investigations = InvestigationRepository(self, now, uid)
        self.maintenance = MaintenanceRepository(self.conn)
        self.memories = MemoryRepository(self, now, uid)
        self.wiki = WikiRepository(self, now, uid, current_datetime)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.migrate()
        configured_mode = os.environ.get(
            "CONTEXT_MEMORY_EMBEDDINGS", "local-hash"
        ).strip()
        mode = configured_mode.casefold()
        embeddings_enabled = mode not in {
            "0",
            "false",
            "off",
            "disabled",
            "none",
        }
        if embedding_provider:
            self.embedding_provider = embedding_provider
        elif mode in {"neural", "sentence-transformers"}:
            model = os.environ.get(
                "CONTEXT_MEMORY_EMBEDDING_MODEL", ""
            ).strip()
            if not model:
                raise ValueError(
                    "CONTEXT_MEMORY_EMBEDDING_MODEL is required when "
                    "CONTEXT_MEMORY_EMBEDDINGS=neural"
                )
            self.embedding_provider = SentenceTransformerEmbedding(
                model,
                device=os.environ.get("CONTEXT_MEMORY_EMBEDDING_DEVICE")
                or None,
            )
        else:
            self.embedding_provider = (
                LocalHashEmbedding() if embeddings_enabled else None
            )
        if self.embedding_provider:
            with self.tx() as cx:
                for memory in cx.execute("SELECT * FROM memories"):
                    self._index_embedding(cx, dict(memory))

    def _secure_directory(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, stat.S_IRWXU)
        except OSError:
            pass

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def migrate(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER"
            " PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        here = Path(__file__).resolve()
        roots = [
            here.parents[2] / "migrations",
            here.parents[1] / "migrations",
        ]
        root = next(
            (candidate for candidate in roots if candidate.is_dir()), roots[0]
        )
        if not root.is_dir():
            raise RuntimeError(
                "database migrations are missing from this installation"
            )
        applied = {
            r[0]
            for r in self.conn.execute("SELECT version FROM schema_migrations")
        }
        for file in sorted(root.glob("[0-9][0-9][0-9]_*.sql")):
            version = int(file.name.split("_", 1)[0])
            if version in applied:
                continue
            script = file.read_text(encoding="utf-8")
            # executescript owns its transaction; migration is recorded
            # only
            # after all statements succeed.
            self.conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + script
                + "\nINSERT INTO schema_migrations"
                f" VALUES({version},'{now()}');\nCOMMIT;"
            )

    def _row(self, query: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        row = self.conn.execute(query, args).fetchone()
        return dict(row) if row else None

    def _audit(
        self,
        cx: sqlite3.Connection,
        project_id: str | None,
        kind: str,
        entity_id: str,
        action: str,
        snapshot: Any,
    ) -> None:
        cx.execute(
            "INSERT INTO"
            " audit_log(project_id,entity_type,entity_id,action,"
            "snapshot_json,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (project_id, kind, entity_id, action, canonical(snapshot), now()),
        )

    def _idem(
        self, operation: str, key: str | None, request: Any
    ) -> dict[str, Any] | None:
        if not key:
            return None
        row = self._row(
            "SELECT request_hash,response_json FROM idempotency_keys WHERE"
            " operation=? AND key=?",
            (operation, key),
        )
        if not row:
            return None
        digest = hashlib.sha256(canonical(request).encode()).hexdigest()
        if digest != row["request_hash"]:
            raise ValueError("idempotency key reused with a different request")
        return json.loads(row["response_json"])

    def _save_idem(
        self,
        cx: sqlite3.Connection,
        operation: str,
        key: str | None,
        request: Any,
        response: Any,
    ) -> None:
        if key:
            cx.execute(
                "INSERT INTO idempotency_keys VALUES(?,?,?,?,?)",
                (
                    operation,
                    key,
                    hashlib.sha256(canonical(request).encode()).hexdigest(),
                    canonical(response),
                    now(),
                ),
            )

    def create_project(
        self,
        slug: str,
        name: str | None = None,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request = {"slug": slug, "name": name, "description": description}
        if hit := self._idem("create_project", idempotency_key, request):
            return hit
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", slug):
            raise ValueError("invalid project slug")
        item = {
            "id": uid(),
            "slug": slug,
            "name": name or slug,
            "description": description,
            "created_at": now(),
        }
        with self.tx() as cx:
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
            self._audit(cx, item["id"], "project", item["id"], "created", item)
            self._save_idem(
                cx, "create_project", idempotency_key, request, item
            )
        return item

    def list_projects(self) -> list[dict[str, Any]]:
        return self.project_evidence.list_projects()

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
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError("project not found")
        normalized = self._normalize_project_alias(kind, value)
        if not normalized:
            raise ValueError("project alias cannot be empty")
        ts = now()
        item = {
            "project_id": project_id,
            "kind": kind,
            "value": value,
            "normalized": normalized,
            "created_at": ts,
            "updated_at": ts,
        }
        current = self._row(
            "SELECT * FROM project_aliases WHERE project_id=? AND kind=? AND"
            " normalized=?",
            (project_id, kind, normalized),
        )
        if current and current["value"] == value:
            return current
        with self.tx() as cx:
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
            self._audit(
                cx,
                project_id,
                "project_alias",
                f"{kind}:{normalized}",
                "updated" if existing else "created",
                item,
            )
        return item

    def list_project_aliases(self, project_id: str) -> list[dict[str, Any]]:
        return self.project_evidence.list_project_aliases(project_id)

    def _workspace_identities(self, path: str) -> dict[str, str]:
        return {"path": path, "name": Path(path).name}

    def _register_project_identities(
        self, project_id: str, identities: dict[str, str]
    ) -> None:
        for kind, value in identities.items():
            self.set_project_alias(project_id, kind, value)

    def _related_project_ids(self, project_id: str) -> list[str]:
        """Find projects sharing the hinted workspace name."""
        rows = self.conn.execute(
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
            rows = self.conn.execute(
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
            "id": uid(),
            "project_id": project_id,
            "name": name,
            "path": path,
            "created_at": now(),
        }
        with self.tx() as cx:
            self.project_evidence.insert_scope(cx, item)
            self._audit(cx, project_id, "scope", item["id"], "created", item)
        return item

    def resolve_project(self, cwd: str) -> dict[str, Any]:
        """Resolve a workspace by path, then repository identity."""
        path = str(Path(cwd).expanduser().resolve())
        identities = self._workspace_identities(path)
        row = self.conn.execute(
            """SELECT p.*, s.id AS scope_id FROM scopes s
          JOIN projects p ON p.id=s.project_id WHERE s.path=?""",
            (path,),
        ).fetchone()
        if row:
            item = dict(row)
            scope_id = item.pop("scope_id")
            self._register_project_identities(item["id"], identities)
            return {"project": item, "scope_id": scope_id, "created": False}
        # A repository name resolves ownership only when it identifies
        # one project.
        # Ambiguous names remain separate and are handled by retrieval
        # discovery.
        for kind in ("name",):
            if kind not in identities:
                continue
            normalized = self._normalize_project_alias(kind, identities[kind])
            matches = list(
                self.conn.execute(
                    "SELECT DISTINCT project_id FROM project_aliases WHERE"
                    " kind=? AND normalized=?",
                    (kind, normalized),
                )
            )
            if len(matches) != 1:
                continue
            project = self._row(
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
        existing = self._row("SELECT * FROM projects WHERE slug=?", (slug,))
        if existing:
            has_root = self.conn.execute(
                "SELECT 1 FROM scopes WHERE project_id=? AND path IS NOT NULL",
                (existing["id"],),
            ).fetchone()
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
            hit = self.project_evidence.find_session(
                project_id, client, external_id
            )
            if hit:
                return hit
        item = {
            "id": uid(),
            "project_id": project_id,
            "scope_id": scope_id,
            "client": client,
            "external_id": external_id,
            "started_at": now(),
            "ended_at": None,
            "metadata_json": canonical(metadata or {}),
        }
        with self.tx() as cx:
            self.project_evidence.insert_session(cx, item)
            self._audit(cx, project_id, "session", item["id"], "started", item)
        return item

    def end_session(
        self,
        session_id: str,
        summary: str | None = None,
        extract_candidates: bool = True,
    ) -> dict[str, Any]:
        with self.tx() as cx:
            row = self.project_evidence.get_session(session_id)
            if not row:
                raise KeyError("session not found")
            ended = row["ended_at"] or now()
            self.project_evidence.set_session_ended(cx, session_id, ended)
            result = dict(row)
            result["ended_at"] = ended
            self._audit(
                cx,
                row["project_id"],
                "session",
                session_id,
                "ended",
                {"summary": summary, **result},
            )
        result["review"] = (
            self.extract_session_candidates(session_id)
            if extract_candidates
            else {"created": [], "conflicts": []}
        )
        return result

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {
            token
            for token in re.findall(
                r"[\w-]+", text.casefold(), flags=re.UNICODE
            )
            if len(token) > 1
        }

    @classmethod
    def _text_similarity(cls, left: str, right: str) -> float:
        a, b = cls._terms(left), cls._terms(right)
        return len(a & b) / len(a | b) if a and b else 0.0

    def extract_session_candidates(self, session_id: str) -> dict[str, Any]:
        session = self._row("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not session:
            raise KeyError("session not found")
        kinds = set(PROMOTABLE_EVENT_KINDS)
        created, conflicts = [], []
        events = self.conn.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY event_seq",
            (session_id,),
        )
        for event in events:
            if event["kind"] not in kinds:
                continue
            existing_source = self._row(
                "SELECT memory_id FROM memory_sources WHERE event_id=?",
                (event["id"],),
            )
            if existing_source:
                continue
            title = (
                event["content"].strip().splitlines()[0][:120]
                or event["kind"].title()
            )
            candidate = self.upsert_memory(
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
            for active in self.conn.execute(
                "SELECT * FROM memories WHERE project_id=? AND status='active'"
                " AND id<>?",
                (session["project_id"], candidate["id"]),
            ):
                similarity = self._text_similarity(
                    f"{candidate['title']} {candidate['content']}",
                    f"{active['title']} {active['content']}",
                )
                if similarity < 0.35:
                    continue
                reason = (
                    "similar active memory; review for duplicate, replacement,"
                    " or dispute"
                )
                with self.tx() as cx:
                    cx.execute(
                        "INSERT OR IGNORE INTO review_conflicts"
                        " VALUES(?,?,?,?,?)",
                        (
                            candidate["id"],
                            active["id"],
                            similarity,
                            reason,
                            now(),
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
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError("project not found")
        rows = []
        for row in self.conn.execute(
            "SELECT * FROM memories WHERE project_id=? AND status='proposed'"
            " ORDER BY created_at,id",
            (project_id,),
        ):
            item = dict(row)
            item["review_kind"] = "memory_candidate"
            item["conflicts"] = [
                dict(x)
                for x in self.conn.execute(
                    "SELECT * FROM review_conflicts WHERE"
                    " candidate_memory_id=? ORDER BY similarity DESC",
                    (item["id"],),
                )
            ]
            item["sources"] = [
                dict(x)
                for x in self.conn.execute(
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
        revisions = self.conn.execute(
            """SELECT r.id FROM wiki_revisions r
          JOIN wiki_pages p ON p.id=r.page_id
          WHERE p.project_id=? AND r.status<>'rejected' AND r.revision_no=(
            SELECT max(latest.revision_no) FROM wiki_revisions latest
            WHERE latest.page_id=r.page_id AND latest.status<>'rejected')
          ORDER BY p.created_at,p.id,r.revision_no,r.id""",
            (project_id,),
        )
        for revision_row in revisions:
            lint = self.lint_wiki_revision(revision_row["id"])
            revision = self.get_wiki_revision(revision_row["id"])
            if revision["status"] != "proposed" and not lint["findings"]:
                continue
            page = self._row(
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
        existing = self._row(
            "SELECT * FROM memories WHERE id=? AND project_id=?",
            (memory_id, project_id),
        )
        if not existing:
            raise KeyError("memory not found")
        event = self.record_event(
            project_id,
            "correction",
            content,
            scope_id=existing["scope_id"],
            metadata={"corrects_memory_id": memory_id},
        )
        candidate = self.upsert_memory(
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
        with self.tx() as cx:
            cx.execute(
                "INSERT OR IGNORE INTO review_conflicts VALUES(?,?,?,?,?)",
                (
                    candidate["id"],
                    memory_id,
                    1.0,
                    "explicit correction",
                    now(),
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
        candidate = self.memories.get_proposed(memory_id)
        if not candidate:
            raise KeyError("proposed memory not found")
        if action == "approve":
            return self.transition(memory_id, "active", note=note)
        if action == "reject":
            return self.transition(memory_id, "rejected", note=note)
        if action not in {"supersede", "dispute"}:
            raise ValueError(
                "action must be approve, reject, supersede, or dispute"
            )
        target = related_memory_id
        if not target:
            row = self._row(
                "SELECT existing_memory_id FROM review_conflicts WHERE"
                " candidate_memory_id=? ORDER BY similarity DESC LIMIT 1",
                (memory_id,),
            )
            target = row["existing_memory_id"] if row else None
        if not target:
            raise ValueError("related_memory_id is required")
        self.transition(memory_id, "active", note=note)
        status = "superseded" if action == "supersede" else "disputed"
        self.transition(target, status, memory_id, note)
        return self.memories.get(memory_id)

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
        if hit := self._idem("record_event", idempotency_key, request):
            if "event_seq" not in hit:
                migrated = self._row(
                    "SELECT event_seq FROM events WHERE id=?", (hit["id"],)
                )
                if migrated:
                    hit["event_seq"] = migrated["event_seq"]
            self._add_promotion_advisory(hit)
            return hit
        if not content.strip():
            raise ValueError("event content cannot be empty")
        with self.tx() as cx:
            stored_metadata = dict(metadata or {})
            if kind == "message" and "expires_at" not in stored_metadata:
                ttl_seconds = self.project_evidence.message_ttl_seconds(
                    cx, project_id
                )
                if ttl_seconds:
                    stored_metadata["expires_at"] = (
                        current_datetime() + timedelta(seconds=ttl_seconds)
                    ).isoformat()
            event_seq = self.project_evidence.allocate_event_sequence(
                cx, project_id
            )
            if event_seq is None:
                raise KeyError("project not found")
            item = {
                "id": uid(),
                "project_id": project_id,
                "scope_id": scope_id,
                "session_id": session_id,
                "kind": kind,
                "content": content,
                "source_uri": source_uri,
                "metadata_json": canonical(stored_metadata),
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "created_at": now(),
                "event_seq": event_seq,
            }
            self.project_evidence.insert_event(cx, item)
            self._audit(cx, project_id, "event", item["id"], "recorded", item)
            self._add_promotion_advisory(item)
            self._save_idem(cx, "record_event", idempotency_key, request, item)
        return item

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
        return self.investigations.create_investigation(
            project_id,
            question,
            reason,
            decision_to_inform,
            constraints,
            initiator,
            scope_id,
            investigation_id,
            idempotency_key,
        )

    def record_source_analysis(
        self,
        investigation_id: str,
        source: dict[str, Any],
        claims: list[dict[str, Any]],
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.investigations.record_source_analysis(
            investigation_id, source, claims, session_id, idempotency_key
        )

    def get_investigation(
        self, investigation_id: str, source_analysis_id: str | None = None
    ) -> dict[str, Any]:
        result = self.investigations.get_investigation(
            investigation_id, source_analysis_id
        )
        if not result:
            raise KeyError("investigation not found")
        return result

    def request_source_reinspection(
        self,
        source_analysis_id: str,
        reason: str,
        details: str | None = None,
        known_source_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.investigations.request_source_reinspection(
            source_analysis_id,
            reason,
            details,
            known_source_version,
            idempotency_key,
        )

    def complete_investigation(self, investigation_id: str) -> dict[str, Any]:
        return self.investigations.complete_investigation(investigation_id)

    def create_wiki_page(
        self,
        project_id: str,
        topic: str,
        title: str,
        scope_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.wiki.create_wiki_page(
            project_id, topic, title, scope_id, idempotency_key
        )

    def set_wiki_notes(
        self, page_id: str, manual_notes: str
    ) -> dict[str, Any]:
        return self.wiki.set_wiki_notes(page_id, manual_notes)

    @staticmethod
    def _wiki_sections(brief: dict[str, Any]) -> dict[str, Any]:
        return {
            "current_position": brief["current_decisions"],
            "why_it_exists": brief["rationale"],
            "governing_constraints": brief["constraints"],
            "considered_alternatives": brief["alternatives"],
            "trade_offs": brief["expected_vs_observed"],
            "decision_timeline": brief["history"],
            "observed_outcomes": brief["outcomes"],
            "open_questions": brief["open_questions"],
        }

    def generate_wiki_revision(
        self,
        page_id: str,
        question: str,
        char_budget: int = 6000,
        generation_metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.wiki.generate_wiki_revision(
            page_id,
            question,
            char_budget,
            generation_metadata,
            idempotency_key,
        )

    @staticmethod
    def _wiki_revision_result(
        item: dict[str, Any], citations: list[tuple[str, int, str, str]]
    ) -> dict[str, Any]:
        result = dict(item)
        result["sections"] = json.loads(result.pop("sections_json"))
        result["generation"] = json.loads(result.pop("generation_json"))
        result["citations"] = [
            {"section": s, "ordinal": o, "memory_id": m, "event_id": e}
            for s, o, m, e in sorted(citations)
        ]
        return result

    def transition_wiki_revision(
        self, revision_id: str, status: str, reason: str = ""
    ) -> dict[str, Any]:
        return self.wiki.transition_wiki_revision(revision_id, status, reason)

    def get_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        return self.wiki.get_wiki_revision(revision_id)

    def lint_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        return self.wiki.lint_wiki_revision(revision_id)

    def get_wiki_page(self, page_id: str) -> dict[str, Any]:
        return self.wiki.get_wiki_page(page_id)

    def browse_wiki(
        self,
        project_id: str,
        page_id: str | None = None,
        scope_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self.wiki.browse_wiki(
            project_id, page_id, scope_id, limit, offset
        )

    def render_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        return self.wiki.render_wiki_revision(revision_id)

    def export_wiki_markdown(
        self,
        project_id: str,
        scope_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self.wiki.export_wiki_markdown(
            project_id, scope_id, limit, offset
        )

    def _stale_wiki_revisions_for_memory(
        self, cx: sqlite3.Connection, memory_id: str, reason: str
    ) -> list[str]:
        return self.wiki._stale_wiki_revisions_for_memory(
            cx, memory_id, reason
        )

    @staticmethod
    def _add_promotion_advisory(item: dict[str, Any]) -> None:
        if not item.get("session_id"):
            return
        eligible = item.get("kind") in PROMOTABLE_EVENT_KINDS
        item["promotion"] = {
            "eligible": eligible,
            "automatic_at_session_end": eligible,
            "promotable_kinds": list(PROMOTABLE_EVENT_KINDS),
        }
        if not eligible:
            item["promotion"]["warning"] = (
                "Event kind"
                f" '{item.get('kind')}'"
                " is preserved as immutable evidence but is not automatically"
                " converted to a proposed memory at session_end. Record new"
                " evidence with a promotable kind if a memory candidate is"
                " intended; do not rewrite this event."
            )

    def create_checkpoint(
        self,
        project_id: str,
        mode: str,
        reason: str,
        goal: str,
        idempotency_key: str,
        session_id: str | None = None,
        scope_id: str | None = None,
        completed: list[str] | None = None,
        next_step: str | None = None,
        blockers: list[str] | None = None,
        source_event_cursor: int | None = None,
        context_usage: float | None = None,
        repository_path: str | None = None,
        test_results: list[dict[str, Any]] | None = None,
        verified_event_ids: list[str] | None = None,
        handoff_title: str | None = None,
        handoff_content: str | None = None,
        previous_handoff_memory_id: str | None = None,
        commit: str | None = None,
    ) -> dict[str, Any]:
        return self.checkpoints.create_checkpoint(
            project_id,
            mode,
            reason,
            goal,
            idempotency_key,
            session_id,
            scope_id,
            completed,
            next_step,
            blockers,
            source_event_cursor,
            context_usage,
            repository_path,
            test_results,
            verified_event_ids,
            handoff_title,
            handoff_content,
            previous_handoff_memory_id,
            commit,
        )

    def evaluate_checkpoint(
        self,
        project_id: str,
        context_usage: float | None = None,
        session_id: str | None = None,
        repository_path: str | None = None,
        goal: str = "",
        completed: list[str] | None = None,
        next_step: str | None = None,
        blockers: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.checkpoints.evaluate_checkpoint(
            project_id,
            context_usage,
            session_id,
            repository_path,
            goal,
            completed,
            next_step,
            blockers,
        )

    def _checkpoint_recovery_hash(
        self,
        project_id: str,
        cursor: int,
        goal: str,
        completed: list[str],
        next_step: str | None,
        blockers: list[str],
        repository: dict[str, Any] | None,
    ) -> str:
        event_hashes = [
            row[0]
            for row in self.conn.execute(
                "SELECT content_hash FROM events WHERE project_id=? AND"
                " event_seq<=? AND kind<>'checkpoint' ORDER BY event_seq",
                (project_id, cursor),
            )
        ]
        state = {
            "goal": goal.strip(),
            "completed": [item.strip() for item in completed],
            "next_step": next_step.strip() if next_step else None,
            "blockers": [item.strip() for item in blockers],
            "repository": repository,
            "event_hashes": event_hashes,
        }
        return hashlib.sha256(canonical(state).encode()).hexdigest()

    @staticmethod
    def _normalize_test_results(
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return normalize_test_results(results)

    @staticmethod
    def _repository_facts(path: str) -> dict[str, Any]:
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("repository_path must be an existing directory")

        def git(*args: str) -> str:
            completed = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise ValueError(
                    "repository_path must identify a Git worktree"
                )
            return completed.stdout.rstrip("\n")

        top_level = str(Path(git("rev-parse", "--show-toplevel")).resolve())
        head = git("rev-parse", "HEAD")
        branch_value = (
            git("symbolic-ref", "--quiet", "--short", "HEAD")
            if subprocess.run(
                ["git", "-C", str(root), "symbolic-ref", "--quiet", "HEAD"],
                capture_output=True,
            ).returncode
            == 0
            else None
        )
        changed = []
        entries = git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all"
        ).split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            if not entry:
                break
            status, path_value = entry[:2], entry[3:]
            changed.append({"path": path_value, "status": status})
            index += 2 if "R" in status or "C" in status else 1
        return {
            "root": top_level,
            "head": head,
            "branch": branch_value,
            "dirty": bool(changed),
            "changed_files": changed,
        }

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
        state = self._row(
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
        rows = [dict(row) for row in self.conn.execute(sql, args)]
        has_more = len(rows) > limit
        rows = rows[:limit]
        page_cursor = rows[-1]["event_seq"] if has_more and rows else snapshot
        visible = []
        current = current_datetime()
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
        receipt = self._row(
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
        ts = now()
        with self.tx() as cx:
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
        with self.tx() as cx:
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
            ts = now()
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
            self._audit(
                cx,
                project_id,
                "event_receipt",
                f"{consumer_id}:{scope_key}:{kinds_json}",
                "acknowledged",
                item,
            )
        return item

    def upsert_memory(
        self,
        project_id: str,
        title: str,
        content: str,
        memory_type: str = "other",
        status: str = "proposed",
        confidence: float = 0.5,
        importance: float = 0.5,
        scope_id: str | None = None,
        source_event_ids: list[str] | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        tags: list[str] | None = None,
        observed_at: str | None = None,
        last_confirmed_at: str | None = None,
        visibility: str | None = None,
        memory_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.memories.upsert_memory(
            project_id,
            title,
            content,
            memory_type,
            status,
            confidence,
            importance,
            scope_id,
            source_event_ids,
            valid_from,
            valid_until,
            tags,
            observed_at,
            last_confirmed_at,
            visibility,
            memory_id,
            idempotency_key,
        )

    def _provider_name(self) -> str | None:
        return self.memories.provider_name()

    def _index_embedding(
        self, cx: sqlite3.Connection, memory: dict[str, Any]
    ) -> None:
        self.memories.index_embedding(cx, memory)

    def transition(
        self,
        memory_id: str,
        status: str,
        related_memory_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        return self.memories.transition(
            memory_id, status, related_memory_id, note
        )

    def set_search_aliases(
        self, project_id: str, term: str, aliases: list[str]
    ) -> dict[str, Any]:
        normalized = term.strip().casefold()
        values = sorted(
            {value.strip().casefold() for value in aliases if value.strip()}
            - {normalized}
        )
        if not normalized or not values:
            raise ValueError(
                "term and at least one distinct alias are required"
            )
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError("project not found")
        item = {
            "project_id": project_id,
            "term": normalized,
            "aliases_json": canonical(values),
            "updated_at": now(),
        }
        existing = self._row(
            "SELECT created_at FROM search_aliases WHERE project_id=? AND"
            " term=?",
            (project_id, normalized),
        )
        item["created_at"] = (
            existing["created_at"] if existing else item["updated_at"]
        )
        with self.tx() as cx:
            cx.execute(
                """INSERT INTO search_aliases(project_id,term,aliases_json,
              created_at,updated_at) VALUES(:project_id,:term,
              :aliases_json,:created_at,:updated_at)
              ON CONFLICT(project_id,term) DO UPDATE SET
              aliases_json=excluded.aliases_json,
              updated_at=excluded.updated_at""",
                item,
            )
            self._audit(
                cx,
                project_id,
                "search_alias",
                normalized,
                "updated" if existing else "created",
                item,
            )
        return {**item, "aliases": values}

    def list_search_aliases(self, project_id: str) -> list[dict[str, Any]]:
        rows = []
        for row in self.conn.execute(
            "SELECT * FROM search_aliases WHERE project_id=? ORDER BY term",
            (project_id,),
        ):
            item = dict(row)
            item["aliases"] = json.loads(item.pop("aliases_json"))
            rows.append(item)
        return rows

    def create_relation(
        self,
        project_id: str,
        from_memory_id: str,
        to_memory_id: str,
        relation: str,
        note: str = "",
    ) -> dict[str, Any]:
        if relation not in RELATIONS:
            raise ValueError("invalid relation")
        if from_memory_id == to_memory_id:
            raise ValueError("self relations are not allowed")
        endpoints = list(
            self.conn.execute(
                "SELECT id,project_id FROM memories WHERE id IN (?,?)",
                (from_memory_id, to_memory_id),
            )
        )
        if len(endpoints) != 2 or any(
            row["project_id"] != project_id for row in endpoints
        ):
            raise ValueError(
                "relation endpoints must be memories in the same project"
            )
        item = {
            "id": uid(),
            "project_id": project_id,
            "from_memory_id": from_memory_id,
            "to_memory_id": to_memory_id,
            "relation": relation,
            "note": note,
            "created_at": now(),
        }
        with self.tx() as cx:
            cx.execute(
                "INSERT INTO edges"
                " VALUES(:id,:project_id,:from_memory_id,:to_memory_id,"
                ":relation,:note,:created_at)",
                item,
            )
            self._audit(cx, project_id, "edge", item["id"], "created", item)
        return item

    def traverse(
        self,
        project_id: str,
        memory_id: str,
        max_depth: int = 2,
        direction: str = "both",
        relations: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> dict[str, Any]:
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValueError("invalid direction")
        if not 1 <= max_depth <= 5:
            raise ValueError("max_depth must be 1..5")
        if relations and any(value not in RELATIONS for value in relations):
            raise ValueError("invalid relation filter")
        allowed_statuses = statuses or ["active", "disputed"]
        start = self._row(
            "SELECT * FROM memories WHERE id=? AND project_id=?",
            (memory_id, project_id),
        )
        if not start:
            raise KeyError("memory not found")
        nodes = {memory_id: {**start, "depth": 0}}
        selected_edges = []
        frontier = {memory_id}
        for depth in range(1, max_depth + 1):
            next_frontier = set()
            for current in frontier:
                clauses = []
                args = []
                if direction in {"outgoing", "both"}:
                    clauses.append("from_memory_id=?")
                    args.append(current)
                if direction in {"incoming", "both"}:
                    clauses.append("to_memory_id=?")
                    args.append(current)
                sql = (
                    "SELECT * FROM edges WHERE project_id=? AND ("
                    + " OR ".join(clauses)
                    + ")"
                )
                params = [project_id, *args]
                if relations:
                    sql += (
                        " AND relation IN ("
                        + ",".join("?" for _ in relations)
                        + ")"
                    )
                    params.extend(relations)
                for edge_row in self.conn.execute(sql, params):
                    edge = dict(edge_row)
                    other = (
                        edge["to_memory_id"]
                        if edge["from_memory_id"] == current
                        else edge["from_memory_id"]
                    )
                    node = self._row(
                        "SELECT * FROM memories WHERE id=? AND project_id=?",
                        (other, project_id),
                    )
                    if not node or node["status"] not in allowed_statuses:
                        continue
                    if edge["id"] not in {e["id"] for e in selected_edges}:
                        selected_edges.append(edge)
                    if other not in nodes:
                        nodes[other] = {**node, "depth": depth}
                        next_frontier.add(other)
            frontier = next_frontier
            if not frontier:
                break
        return {
            "start_memory_id": memory_id,
            "max_depth": max_depth,
            "direction": direction,
            "nodes": sorted(
                nodes.values(), key=lambda x: (x["depth"], x["id"])
            ),
            "edges": selected_edges,
        }

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
            row = self._row(
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
        timestamp = now()
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
            for r in self.conn.execute(
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
                for r in self.conn.execute(
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
        if self.embedding_provider:
            query_vector = self.embedding_provider.embed([query])[0]
            vector_only_threshold = getattr(
                self.embedding_provider, "vector_only_threshold", None
            )
            supplements_lexical = bool(
                getattr(
                    self.embedding_provider,
                    "supplements_lexical_results",
                    False,
                )
            )
            discovery_project_ids: list[str] | None = None
            sem_boundary = boundary
            if discover_projects:
                discovery_project_ids = self._discovery_project_candidates(
                    project_id, query_tokens, lexical
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
                self._provider_name(),
                self.embedding_provider.dimensions,
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
            for row in self.conn.execute(sem_sql, sem_args):
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
                        for row in self.conn.execute(
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
                for row in self.conn.execute(
                    "SELECT * FROM memory_usage WHERE memory_id IN"
                    f" ({candidate_placeholders})",
                    candidate_ids,
                )
            }
            for source in self.conn.execute(
                f"""SELECT s.memory_id,e.id,e.kind,e.source_uri,e.created_at
              FROM memory_sources s JOIN events e ON e.id=s.event_id
              WHERE s.memory_id IN ({candidate_placeholders})
              ORDER BY s.memory_id,e.id""",
                candidate_ids,
            ):
                item = dict(source)
                sources[item.pop("memory_id")].append(item)
        current = current_datetime()
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
                "embedding_provider": self._provider_name(),
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

    def _aggregate_project_candidates(
        self, memories: list[dict[str, Any]], current_project_id: str
    ) -> list[dict[str, Any]]:
        """Aggregate database relevance and recent project activity."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for memory in memories:
            if (
                memory["project_id"] != current_project_id
                and memory["visibility"] == "project"
            ):
                grouped.setdefault(memory["project_id"], []).append(memory)
        candidates = []
        source_aliases = {
            (row["kind"], row["normalized"])
            for row in self.conn.execute(
                "SELECT kind,normalized FROM project_aliases WHERE"
                " project_id=?",
                (current_project_id,),
            )
        }
        current = current_datetime()
        for candidate_id, matches in grouped.items():
            project = self._row(
                "SELECT id,slug,name,description FROM projects WHERE id=?",
                (candidate_id,),
            )
            activity = self._row(
                """SELECT MAX(activity_at) AS activity_at FROM (
              SELECT MAX(COALESCE(ended_at,started_at)) AS activity_at
              FROM sessions WHERE project_id=?
              UNION ALL SELECT MAX(created_at) FROM events WHERE project_id=?
            )""",
                (candidate_id, candidate_id),
            )
            checkpoint = self._row(
                """SELECT id,title,type,status,updated_at FROM memories
              WHERE project_id=? AND status IN ('active','disputed')
              AND type IN ('task','summary')
              ORDER BY updated_at DESC,id LIMIT 1""",
                (candidate_id,),
            )
            # Hybrid RRF should improve ordering within a project, but
            # counting
            # the same hit twice would dilute path/name priors during
            # project
            # selection. Collapse the overlapping lexical/vector
            # contribution.
            relevance_scores = sorted(
                (
                    m["retrieval"]["score"]
                    - min(
                        m["retrieval"]["components"].get("lexical_rrf", 0.0),
                        m["retrieval"]["components"].get("semantic_rrf", 0.0),
                    )
                    for m in matches
                ),
                reverse=True,
            )
            relevance = sum(
                score / (index + 1)
                for index, score in enumerate(relevance_scores)
            )
            evidence_quality = max(
                max(
                    m["retrieval"].get("query_coverage", 0.0),
                    m["retrieval"].get("semantic_similarity") or 0.0,
                )
                for m in matches
            )
            activity_at = activity["activity_at"] if activity else None
            try:
                age_days = (
                    max(
                        0.0,
                        (
                            current - datetime.fromisoformat(activity_at)
                        ).total_seconds()
                        / 86400,
                    )
                    if activity_at
                    else None
                )
            except ValueError:
                age_days = None
            recency = (
                0.0 if age_days is None else 1.0 / (1.0 + age_days / 30.0)
            )
            candidate_aliases = {
                (row["kind"], row["normalized"])
                for row in self.conn.execute(
                    "SELECT kind,normalized FROM project_aliases WHERE"
                    " project_id=?",
                    (candidate_id,),
                )
            }
            shared_aliases = source_aliases & candidate_aliases
            identity_prior = (
                0.35
                if any(kind == "path" for kind, _ in shared_aliases)
                else (
                    0.15
                    if any(kind == "name" for kind, _ in shared_aliases)
                    else 0.0
                )
            )
            # A single strong lexical/local-vector hit is approximately
            # 1/61.
            # Normalize the aggregate before adding bounded identity and
            # activity
            # priors so registry size and raw RRF scale do not leak into
            # confidence.
            relevance_confidence = (
                min(1.0, relevance / 0.02) * evidence_quality
            )
            confidence = min(
                1.0,
                relevance_confidence * 0.75 + identity_prior + recency * 0.05,
            )
            reasons = ["memory_relevance"]
            if identity_prior:
                reasons.append(
                    "shared_path" if identity_prior == 0.35 else "shared_name"
                )
            if recency:
                reasons.append("recent_activity")
            candidates.append(
                {
                    **project,
                    "relevance": relevance,
                    "matching_memory_count": len(matches),
                    "top_memory_score": relevance_scores[0],
                    "recent_activity_at": activity_at,
                    "recency": recency,
                    "identity_prior": identity_prior,
                    "evidence_quality": evidence_quality,
                    "confidence": confidence,
                    "confidence_reasons": reasons,
                    "latest_checkpoint": checkpoint,
                }
            )
        return sorted(
            candidates,
            key=lambda item: (
                -item["confidence"],
                -item["relevance"],
                item["id"],
            ),
        )

    @staticmethod
    def _select_project_candidate(
        candidates: list[dict[str, Any]],
    ) -> tuple[str | None, str, float]:
        return select_project_candidate(candidates)

    def record_memory_feedback(
        self, memory_id: str, signal: str
    ) -> dict[str, Any]:
        if signal not in {"retrieved", "used", "helpful", "incorrect"}:
            raise ValueError(
                "signal must be retrieved, used, helpful, or incorrect"
            )
        memory = self.memories.get(memory_id)
        if not memory:
            raise KeyError("memory not found")
        ts = now()
        column = signal + "_count"
        with self.tx() as cx:
            cx.execute(
                """INSERT OR IGNORE INTO memory_usage(
                memory_id,updated_at) VALUES(?,?)""",
                (memory_id, ts),
            )
            updates = f"{column}={column}+1,updated_at=?"
            if signal == "retrieved":
                updates += ",last_retrieved_at=?"
            if signal == "used":
                updates += ",last_used_at=?"
            values: list[Any] = [ts]
            if signal in {"retrieved", "used"}:
                values.append(ts)
            values.append(memory_id)
            cx.execute(
                f"UPDATE memory_usage SET {updates} WHERE memory_id=?", values
            )
            result = dict(
                cx.execute(
                    "SELECT * FROM memory_usage WHERE memory_id=?",
                    (memory_id,),
                ).fetchone()
            )
            delta = {"used": 0.005, "helpful": 0.02, "incorrect": -0.05}.get(
                signal, 0.0
            )
            if delta:
                cx.execute(
                    "UPDATE memories SET"
                    " importance=max(0,min(1,importance+?)),updated_at=? WHERE"
                    " id=?",
                    (delta, ts, memory_id),
                )
                result["importance"] = cx.execute(
                    "SELECT importance FROM memories WHERE id=?", (memory_id,)
                ).fetchone()[0]
            self._audit(
                cx,
                memory["project_id"],
                "memory_feedback",
                memory_id,
                signal,
                result,
            )
        return result

    @staticmethod
    def _retrieval_gate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
        return retrieval_gate(candidates)

    def get_context(
        self,
        project_id: str,
        query: str,
        char_budget: int = 6000,
        statuses: list[str] | None = None,
        scope_id: str | None = None,
        event_cursor: int | None = None,
        event_kinds: list[str] | None = None,
        event_limit: int = 20,
        event_char_budget: int = 2000,
        discover_projects: bool = True,
        response_format: str = "legacy",
    ) -> dict[str, Any]:
        if response_format not in {"legacy", "compact"}:
            raise ValueError("response_format must be legacy or compact")
        policy = self.get_policy(project_id)
        requested = max(0, char_budget)
        budget = min(requested, policy["max_context_chars"])
        selected, used = [], 0
        recent_events: list[dict[str, Any]] = []
        event_used = 0
        event_result = None
        reserved = 0
        if event_cursor is not None:
            reserved = min(max(0, event_char_budget), 4000, budget)
            selected_kinds = (
                ["message"] if event_kinds is None else event_kinds
            )
            event_result = self.read_events_since(
                project_id, event_cursor, selected_kinds, scope_id, event_limit
            )
            for event in event_result["events"]:
                prefix = f"[{event['event_seq']}/{event['kind']}] "
                remaining = reserved - event_used - len(prefix)
                if remaining <= 0:
                    break
                content = event["content"]
                truncated = len(content) > remaining
                text = prefix + (
                    content[: max(0, remaining - 1)] + "…"
                    if truncated and remaining
                    else content
                )
                recent_events.append(
                    {
                        "event_id": event["id"],
                        "event_seq": event["event_seq"],
                        "kind": event["kind"],
                        "text": text,
                        "created_at": event["created_at"],
                        "session_id": event["session_id"],
                        "scope_id": event["scope_id"],
                        "metadata": event["metadata"],
                        "content_truncated": truncated,
                    }
                )
                event_used += len(text)
            fully_consumed = len(recent_events) == len(event_result["events"])
            event_result["next_cursor"] = (
                event_result["next_cursor"]
                if fully_consumed
                else (
                    recent_events[-1]["event_seq"]
                    if recent_events
                    else event_cursor
                )
            )
            event_result["has_more"] = (
                event_result["has_more"] or not fully_consumed
            )
        memory_budget = budget - event_used
        selected_texts: list[str] = []
        candidates = self.search(
            project_id,
            query,
            policy["max_context_items"] * 3,
            statuses or ["active", "disputed"],
            scope_id,
        )
        retrieval_gate = self._retrieval_gate(candidates)
        if retrieval_gate["status"] == "no_confident_match":
            candidates = []
        local_matches = [
            m for m in candidates if m["project_id"] == project_id
        ]
        discovery_used = bool(discover_projects and not local_matches)
        discovery_candidates: list[dict[str, Any]] = []
        if discovery_used:
            discovery_candidates = self.search(
                project_id,
                query,
                policy["max_context_items"] * 3,
                statuses or ["active", "disputed"],
                None,
                True,
            )
            discovery_gate = self._retrieval_gate(discovery_candidates)
            if discovery_gate["status"] == "no_confident_match":
                discovery_candidates = []
            seen = {m["id"] for m in candidates}
            candidates.extend(
                m for m in discovery_candidates if m["id"] not in seen
            )
        project_candidates = self._aggregate_project_candidates(
            discovery_candidates, project_id
        )
        selected_project_id, selection_reason, discovery_confidence = (
            self._select_project_candidate(project_candidates)
        )
        discovery_ambiguous = selection_reason == "ambiguous_candidates"
        if discovery_used:
            candidates = [
                m
                for m in candidates
                if m["project_id"] == project_id
                or m["visibility"] == "global"
                or m["project_id"] == selected_project_id
            ]
        eligible = 0
        for m in candidates:
            block = (
                f"[{m['status']}/{m['type']}]"
                f" {m['title']}\n{m['content']}\nsource_events:"
                f" {', '.join(s['id'] for s in m['sources']) or 'none'}"
            )
            comparable = f"{m['title']} {m['content']}"
            if any(
                self._text_similarity(comparable, previous) >= 0.8
                for previous in selected_texts
            ):
                continue
            eligible += 1
            if len(selected) >= policy["max_context_items"]:
                continue
            if used + len(block) + 2 > memory_budget:
                continue
            item = {
                "memory_id": m["id"],
                "project_id": m["project_id"],
                "visibility": m["visibility"],
                "confidence": m["confidence"],
                "importance": m["importance"],
            }
            if response_format == "legacy":
                item["text"] = block
            else:
                item.update(
                    {
                        "status": m["status"],
                        "type": m["type"],
                        "title": m["title"],
                        "content": m["content"],
                        "source_event_ids": [s["id"] for s in m["sources"]],
                        "tags": json.loads(m["tags_json"]),
                        "observed_at": m["observed_at"],
                        "valid_from": m["valid_from"],
                        "valid_until": m["valid_until"],
                        "last_confirmed_at": m["last_confirmed_at"],
                        "truncated": False,
                    }
                )
            selected.append(item)
            selected_texts.append(comparable)
            used += len(block) + 2
        result = {
            "query": query,
            "requested_budget": requested,
            "budget": budget,
            "budget_capped": requested > budget,
            "max_items": policy["max_context_items"],
            "memory_budget": memory_budget,
            "event_budget": reserved,
            "used": used + event_used,
            "memory_used": used,
            "event_used": event_used,
            "items": selected,
            "recent_events": recent_events,
            "retrieval_gate": retrieval_gate,
            "project_discovery": {
                "enabled": discover_projects,
                "used": discovery_used,
                "ambiguous": discovery_ambiguous,
                "project_ids": list(
                    dict.fromkeys(
                        i["project_id"]
                        for i in selected
                        if i["project_id"] != project_id
                    )
                ),
                "selected_project_id": selected_project_id,
                "confidence": discovery_confidence,
                "selection_reason": selection_reason,
                "candidates": project_candidates,
            },
            "event_cursor": event_cursor,
            "next_event_cursor": (
                event_result["next_cursor"] if event_result else None
            ),
            "event_snapshot_cursor": (
                event_result["snapshot_cursor"] if event_result else None
            ),
            "has_more_events": (
                event_result["has_more"] if event_result else False
            ),
            "response_format": response_format,
            "truncated": eligible > len(selected),
            "has_more": eligible > len(selected),
        }
        if response_format == "legacy":
            result["context"] = "\n\n".join(i["text"] for i in selected)
        return result

    def decision_context(
        self,
        project_id: str,
        question: str,
        char_budget: int = 6000,
        scope_id: str | None = None,
        discover_projects: bool = True,
    ) -> dict[str, Any]:
        """Compose a cited Decision Brief from existing retrieval."""
        context = self.get_context(
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

    @staticmethod
    def _rerank_decision_candidates(
        question: str, memories: list[dict[str, Any]]
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
        current = current_datetime()
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
        for edge in self.conn.execute(
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
        for row in self.conn.execute(
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
            timestamp = now()
            params: list[Any] = [*remaining_ids, timestamp, timestamp]
            if scope_id is not None:
                params.append(scope_id)
            rows = {
                row["id"]: dict(row)
                for row in self.conn.execute(
                    f"""SELECT * FROM memories
                WHERE id IN ({remaining_placeholders})
                AND (valid_from IS NULL OR valid_from<=?)
                AND (valid_until IS NULL OR valid_until>?)
                {scope_clause}""",
                    params,
                )
            }
            for source in self.conn.execute(
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
        rows = self.conn.execute(
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

    def get_policy(self, project_id: str) -> dict[str, Any]:
        item = self.maintenance.get_policy(project_id)
        if not item:
            raise KeyError("project not found")
        return item

    def set_policy(
        self,
        project_id: str,
        max_context_chars: int | None = None,
        max_context_items: int | None = None,
        audit_keep_entries: int | None = None,
        terminal_memory_days: int | None = None,
        checkpoint_soft_usage: float | None = None,
        checkpoint_hard_usage: float | None = None,
        checkpoint_elapsed_seconds: int | None = None,
        checkpoint_event_count: int | None = None,
        checkpoint_max_age_seconds: int | None = None,
        checkpoint_cooldown_seconds: int | None = None,
        checkpoint_hysteresis: float | None = None,
        maintenance_interval_seconds: int | None = None,
        message_ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        current = self.get_policy(project_id)
        values = {
            "max_context_chars": max_context_chars,
            "max_context_items": max_context_items,
            "audit_keep_entries": audit_keep_entries,
            "terminal_memory_days": terminal_memory_days,
            "checkpoint_soft_usage": checkpoint_soft_usage,
            "checkpoint_hard_usage": checkpoint_hard_usage,
            "checkpoint_elapsed_seconds": checkpoint_elapsed_seconds,
            "checkpoint_event_count": checkpoint_event_count,
            "checkpoint_max_age_seconds": checkpoint_max_age_seconds,
            "checkpoint_cooldown_seconds": checkpoint_cooldown_seconds,
            "checkpoint_hysteresis": checkpoint_hysteresis,
            "maintenance_interval_seconds": maintenance_interval_seconds,
            "message_ttl_seconds": message_ttl_seconds,
        }
        limits = {
            "max_context_chars": (1000, 20000),
            "max_context_items": (1, 50),
            "audit_keep_entries": (100, 100000),
            "terminal_memory_days": (1, 3650),
            "checkpoint_soft_usage": (0, 1),
            "checkpoint_hard_usage": (0, 1),
            "checkpoint_elapsed_seconds": (60, 86400),
            "checkpoint_event_count": (1, 10000),
            "checkpoint_max_age_seconds": (60, 604800),
        }
        limits.update(
            {
                "checkpoint_cooldown_seconds": (0, 86400),
                "checkpoint_hysteresis": (0, 0.5),
                "message_ttl_seconds": (0, 2592000),
            }
        )
        for key, value in values.items():
            if value is not None:
                if key == "maintenance_interval_seconds":
                    if value != 0 and not 300 <= value <= 2592000:
                        raise ValueError(f"{key} must be 0 or 300..2592000")
                else:
                    low, high = limits[key]
                    if not low <= value <= high:
                        raise ValueError(f"{key} must be {low}..{high}")
                current[key] = value
        if (
            current["checkpoint_soft_usage"]
            >= current["checkpoint_hard_usage"]
        ):
            raise ValueError(
                "checkpoint_soft_usage must be less than checkpoint_hard_usage"
            )
        current["updated_at"] = now()
        with self.tx() as cx:
            cx.execute(
                """UPDATE project_policies SET
              max_context_chars=:max_context_chars,
              max_context_items=:max_context_items,
              audit_keep_entries=:audit_keep_entries,
              terminal_memory_days=:terminal_memory_days,
              checkpoint_soft_usage=:checkpoint_soft_usage,
              checkpoint_hard_usage=:checkpoint_hard_usage,
              checkpoint_elapsed_seconds=:checkpoint_elapsed_seconds,
              checkpoint_event_count=:checkpoint_event_count,
              checkpoint_max_age_seconds=:checkpoint_max_age_seconds,
              checkpoint_cooldown_seconds=:checkpoint_cooldown_seconds,
              checkpoint_hysteresis=:checkpoint_hysteresis,
              maintenance_interval_seconds=:maintenance_interval_seconds,
              message_ttl_seconds=:message_ttl_seconds,
              updated_at=:updated_at WHERE project_id=:project_id""",
                current,
            )
            self._audit(
                cx, project_id, "policy", project_id, "updated", current
            )
        return current

    def search_health(self, project_id: str) -> dict[str, Any]:
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError("project not found")
        memories = self.conn.execute(
            "SELECT count(*) FROM memories WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        indexed = self.conn.execute(
            "SELECT count(*) FROM memories_fts f JOIN memories m ON"
            " m.id=f.memory_id WHERE m.project_id=?",
            (project_id,),
        ).fetchone()[0]
        missing = self.conn.execute(
            "SELECT count(*) FROM memories m WHERE m.project_id=? AND NOT"
            " EXISTS(SELECT 1 FROM memories_fts f WHERE f.memory_id=m.id)",
            (project_id,),
        ).fetchone()[0]
        duplicate = self.conn.execute(
            """SELECT count(*) FROM (SELECT f.memory_id
          FROM memories_fts f JOIN memories m ON m.id=f.memory_id
          WHERE m.project_id=? GROUP BY f.memory_id HAVING count(*)<>1)""",
            (project_id,),
        ).fetchone()[0]
        orphan = self.conn.execute(
            "SELECT count(*) FROM memories_fts f LEFT JOIN memories m ON"
            " m.id=f.memory_id WHERE m.id IS NULL"
        ).fetchone()[0]
        embedding = {
            "enabled": bool(self.embedding_provider),
            "provider": self._provider_name(),
            "indexed_rows": 0,
            "missing": 0,
            "stale": 0,
        }
        if self.embedding_provider:
            embedding["indexed_rows"] = self.conn.execute(
                """SELECT count(*) FROM memory_embeddings e
              JOIN memories m ON m.id=e.memory_id
              WHERE m.project_id=? AND e.provider=? AND e.dimensions=?""",
                (
                    project_id,
                    self._provider_name(),
                    self.embedding_provider.dimensions,
                ),
            ).fetchone()[0]
            embedding["missing"] = memories - embedding["indexed_rows"]
            for row in self.conn.execute(
                """SELECT m.*,e.content_hash FROM memories m
              JOIN memory_embeddings e ON e.memory_id=m.id
              WHERE m.project_id=? AND e.provider=?""",
                (project_id, self._provider_name()),
            ):
                tags = " ".join(json.loads(row["tags_json"]))
                text = f"{row['title']}\n{row['content']}\n{tags}"
                if (
                    hashlib.sha256(text.encode()).hexdigest()
                    != row["content_hash"]
                ):
                    embedding["stale"] += 1
        ok = (
            missing == 0
            and duplicate == 0
            and orphan == 0
            and indexed == memories
            and (
                not self.embedding_provider
                or (embedding["missing"] == 0 and embedding["stale"] == 0)
            )
        )
        return {
            "ok": ok,
            "project_id": project_id,
            "memories": memories,
            "indexed_rows": indexed,
            "missing": missing,
            "duplicate_memory_ids": duplicate,
            "orphan_rows": orphan,
            "embeddings": embedding,
        }

    def get_source(self, event_id: str) -> dict[str, Any]:
        item = self.project_evidence.get_event(event_id)
        if not item:
            raise KeyError("source event not found")
        return item

    def maintain(self, project_id: str, apply: bool = False) -> dict[str, Any]:
        """Bound state while preserving events and audit detail."""
        policy = self.get_policy(project_id)
        cutoff = (
            current_datetime() - timedelta(days=policy["terminal_memory_days"])
        ).isoformat()
        terminal = [
            dict(row)
            for row in self.conn.execute(
                """SELECT * FROM memories WHERE project_id=?
          AND status IN ('superseded','rejected','expired')
          AND updated_at<? ORDER BY updated_at,id""",
                (project_id, cutoff),
            )
        ]
        audit_total = self.conn.execute(
            "SELECT count(*) FROM audit_log WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        # Purge audit records only after accounting for one purge audit
        # entry
        # per terminal memory.
        projected_total = audit_total + len(terminal)
        prune_count = max(0, projected_total - policy["audit_keep_entries"])
        plan = {
            "project_id": project_id,
            "apply": apply,
            "policy": policy,
            "terminal_cutoff": cutoff,
            "terminal_memories": len(terminal),
            "audit_entries": audit_total,
            "audit_entries_to_checkpoint": prune_count,
        }
        if not apply:
            return plan
        checkpoint = None
        with self.tx() as cx:
            for memory in terminal:
                sources = [
                    row[0]
                    for row in cx.execute(
                        "SELECT event_id FROM memory_sources WHERE memory_id=?"
                        " ORDER BY event_id",
                        (memory["id"],),
                    )
                ]
                self._audit(
                    cx,
                    project_id,
                    "memory",
                    memory["id"],
                    "purged_terminal",
                    {**memory, "source_event_ids": sources},
                )
                cx.execute("DELETE FROM memories WHERE id=?", (memory["id"],))
            total = cx.execute(
                "SELECT count(*) FROM audit_log WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            prune_count = max(0, total - policy["audit_keep_entries"])
            if prune_count:
                rows = [
                    dict(row)
                    for row in cx.execute(
                        "SELECT * FROM audit_log WHERE project_id=? ORDER BY"
                        " seq LIMIT ?",
                        (project_id, prune_count),
                    )
                ]
                previous = cx.execute(
                    "SELECT digest FROM audit_checkpoints WHERE project_id=?"
                    " ORDER BY through_seq DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
                previous_digest = previous[0] if previous else None
                checkpoint = build_audit_checkpoint(
                    project_id,
                    rows,
                    previous_digest,
                    checkpoint_id=uid(),
                    created_at=now(),
                )
                cx.execute(
                    "INSERT INTO audit_checkpoints"
                    " VALUES(:id,:project_id,:from_seq,:through_seq,"
                    ":entry_count,:previous_digest,:digest,:created_at)",
                    checkpoint,
                )
                cx.execute(
                    "UPDATE maintenance_control SET audit_prune_enabled=1"
                    " WHERE id=1"
                )
                cx.execute(
                    "DELETE FROM audit_log WHERE project_id=? AND seq<=?",
                    (project_id, rows[-1]["seq"]),
                )
                cx.execute(
                    "UPDATE maintenance_control SET audit_prune_enabled=0"
                    " WHERE id=1"
                )
        return {
            **plan,
            "terminal_memories_purged": len(terminal),
            "audit_entries_checkpointed": prune_count,
            "checkpoint": checkpoint,
        }

    def maintenance_status(self, project_id: str) -> dict[str, Any]:
        policy = self.get_policy(project_id)
        counts = {
            "events": (
                self.conn.execute(
                    "SELECT count(*) FROM events WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
            ),
            "memories": (
                self.conn.execute(
                    "SELECT count(*) FROM memories WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
            ),
            "terminal_memories": (
                self.conn.execute(
                    "SELECT count(*) FROM memories WHERE project_id=? AND"
                    " status IN ('superseded','rejected','expired')",
                    (project_id,),
                ).fetchone()[0]
            ),
            "audit_entries": (
                self.conn.execute(
                    "SELECT count(*) FROM audit_log WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
            ),
        }
        checkpoints = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM audit_checkpoints WHERE project_id=? ORDER BY"
                " through_seq",
                (project_id,),
            )
        ]
        schedule = self._row(
            "SELECT * FROM maintenance_runs WHERE project_id=?", (project_id,)
        )
        return {
            "project_id": project_id,
            "policy": policy,
            "counts": counts,
            "audit_checkpoints": checkpoints,
            "schedule": schedule,
            "search": self.search_health(project_id),
        }

    def export_audit_chain(self, project_id: str) -> dict[str, Any]:
        """Return a bundle for offline audit-chain verification."""
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError("project not found")
        checkpoints = self.maintenance.audit_checkpoints(project_id)
        entries = self.maintenance.audit_entries(project_id)
        return serialize_audit_chain(project_id, checkpoints, entries)

    @staticmethod
    def verify_audit_chain(
        bundle: dict[str, Any], expected_head_digest: str | None = None
    ) -> dict[str, Any]:
        """Verify an exported chain without opening its source database.

        Checkpoint digests commit to compacted audit rows that are
        intentionally absent. A separately recorded head
        digest anchors the exported chain and
        detects replacement of the bundle as a whole.
        """
        return verify_audit_chain_bundle(bundle, expected_head_digest)

    def maintain_scheduled(self, project_id: str) -> dict[str, Any]:
        """Run maintenance once when its persisted interval is due."""
        policy = self.get_policy(project_id)
        interval = policy["maintenance_interval_seconds"]
        if not interval:
            return {
                "project_id": project_id,
                "scheduled": True,
                "ran": False,
                "reason": "disabled",
            }
        ts = now()
        with self.tx() as cx:
            state = dict(
                cx.execute(
                    "SELECT * FROM maintenance_runs WHERE project_id=?",
                    (project_id,),
                ).fetchone()
            )
            baseline = state["last_completed_at"] or state["last_started_at"]
            if baseline and datetime.fromisoformat(baseline) + timedelta(
                seconds=interval
            ) > datetime.fromisoformat(ts):
                return {
                    "project_id": project_id,
                    "scheduled": True,
                    "ran": False,
                    "reason": "not_due",
                    "next_due_at": (
                        (
                            datetime.fromisoformat(baseline)
                            + timedelta(seconds=interval)
                        ).isoformat()
                    ),
                }
            cx.execute(
                "UPDATE maintenance_runs SET last_started_at=?,last_error=NULL"
                " WHERE project_id=?",
                (ts, project_id),
            )
        try:
            result = self.maintain(project_id, True)
        except Exception as exc:
            self.conn.execute(
                "UPDATE maintenance_runs SET last_error=? WHERE project_id=?",
                (str(exc), project_id),
            )
            raise
        completed = now()
        self.conn.execute(
            "UPDATE maintenance_runs SET last_completed_at=?,last_error=NULL"
            " WHERE project_id=?",
            (completed, project_id),
        )
        return {
            **result,
            "scheduled": True,
            "ran": True,
            "completed_at": completed,
        }

    def backup_to(
        self, output_path: str | Path, encryption_passphrase: str | None = None
    ) -> dict[str, Any]:
        """Create a SQLite snapshot, including committed WAL data."""
        destination = Path(output_path).expanduser().resolve()
        if destination == self.path:
            raise ValueError(
                "backup output must differ from the live database"
            )
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(f".{destination.name}.{uid()}.tmp")
        target = sqlite3.connect(temporary)
        try:
            self.conn.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(
                    f"backup integrity check failed: {integrity}"
                )
        except Exception:
            target.close()
            temporary.unlink(missing_ok=True)
            raise
        else:
            target.close()
        os.chmod(temporary, 0o600)
        encryption = {"encrypted": False}
        if encryption_passphrase is not None:
            from .backup_crypto import encrypt_file

            plaintext = temporary
            encrypted = temporary.with_suffix(temporary.suffix + ".enc")
            try:
                encryption = encrypt_file(
                    plaintext, encrypted, encryption_passphrase
                )
                os.chmod(encrypted, 0o600)
                temporary = encrypted
            except Exception:
                encrypted.unlink(missing_ok=True)
                raise
            finally:
                plaintext.unlink(missing_ok=True)
        os.replace(temporary, destination)
        digest = hashlib.sha256()
        with destination.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "ok": True,
            "source": str(self.path),
            "output": str(destination),
            "bytes": destination.stat().st_size,
            "sha256": digest.hexdigest(),
            "created_at": now(),
            "integrity": "ok",
            **encryption,
        }

    def export_project(self, project_id: str) -> list[dict[str, Any]]:
        """Return a portable snapshot without SQLite internals."""
        project = self._row("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise KeyError("project not found")
        records: list[dict[str, Any]] = [
            {"record_type": "project", "data": project}
        ]
        queries = [
            (
                "scope",
                (
                    "SELECT * FROM scopes WHERE project_id=? ORDER BY"
                    " created_at,id"
                ),
            ),
            (
                "session",
                (
                    "SELECT * FROM sessions WHERE project_id=? ORDER BY"
                    " started_at,id"
                ),
            ),
            (
                "event",
                "SELECT * FROM events WHERE project_id=? ORDER BY event_seq",
            ),
            (
                "memory",
                (
                    "SELECT * FROM memories WHERE project_id=? ORDER BY"
                    " created_at,id"
                ),
            ),
            (
                "memory_source",
                (
                    "SELECT ms.* FROM memory_sources ms JOIN memories m ON"
                    " m.id=ms.memory_id WHERE m.project_id=? ORDER BY"
                    " ms.created_at,ms.memory_id,ms.event_id"
                ),
            ),
            (
                "investigation",
                (
                    "SELECT * FROM investigations WHERE project_id=? ORDER BY"
                    " started_at,id"
                ),
            ),
            (
                "source_analysis",
                (
                    "SELECT s.* FROM source_analyses s JOIN investigations i"
                    " ON i.id=s.investigation_id WHERE i.project_id=? ORDER BY"
                    " s.created_at,s.id"
                ),
            ),
            (
                "source_reinspection_request",
                (
                    "SELECT r.* FROM source_reinspection_requests r JOIN"
                    " source_analyses s ON s.id=r.source_analysis_id JOIN"
                    " investigations i ON i.id=s.investigation_id WHERE"
                    " i.project_id=? ORDER BY r.requested_at,r.id"
                ),
            ),
            (
                "investigation_claim",
                (
                    "SELECT c.* FROM investigation_claims c JOIN"
                    " investigations i ON i.id=c.investigation_id WHERE"
                    " i.project_id=? ORDER BY"
                    " c.created_at,c.source_analysis_id,c.ordinal"
                ),
            ),
            (
                "investigation_claim_link",
                (
                    "SELECT l.* FROM investigation_claim_links l JOIN"
                    " investigation_claims c ON c.id=l.from_claim_id JOIN"
                    " investigations i ON i.id=c.investigation_id WHERE"
                    " i.project_id=? ORDER BY"
                    " l.created_at,l.from_claim_id,l.to_claim_id"
                ),
            ),
            (
                "wiki_page",
                (
                    "SELECT * FROM wiki_pages WHERE project_id=? ORDER BY"
                    " created_at,id"
                ),
            ),
            (
                "wiki_revision",
                (
                    "SELECT r.* FROM wiki_revisions r JOIN wiki_pages p ON"
                    " p.id=r.page_id WHERE p.project_id=? ORDER BY"
                    " r.created_at,r.id"
                ),
            ),
            (
                "wiki_revision_citation",
                (
                    "SELECT c.* FROM wiki_revision_citations c JOIN"
                    " wiki_revisions r ON r.id=c.revision_id JOIN wiki_pages p"
                    " ON p.id=r.page_id WHERE p.project_id=? ORDER BY"
                    " c.revision_id,c.section_name,c.ordinal,c.memory_id,"
                    " c.event_id"
                ),
            ),
            (
                "memory_usage",
                (
                    "SELECT u.* FROM memory_usage u JOIN memories m ON"
                    " m.id=u.memory_id WHERE m.project_id=? ORDER BY"
                    " u.memory_id"
                ),
            ),
            (
                "review_conflict",
                (
                    "SELECT c.* FROM review_conflicts c JOIN memories m ON"
                    " m.id=c.candidate_memory_id WHERE m.project_id=? ORDER BY"
                    " c.created_at,c.candidate_memory_id,c.existing_memory_id"
                ),
            ),
            (
                "edge",
                (
                    "SELECT * FROM edges WHERE project_id=? ORDER BY"
                    " created_at,id"
                ),
            ),
            (
                "search_alias",
                (
                    "SELECT * FROM search_aliases WHERE project_id=? ORDER BY"
                    " term"
                ),
            ),
            (
                "project_alias",
                (
                    "SELECT * FROM project_aliases WHERE project_id=? ORDER BY"
                    " kind,normalized"
                ),
            ),
            (
                "event_receipt",
                (
                    "SELECT * FROM event_receipts WHERE project_id=? ORDER BY"
                    " consumer_id,scope_key,kinds_json"
                ),
            ),
            ("policy", "SELECT * FROM project_policies WHERE project_id=?"),
            (
                "audit_checkpoint",
                (
                    "SELECT * FROM audit_checkpoints WHERE project_id=? ORDER"
                    " BY through_seq"
                ),
            ),
            (
                "audit",
                "SELECT * FROM audit_log WHERE project_id=? ORDER BY seq",
            ),
        ]
        for record_type, sql in queries:
            records.extend(
                {"record_type": record_type, "data": dict(row)}
                for row in self.conn.execute(sql, (project_id,))
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
        if self._row(
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
        with self.tx() as cx:
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

    def rebuild_fts(self, project_id: str | None = None) -> dict[str, Any]:
        if project_id and not self._row(
            "SELECT id FROM projects WHERE id=?", (project_id,)
        ):
            raise KeyError("project not found")
        condition = " WHERE project_id=?" if project_id else ""
        args = (project_id,) if project_id else ()
        with self.tx() as cx:
            if project_id:
                ids = [
                    row[0]
                    for row in cx.execute(
                        "SELECT id FROM memories WHERE project_id=?", args
                    )
                ]
                if ids:
                    cx.execute(
                        "DELETE FROM memories_fts WHERE memory_id IN ("
                        + ",".join("?" for _ in ids)
                        + ")",
                        ids,
                    )
            else:
                cx.execute("DELETE FROM memories_fts")
            rows = list(
                cx.execute(
                    "SELECT id,title,content,tags_json FROM memories"
                    + condition,
                    args,
                )
            )
            for row in rows:
                cx.execute(
                    "INSERT INTO memories_fts(memory_id,title,content,tags)"
                    " VALUES(?,?,?,?)",
                    (
                        row["id"],
                        row["title"],
                        row["content"],
                        " ".join(json.loads(row["tags_json"])),
                    ),
                )
            if self.embedding_provider:
                memories = list(
                    cx.execute("SELECT * FROM memories" + condition, args)
                )
                for memory in memories:
                    self._index_embedding(cx, dict(memory))
        return {
            "ok": True,
            "project_id": project_id,
            "indexed_memories": len(rows),
            "embedding_provider": self._provider_name(),
            "embedded_memories": len(rows) if self.embedding_provider else 0,
        }

    def audit(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        return self.project_evidence.audit_entries(entity_type, entity_id)
