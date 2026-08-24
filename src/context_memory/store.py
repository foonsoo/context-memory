from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import retrieval, wiki_lint
from .audit_serialization import (
    serialize_audit_chain,
    verify_audit_chain_bundle,
)
from .context_assembly import ContextAssembler
from .contracts import (
    MEMORY_TYPES,
    PROMOTABLE_EVENT_KINDS,
)
from .decision_assembly import DecisionAssembler
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
    RetrievalRepository,
    TransferRepository,
    WikiRepository,
)
from .retrieval import (
    DISCOVERY_PROJECT_CANDIDATE_LIMIT,
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
LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT = (
    retrieval.LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT
)
LOCAL_HASH_FALLBACK_TIME_LIMIT_MS = retrieval.LOCAL_HASH_FALLBACK_TIME_LIMIT_MS
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
        self.retrieval_repository = RetrievalRepository(
            self, now, current_datetime
        )
        self.transfer = TransferRepository(self)
        self.checkpoints = CheckpointRepository(
            self, now, uid, current_datetime
        )
        self.context_assembler = ContextAssembler(self)
        self.decision_assembler = DecisionAssembler(
            self, now, current_datetime
        )
        self.investigations = InvestigationRepository(self, now, uid)
        self.maintenance = MaintenanceRepository(
            self, now, uid, current_datetime
        )
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
        return self.memories.set_search_aliases(project_id, term, aliases)

    def list_search_aliases(self, project_id: str) -> list[dict[str, Any]]:
        return self.memories.list_search_aliases(project_id)

    def create_relation(
        self,
        project_id: str,
        from_memory_id: str,
        to_memory_id: str,
        relation: str,
        note: str = "",
    ) -> dict[str, Any]:
        return self.memories.create_relation(
            project_id,
            from_memory_id,
            to_memory_id,
            relation,
            note,
        )

    def traverse(
        self,
        project_id: str,
        memory_id: str,
        max_depth: int = 2,
        direction: str = "both",
        relations: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.memories.traverse(
            project_id,
            memory_id,
            max_depth,
            direction,
            relations,
            statuses,
        )

    def search(
        self,
        project_id: str,
        query: str,
        limit: int = 10,
        statuses: list[str] | None = None,
        scope_id: str | None = None,
        discover_projects: bool = False,
    ) -> list[dict[str, Any]]:
        return self.retrieval_repository.search(
            project_id, query, limit, statuses, scope_id, discover_projects
        )

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
        return self.memories.record_feedback(memory_id, signal)

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
        return self.context_assembler.get_context(
            project_id,
            query,
            char_budget,
            statuses,
            scope_id,
            event_cursor,
            event_kinds,
            event_limit,
            event_char_budget,
            discover_projects,
            response_format,
        )

    def decision_context(
        self,
        project_id: str,
        question: str,
        char_budget: int = 6000,
        scope_id: str | None = None,
        discover_projects: bool = True,
    ) -> dict[str, Any]:
        return self.decision_assembler.decision_context(
            project_id,
            question,
            char_budget,
            scope_id,
            discover_projects,
        )

    def _rerank_decision_candidates(
        self, question: str, memories: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return self.decision_assembler._rerank_decision_candidates(
            question, memories
        )

    def _expand_decision_seeds(
        self, project_id: str, context: dict[str, Any], scope_id: str | None
    ) -> None:
        self.decision_assembler._expand_decision_seeds(
            project_id, context, scope_id
        )

    def _decision_outcome_comparisons(
        self, retrieved_memory_ids: list[str]
    ) -> list[dict[str, Any]]:
        return self.decision_assembler._decision_outcome_comparisons(
            retrieved_memory_ids
        )

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
        return self.maintenance.maintain(project_id, apply)

    def maintenance_status(self, project_id: str) -> dict[str, Any]:
        return self.maintenance.status(project_id)

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
        return self.maintenance.maintain_scheduled(project_id)

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
        return self.transfer.export_project(project_id)

    def import_project(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        return self.transfer.import_project(records)

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
