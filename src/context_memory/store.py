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

from .embeddings import EmbeddingProvider, LocalHashEmbedding, SentenceTransformerEmbedding
from .contracts import PROMOTABLE_EVENT_KINDS

TYPES = {"fact", "decision", "preference", "constraint", "procedure", "summary", "task", "other"}
STATUSES = {"proposed", "active", "superseded", "disputed", "expired", "rejected"}
RELATIONS = {"supersedes", "disputes", "supports", "depends_on", "related_to"}
INVESTIGATION_ROLES = {"evidence", "inference", "action", "decision", "rationale", "outcome"}
OUTCOME_EFFECTS = {"confirms", "weakens", "disputes", "supersedes"}
CHECKPOINT_MODES = {"interim", "final"}
CHECKPOINT_REASONS = {"context_budget", "elapsed", "material_change", "completed", "manual"}
DISCOVERY_MIN_CONFIDENCE = .45
DISCOVERY_AUTO_SELECT_CONFIDENCE = .60
DISCOVERY_MIN_MARGIN = .12
NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY = .30
NEGATIVE_VECTOR_ONLY_MIN_SEPARATION = .03
LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT = 1000
LOCAL_HASH_FALLBACK_TIME_LIMIT_MS = 25
DISCOVERY_PROJECT_CANDIDATE_LIMIT = 12
SOURCE_REINSPECTION_AGE_DAYS = 30


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class MemoryStore:
    def __init__(self, db_path: str | Path, embedding_provider: EmbeddingProvider | None = None):
        self.path = Path(db_path).expanduser().resolve()
        self._secure_directory()
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.migrate()
        configured_mode = os.environ.get("CONTEXT_MEMORY_EMBEDDINGS", "local-hash").strip()
        mode = configured_mode.casefold()
        embeddings_enabled = mode not in {"0", "false", "off", "disabled", "none"}
        if embedding_provider:
            self.embedding_provider = embedding_provider
        elif mode in {"neural", "sentence-transformers"}:
            model = os.environ.get("CONTEXT_MEMORY_EMBEDDING_MODEL", "").strip()
            if not model:
                raise ValueError(
                    "CONTEXT_MEMORY_EMBEDDING_MODEL is required when "
                    "CONTEXT_MEMORY_EMBEDDINGS=neural"
                )
            self.embedding_provider = SentenceTransformerEmbedding(
                model, device=os.environ.get("CONTEXT_MEMORY_EMBEDDING_DEVICE") or None
            )
        else:
            self.embedding_provider = LocalHashEmbedding() if embeddings_enabled else None
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
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        here = Path(__file__).resolve()
        roots = [here.parents[2] / "migrations", here.parents[1] / "migrations"]
        root = next((candidate for candidate in roots if candidate.is_dir()), roots[0])
        if not root.is_dir():
            raise RuntimeError("database migrations are missing from this installation")
        applied = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")}
        for file in sorted(root.glob("[0-9][0-9][0-9]_*.sql")):
            version = int(file.name.split("_", 1)[0])
            if version in applied:
                continue
            script = file.read_text(encoding="utf-8")
            # executescript owns its transaction; migration is recorded only after all statements succeed.
            self.conn.executescript("BEGIN IMMEDIATE;\n" + script + f"\nINSERT INTO schema_migrations VALUES({version},'{now()}');\nCOMMIT;")

    def _row(self, query: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        row = self.conn.execute(query, args).fetchone()
        return dict(row) if row else None

    def _audit(self, cx: sqlite3.Connection, project_id: str | None, kind: str, entity_id: str, action: str, snapshot: Any) -> None:
        cx.execute("INSERT INTO audit_log(project_id,entity_type,entity_id,action,snapshot_json,created_at) VALUES(?,?,?,?,?,?)",
                   (project_id, kind, entity_id, action, canonical(snapshot), now()))

    def _idem(self, operation: str, key: str | None, request: Any) -> dict[str, Any] | None:
        if not key:
            return None
        row = self._row("SELECT request_hash,response_json FROM idempotency_keys WHERE operation=? AND key=?", (operation, key))
        if not row:
            return None
        digest = hashlib.sha256(canonical(request).encode()).hexdigest()
        if digest != row["request_hash"]:
            raise ValueError("idempotency key reused with a different request")
        return json.loads(row["response_json"])

    def _save_idem(self, cx: sqlite3.Connection, operation: str, key: str | None, request: Any, response: Any) -> None:
        if key:
            cx.execute("INSERT INTO idempotency_keys VALUES(?,?,?,?,?)", (operation, key, hashlib.sha256(canonical(request).encode()).hexdigest(), canonical(response), now()))

    def create_project(self, slug: str, name: str | None = None, description: str = "", idempotency_key: str | None = None) -> dict[str, Any]:
        request = {"slug": slug, "name": name, "description": description}
        if hit := self._idem("create_project", idempotency_key, request): return hit
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", slug): raise ValueError("invalid project slug")
        item = {"id": uid(), "slug": slug, "name": name or slug, "description": description, "created_at": now()}
        with self.tx() as cx:
            cx.execute("INSERT INTO projects VALUES(:id,:slug,:name,:description,:created_at)", item)
            normalized_name = self._normalize_project_alias("name", item["name"])
            cx.execute("INSERT INTO project_aliases VALUES(?,?,?,?,?,?)",
                       (item["id"], "name", item["name"], normalized_name, item["created_at"], item["created_at"]))
            self._audit(cx, item["id"], "project", item["id"], "created", item)
            self._save_idem(cx, "create_project", idempotency_key, request, item)
        return item

    def list_projects(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM projects ORDER BY slug")]

    @staticmethod
    def _normalize_project_alias(kind: str, value: str) -> str:
        value = value.strip()
        if kind == "path": return str(Path(value).expanduser().resolve())
        return value.casefold()

    def set_project_alias(self, project_id: str, kind: str, value: str) -> dict[str, Any]:
        if kind not in {"path", "name"}: raise ValueError("invalid project alias kind")
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)): raise KeyError("project not found")
        normalized = self._normalize_project_alias(kind, value)
        if not normalized: raise ValueError("project alias cannot be empty")
        ts = now(); item = {"project_id":project_id,"kind":kind,"value":value,"normalized":normalized,"created_at":ts,"updated_at":ts}
        current = self._row("SELECT * FROM project_aliases WHERE project_id=? AND kind=? AND normalized=?",
                            (project_id, kind, normalized))
        if current and current["value"] == value: return current
        with self.tx() as cx:
            existing = cx.execute("SELECT created_at FROM project_aliases WHERE project_id=? AND kind=? AND normalized=?",
                                  (project_id, kind, normalized)).fetchone()
            if existing: item["created_at"] = existing["created_at"]
            cx.execute("""INSERT INTO project_aliases(project_id,kind,value,normalized,created_at,updated_at)
              VALUES(:project_id,:kind,:value,:normalized,:created_at,:updated_at)
              ON CONFLICT(project_id,kind,normalized) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""", item)
            self._audit(cx, project_id, "project_alias", f"{kind}:{normalized}", "updated" if existing else "created", item)
        return item

    def list_project_aliases(self, project_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM project_aliases WHERE project_id=? ORDER BY kind,normalized", (project_id,))]

    def _workspace_identities(self, path: str) -> dict[str, str]:
        return {"path":path, "name":Path(path).name}

    def _register_project_identities(self, project_id: str, identities: dict[str, str]) -> None:
        for kind, value in identities.items(): self.set_project_alias(project_id, kind, value)

    def _related_project_ids(self, project_id: str) -> list[str]:
        """Find registry projects sharing the hinted repository/workspace name."""
        rows = self.conn.execute("""SELECT DISTINCT candidate.project_id
          FROM project_aliases source JOIN project_aliases candidate
            ON candidate.kind=source.kind AND candidate.normalized=source.normalized
          WHERE source.project_id=? AND candidate.project_id<>? AND source.kind='name'
          ORDER BY candidate.project_id""", (project_id, project_id))
        return list(dict.fromkeys(row["project_id"] for row in rows))

    def _discovery_project_candidates(self, project_id: str, query_tokens: list[str],
                                      lexical: list[dict[str, Any]]) -> list[str]:
        """Bound discovery to projects supported by lexical or identity evidence."""
        ordered: list[str] = []

        def add(candidate_id: str) -> None:
            if candidate_id != project_id and candidate_id not in ordered:
                ordered.append(candidate_id)

        for memory in lexical:
            if memory["visibility"] == "project": add(memory["project_id"])
        for candidate_id in self._related_project_ids(project_id): add(candidate_id)
        # Registry identity matching is the fallback when memory FTS supplied no
        # project evidence. Avoid an alias join on the common lexical path.
        if not lexical and query_tokens and len(ordered) < DISCOVERY_PROJECT_CANDIDATE_LIMIT:
            clauses = []
            args: list[Any] = []
            for token in query_tokens:
                pattern = f"%{token}%"
                clauses.append("(lower(p.slug) LIKE ? OR lower(p.name) LIKE ? OR lower(COALESCE(p.description,'')) LIKE ? OR lower(a.normalized) LIKE ?)")
                args.extend([pattern] * 4)
            rows = self.conn.execute(f"""SELECT DISTINCT p.id FROM projects p
              LEFT JOIN project_aliases a ON a.project_id=p.id
              WHERE p.id<>? AND ({' OR '.join(clauses)}) ORDER BY p.id LIMIT ?""",
              [project_id, *args, DISCOVERY_PROJECT_CANDIDATE_LIMIT + 1])
            for row in rows: add(row["id"])
        return ordered[:DISCOVERY_PROJECT_CANDIDATE_LIMIT]

    def create_scope(self, project_id: str, name: str, path: str | None = None) -> dict[str, Any]:
        item = {"id": uid(), "project_id": project_id, "name": name, "path": path, "created_at": now()}
        with self.tx() as cx:
            cx.execute("INSERT INTO scopes VALUES(:id,:project_id,:name,:path,:created_at)", item)
            self._audit(cx, project_id, "scope", item["id"], "created", item)
        return item

    def resolve_project(self, cwd: str) -> dict[str, Any]:
        """Resolve a workspace hint using paths first, then stable repository identities."""
        path = str(Path(cwd).expanduser().resolve())
        identities = self._workspace_identities(path)
        row = self.conn.execute("""SELECT p.*, s.id AS scope_id FROM scopes s
          JOIN projects p ON p.id=s.project_id WHERE s.path=?""", (path,)).fetchone()
        if row:
            item = dict(row); scope_id = item.pop("scope_id")
            self._register_project_identities(item["id"], identities)
            return {"project": item, "scope_id": scope_id, "created": False}
        # A repository name resolves ownership only when it identifies one project.
        # Ambiguous names remain separate and are handled by retrieval discovery.
        for kind in ("name",):
            if kind not in identities: continue
            normalized = self._normalize_project_alias(kind, identities[kind])
            matches = list(self.conn.execute("SELECT DISTINCT project_id FROM project_aliases WHERE kind=? AND normalized=?", (kind, normalized)))
            if len(matches) != 1: continue
            project = self._row("SELECT * FROM projects WHERE id=?", (matches[0]["project_id"],))
            scope = self.create_scope(project["id"], f"__workspace__:{hashlib.sha256(path.encode()).hexdigest()[:12]}", path)
            self._register_project_identities(project["id"], identities)
            return {"project": project, "scope_id": scope["id"], "created": False, "matched_by": kind}
        base = re.sub(r"[^a-z0-9._-]+", "-", Path(path).name.lower()).strip("-._") or "workspace"
        slug = base[:54]
        existing = self._row("SELECT * FROM projects WHERE slug=?", (slug,))
        if existing:
            has_root = self.conn.execute("SELECT 1 FROM scopes WHERE project_id=? AND path IS NOT NULL", (existing["id"],)).fetchone()
            if not has_root:
                scope = self.create_scope(existing["id"], "__root__", path)
                return {"project": existing, "scope_id": scope["id"], "created": False}
            slug = f"{slug}-{hashlib.sha256(path.encode()).hexdigest()[:8]}"
        project = self.create_project(slug, Path(path).name, f"Automatically mapped from agent workspace: {path}")
        scope = self.create_scope(project["id"], "__root__", path)
        self._register_project_identities(project["id"], identities)
        return {"project": project, "scope_id": scope["id"], "created": True}

    def start_session(self, project_id: str, client: str = "codex", scope_id: str | None = None, external_id: str | None = None, metadata: dict | None = None) -> dict[str, Any]:
        if external_id:
            hit = self._row("SELECT * FROM sessions WHERE project_id=? AND client=? AND external_id=?", (project_id, client, external_id))
            if hit: return hit
        item = {"id": uid(), "project_id": project_id, "scope_id": scope_id, "client": client, "external_id": external_id,
                "started_at": now(), "ended_at": None, "metadata_json": canonical(metadata or {})}
        with self.tx() as cx:
            cx.execute("INSERT INTO sessions VALUES(:id,:project_id,:scope_id,:client,:external_id,:started_at,:ended_at,:metadata_json)", item)
            self._audit(cx, project_id, "session", item["id"], "started", item)
        return item

    def end_session(self, session_id: str, summary: str | None = None, extract_candidates: bool = True) -> dict[str, Any]:
        with self.tx() as cx:
            row = cx.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row: raise KeyError("session not found")
            ended = row["ended_at"] or now()
            cx.execute("UPDATE sessions SET ended_at=? WHERE id=?", (ended, session_id))
            result = dict(row); result["ended_at"] = ended
            self._audit(cx, row["project_id"], "session", session_id, "ended", {"summary": summary, **result})
        result["review"] = self.extract_session_candidates(session_id) if extract_candidates else {"created": [], "conflicts": []}
        return result

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {token for token in re.findall(r"[\w-]+", text.casefold(), flags=re.UNICODE) if len(token) > 1}

    @classmethod
    def _text_similarity(cls, left: str, right: str) -> float:
        a, b = cls._terms(left), cls._terms(right)
        return len(a & b) / len(a | b) if a and b else 0.0

    def extract_session_candidates(self, session_id: str) -> dict[str, Any]:
        session = self._row("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not session: raise KeyError("session not found")
        kinds = set(PROMOTABLE_EVENT_KINDS)
        created, conflicts = [], []
        events = self.conn.execute("SELECT * FROM events WHERE session_id=? ORDER BY event_seq", (session_id,))
        for event in events:
            if event["kind"] not in kinds: continue
            existing_source = self._row("SELECT memory_id FROM memory_sources WHERE event_id=?", (event["id"],))
            if existing_source: continue
            title = event["content"].strip().splitlines()[0][:120] or event["kind"].title()
            candidate = self.upsert_memory(session["project_id"], title, event["content"], event["kind"], "proposed",
                                           .6, .5, session["scope_id"], [event["id"]], idempotency_key=f"candidate:{event['id']}")
            created.append(candidate)
            for active in self.conn.execute("SELECT * FROM memories WHERE project_id=? AND status='active' AND id<>?", (session["project_id"], candidate["id"])):
                similarity = self._text_similarity(f"{candidate['title']} {candidate['content']}", f"{active['title']} {active['content']}")
                if similarity < .35: continue
                reason = "similar active memory; review for duplicate, replacement, or dispute"
                with self.tx() as cx:
                    cx.execute("INSERT OR IGNORE INTO review_conflicts VALUES(?,?,?,?,?)", (candidate["id"], active["id"], similarity, reason, now()))
                conflicts.append({"candidate_memory_id":candidate["id"], "existing_memory_id":active["id"], "similarity":similarity, "reason":reason})
        return {"created": created, "conflicts": conflicts}

    def review_queue(self, project_id: str) -> list[dict[str, Any]]:
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError("project not found")
        rows = []
        for row in self.conn.execute("SELECT * FROM memories WHERE project_id=? AND status='proposed' ORDER BY created_at,id", (project_id,)):
            item = dict(row)
            item["review_kind"] = "memory_candidate"
            item["conflicts"] = [dict(x) for x in self.conn.execute("SELECT * FROM review_conflicts WHERE candidate_memory_id=? ORDER BY similarity DESC", (item["id"],))]
            item["sources"] = [dict(x) for x in self.conn.execute("SELECT e.id,e.kind,e.source_uri,e.created_at FROM memory_sources s JOIN events e ON e.id=s.event_id WHERE s.memory_id=?", (item["id"],))]
            item["available_actions"] = ["approve", "reject"] + (["supersede", "dispute"] if item["conflicts"] else [])
            rows.append(item)
        revisions = self.conn.execute("""SELECT r.id FROM wiki_revisions r JOIN wiki_pages p ON p.id=r.page_id
          WHERE p.project_id=? AND r.status<>'rejected' AND r.revision_no=(
            SELECT max(latest.revision_no) FROM wiki_revisions latest
            WHERE latest.page_id=r.page_id AND latest.status<>'rejected')
          ORDER BY p.created_at,p.id,r.revision_no,r.id""", (project_id,))
        for revision_row in revisions:
            lint = self.lint_wiki_revision(revision_row["id"])
            revision = self.get_wiki_revision(revision_row["id"])
            if revision["status"] != "proposed" and not lint["findings"]:
                continue
            page = self._row("SELECT title,topic FROM wiki_pages WHERE id=?", (revision["page_id"],))
            actions = []
            if revision["status"] == "proposed":
                actions = [
                    {"action":"approve", "tool":"wiki_revision_transition", "arguments":{"status":"published"}},
                    {"action":"reject", "tool":"wiki_revision_transition", "arguments":{"status":"rejected"}},
                ]
            rows.append({"review_kind":"wiki_revision", "id":revision["id"],
                         "page_id":revision["page_id"], "page_title":page["title"], "topic":page["topic"],
                         "revision_no":revision["revision_no"], "status":revision["status"],
                         "lint":lint, "available_actions":actions})
        return rows

    def propose_correction(self, project_id: str, memory_id: str, content: str, title: str | None = None) -> dict[str, Any]:
        existing = self._row("SELECT * FROM memories WHERE id=? AND project_id=?", (memory_id, project_id))
        if not existing: raise KeyError("memory not found")
        event = self.record_event(project_id, "correction", content, scope_id=existing["scope_id"], metadata={"corrects_memory_id":memory_id})
        candidate = self.upsert_memory(project_id, title or existing["title"], content, existing["type"], "proposed",
                                       existing["confidence"], existing["importance"], existing["scope_id"], [event["id"]],
                                       visibility=existing["visibility"])
        with self.tx() as cx:
            cx.execute("INSERT OR IGNORE INTO review_conflicts VALUES(?,?,?,?,?)", (candidate["id"], memory_id, 1.0, "explicit correction", now()))
        return candidate

    def review_candidate(self, memory_id: str, action: str, related_memory_id: str | None = None, note: str = "") -> dict[str, Any]:
        candidate = self._row("SELECT * FROM memories WHERE id=? AND status='proposed'", (memory_id,))
        if not candidate: raise KeyError("proposed memory not found")
        if action == "approve": return self.transition(memory_id, "active", note=note)
        if action == "reject": return self.transition(memory_id, "rejected", note=note)
        if action not in {"supersede", "dispute"}: raise ValueError("action must be approve, reject, supersede, or dispute")
        target = related_memory_id
        if not target:
            row = self._row("SELECT existing_memory_id FROM review_conflicts WHERE candidate_memory_id=? ORDER BY similarity DESC LIMIT 1", (memory_id,))
            target = row["existing_memory_id"] if row else None
        if not target: raise ValueError("related_memory_id is required")
        self.transition(memory_id, "active", note=note)
        status = "superseded" if action == "supersede" else "disputed"
        self.transition(target, status, memory_id, note)
        return self._row("SELECT * FROM memories WHERE id=?", (memory_id,))

    def record_event(self, project_id: str, kind: str, content: str, session_id: str | None = None,
                     scope_id: str | None = None, source_uri: str | None = None, metadata: dict | None = None,
                     idempotency_key: str | None = None) -> dict[str, Any]:
        request = locals().copy(); request.pop("self"); request.pop("idempotency_key")
        if hit := self._idem("record_event", idempotency_key, request):
            if "event_seq" not in hit:
                migrated = self._row("SELECT event_seq FROM events WHERE id=?", (hit["id"],))
                if migrated: hit["event_seq"] = migrated["event_seq"]
            self._add_promotion_advisory(hit)
            return hit
        if not content.strip(): raise ValueError("event content cannot be empty")
        with self.tx() as cx:
            stored_metadata = dict(metadata or {})
            if kind == "message" and "expires_at" not in stored_metadata:
                policy = cx.execute("SELECT message_ttl_seconds FROM project_policies WHERE project_id=?", (project_id,)).fetchone()
                if policy and policy["message_ttl_seconds"]:
                    stored_metadata["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=policy["message_ttl_seconds"])).isoformat()
            cursor = cx.execute("UPDATE project_event_cursors SET next_seq=next_seq+1 WHERE project_id=? RETURNING next_seq-1", (project_id,)).fetchone()
            if not cursor: raise KeyError("project not found")
            item = {"id": uid(), "project_id": project_id, "scope_id": scope_id, "session_id": session_id, "kind": kind,
                    "content": content, "source_uri": source_uri, "metadata_json": canonical(stored_metadata),
                    "content_hash": hashlib.sha256(content.encode()).hexdigest(), "created_at": now(), "event_seq": cursor[0]}
            cx.execute("""INSERT INTO events(id,project_id,scope_id,session_id,kind,content,source_uri,metadata_json,content_hash,created_at,event_seq)
              VALUES(:id,:project_id,:scope_id,:session_id,:kind,:content,:source_uri,:metadata_json,:content_hash,:created_at,:event_seq)""", item)
            self._audit(cx, project_id, "event", item["id"], "recorded", item)
            self._add_promotion_advisory(item)
            self._save_idem(cx, "record_event", idempotency_key, request, item)
        return item

    def create_investigation(self, project_id: str, question: str, reason: str, decision_to_inform: str,
                             constraints: list[str] | None = None, initiator: str = "unknown",
                             scope_id: str | None = None, investigation_id: str | None = None,
                             idempotency_key: str | None = None) -> dict[str, Any]:
        """Create durable investigation intent without recording browsing activity."""
        request = locals().copy(); request.pop("self"); request.pop("idempotency_key")
        if hit := self._idem("create_investigation", idempotency_key, request): return hit
        if not all(value.strip() for value in (question, reason, decision_to_inform, initiator)):
            raise ValueError("question, reason, decision_to_inform, and initiator cannot be empty")
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError("project not found")
        if scope_id and not self._row("SELECT id FROM scopes WHERE id=? AND project_id=?", (scope_id, project_id)):
            raise ValueError("scope must belong to project")
        item = {"id":investigation_id or uid(), "project_id":project_id, "scope_id":scope_id,
                "question":question.strip(), "reason":reason.strip(), "decision_to_inform":decision_to_inform.strip(),
                "constraints_json":canonical(constraints or []), "initiator":initiator.strip(), "status":"open",
                "started_at":now(), "completed_at":None}
        with self.tx() as cx:
            cx.execute("""INSERT INTO investigations(id,project_id,scope_id,question,reason,decision_to_inform,
              constraints_json,initiator,status,started_at,completed_at)
              VALUES(:id,:project_id,:scope_id,:question,:reason,:decision_to_inform,:constraints_json,
              :initiator,:status,:started_at,:completed_at)""", item)
            self._audit(cx, project_id, "investigation", item["id"], "created", item)
            result = {**item, "constraints":json.loads(item["constraints_json"])}; result.pop("constraints_json")
            self._save_idem(cx, "create_investigation", idempotency_key, request, result)
        return result

    def record_source_analysis(self, investigation_id: str, source: dict[str, Any], claims: list[dict[str, Any]],
                               session_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        """Atomically record one source version and its consequential typed claims."""
        request = {"investigation_id":investigation_id,"source":source,"claims":claims,"session_id":session_id}
        if hit := self._idem("record_source_analysis", idempotency_key, request): return hit
        investigation = self._row("SELECT * FROM investigations WHERE id=?", (investigation_id,))
        if not investigation: raise KeyError("investigation not found")
        if investigation["status"] != "open": raise ValueError("investigation is completed")
        required = ("source_type", "stable_source_id", "access_reason", "analysis_method")
        if any(not isinstance(source.get(key), str) or not source[key].strip() for key in required):
            raise ValueError(f"source requires non-empty {', '.join(required)}")
        version, fingerprint = source.get("source_version"), source.get("content_fingerprint")
        if not (isinstance(version, str) and version.strip()) and not (isinstance(fingerprint, str) and fingerprint.strip()):
            raise ValueError("source_version or content_fingerprint is required for change detection")
        if not claims: raise ValueError("claims must be non-empty")
        keys = [claim.get("key") for claim in claims]
        if any(not isinstance(key, str) or not key.strip() for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("claim keys must be unique non-empty strings")
        version_key = (version or "").strip() or f"fingerprint:{(fingerprint or '').strip()}"
        identity = canonical([source["source_type"].strip(), source["stable_source_id"].strip(), version_key])
        identity_key = hashlib.sha256(identity.encode()).hexdigest()
        existing = self._row("SELECT id FROM source_analyses WHERE investigation_id=? AND identity_key=?",
                             (investigation_id, identity_key))
        if existing:
            chain = self.get_investigation(investigation_id, existing["id"])["source_analyses"][0]
            repeated_claims = []
            for claim in chain["claims"]:
                memory = self._row("SELECT status FROM memories WHERE id=?", (claim["memory_id"],))
                repeated_claims.append({**claim,"evidence_claim_keys":[item["claim_key"] for item in claim["evidence"]],
                                        "memory_status":memory["status"] if memory else None})
            return {"contract_version":"research-provenance/v1","investigation_id":investigation_id,
                    "source_analysis_id":chain["id"],"identity_key":chain["identity_key"],
                    "claims":repeated_claims,"idempotent":True}
        ts, analysis_id = now(), uid()
        source_item = {"id":analysis_id,"investigation_id":investigation_id,"source_type":source["source_type"].strip(),
                       "stable_source_id":source["stable_source_id"].strip(),"canonical_uri":source.get("canonical_uri"),
                       "source_version":version,"source_updated_at":source.get("source_updated_at"),
                       "retrieved_at":source.get("retrieved_at") or ts,"section_anchor":source.get("section_anchor"),
                       "access_reason":source["access_reason"].strip(),"analysis_method":source["analysis_method"].strip(),
                       "content_fingerprint":fingerprint,"identity_key":identity_key,"created_at":ts}
        result_claims = []
        with self.tx() as cx:
            cx.execute("""INSERT INTO source_analyses VALUES(:id,:investigation_id,:source_type,:stable_source_id,
              :canonical_uri,:source_version,:source_updated_at,:retrieved_at,:section_anchor,:access_reason,
              :analysis_method,:content_fingerprint,:identity_key,:created_at)""", source_item)
            created: dict[str, dict[str, Any]] = {}
            for ordinal, claim in enumerate(claims):
                role, content = claim.get("role"), claim.get("content")
                if role not in INVESTIGATION_ROLES or not isinstance(content, str) or not content.strip():
                    raise ValueError("each claim requires a valid role and non-empty content")
                refs = claim.get("evidence_claim_keys", [])
                external_refs = claim.get("evidence_claim_refs", [])
                if not isinstance(refs, list) or any(ref not in created for ref in refs):
                    raise ValueError("evidence_claim_keys must reference earlier claims in this analysis")
                if not isinstance(external_refs, list):
                    raise ValueError("evidence_claim_refs must be a list")
                resolved_external = []
                for ref in external_refs:
                    if not isinstance(ref, dict) or not isinstance(ref.get("source_analysis_id"), str) or not isinstance(ref.get("claim_key"), str):
                        raise ValueError("evidence_claim_refs require source_analysis_id and claim_key")
                    prior = cx.execute("""SELECT c.* FROM investigation_claims c
                      WHERE c.investigation_id=? AND c.source_analysis_id=? AND c.claim_key=?""",
                      (investigation_id, ref["source_analysis_id"], ref["claim_key"])).fetchone()
                    if not prior: raise ValueError("evidence_claim_refs must reference an existing claim in this investigation")
                    resolved_external.append(dict(prior))
                if role in {"inference", "action", "decision", "rationale", "outcome"} and not (refs or resolved_external):
                    raise ValueError(f"{role} claims require evidence claim references")
                expected_outcome, outcome_effect = claim.get("expected_outcome"), claim.get("outcome_effect")
                if expected_outcome is not None and (role != "decision" or not isinstance(expected_outcome, str) or not expected_outcome.strip()):
                    raise ValueError("expected_outcome is only valid as non-empty text on decision claims")
                if outcome_effect is not None and (role != "outcome" or outcome_effect not in OUTCOME_EFFECTS):
                    raise ValueError("outcome_effect is only valid on outcome claims")
                event_id, claim_id = uid(), uid()
                cursor = cx.execute("UPDATE project_event_cursors SET next_seq=next_seq+1 WHERE project_id=? RETURNING next_seq-1",
                                    (investigation["project_id"],)).fetchone()
                evidence_events = [created[key]["event_id"] for key in refs] + [item["event_id"] for item in resolved_external]
                metadata = {"investigation_id":investigation_id,"source_analysis_id":analysis_id,"claim_key":claim["key"],
                            "role":role,"evidence_event_ids":evidence_events,"expected_outcome":expected_outcome,
                            "outcome_effect":outcome_effect}
                event = {"id":event_id,"project_id":investigation["project_id"],"scope_id":investigation["scope_id"],
                         "session_id":session_id,"kind":"fact" if role in {"evidence","rationale","outcome"} else role,
                         "content":content.strip(),"source_uri":source_item["canonical_uri"],"metadata_json":canonical(metadata),
                         "content_hash":hashlib.sha256(content.strip().encode()).hexdigest(),"created_at":ts,"event_seq":cursor[0]}
                cx.execute("""INSERT INTO events(id,project_id,scope_id,session_id,kind,content,source_uri,metadata_json,
                  content_hash,created_at,event_seq) VALUES(:id,:project_id,:scope_id,:session_id,:kind,:content,
                  :source_uri,:metadata_json,:content_hash,:created_at,:event_seq)""", event)
                self._audit(cx, investigation["project_id"], "event", event_id, "recorded", event)
                status = claim.get("memory_status", "proposed")
                if status not in {"proposed", "active"} or (role == "inference" and status != "proposed"):
                    raise ValueError("memory_status must be proposed or active; inference must remain proposed")
                memory_id = uid()
                memory_type = claim.get("memory_type") or ({"decision":"decision","action":"task","rationale":"fact","outcome":"fact"}.get(role, "fact"))
                if memory_type not in TYPES: raise ValueError("invalid claim memory_type")
                memory = {"id":memory_id,"project_id":investigation["project_id"],"scope_id":investigation["scope_id"],
                          "type":memory_type,"status":status,"title":claim.get("title") or content.strip()[:120],
                          "content":content.strip(),"confidence":float(claim.get("confidence", .6)),
                          "importance":float(claim.get("importance", .5)),"valid_from":None,"valid_until":None,
                          "tags_json":canonical(["investigation", role]),"created_at":ts,"updated_at":ts,"observed_at":ts,
                          "last_confirmed_at":ts if status == "active" else None,"visibility":"project"}
                if not (0 <= memory["confidence"] <= 1 and 0 <= memory["importance"] <= 1):
                    raise ValueError("confidence and importance must be 0..1")
                cx.execute("""INSERT INTO memories(id,project_id,scope_id,type,status,title,content,confidence,importance,
                  valid_from,valid_until,tags_json,created_at,updated_at,observed_at,last_confirmed_at,visibility)
                  VALUES(:id,:project_id,:scope_id,:type,:status,:title,:content,:confidence,:importance,:valid_from,
                  :valid_until,:tags_json,:created_at,:updated_at,:observed_at,:last_confirmed_at,:visibility)""", memory)
                cx.execute("INSERT INTO memory_sources VALUES(?,?,?,?)", (memory_id,event_id,"investigation claim",ts))
                self._index_embedding(cx, memory)
                self._audit(cx, investigation["project_id"], "memory", memory_id, "created", memory)
                claim_item = {"id":claim_id,"investigation_id":investigation_id,"source_analysis_id":analysis_id,
                              "claim_key":claim["key"],"ordinal":ordinal,"role":role,"event_id":event_id,
                              "memory_id":memory_id,"created_at":ts,"expected_outcome":expected_outcome.strip() if expected_outcome else None,
                              "outcome_effect":outcome_effect}
                cx.execute("""INSERT INTO investigation_claims(id,investigation_id,source_analysis_id,claim_key,ordinal,
                  role,event_id,memory_id,created_at,expected_outcome,outcome_effect)
                  VALUES(:id,:investigation_id,:source_analysis_id,:claim_key,:ordinal,:role,:event_id,:memory_id,
                  :created_at,:expected_outcome,:outcome_effect)""", claim_item)
                relation = "derived_from" if role == "inference" else "informed" if role in {"action","decision"} else "supports"
                for ref in refs:
                    cx.execute("INSERT INTO investigation_claim_links VALUES(?,?,?,?)",
                               (created[ref]["id"],claim_id,relation,ts))
                for prior in resolved_external:
                    cx.execute("INSERT INTO investigation_claim_links VALUES(?,?,?,?)", (prior["id"],claim_id,relation,ts))
                created[claim["key"]] = claim_item
                result_claims.append({**claim_item,"evidence_claim_keys":refs,"evidence_claim_refs":external_refs,
                                      "memory_status":status})
            response = {"contract_version":"research-provenance/v1","investigation_id":investigation_id,
                        "source_analysis_id":analysis_id,"identity_key":identity_key,"claims":result_claims,"idempotent":False}
            self._audit(cx, investigation["project_id"], "source_analysis", analysis_id, "recorded", response)
            self._save_idem(cx, "record_source_analysis", idempotency_key, request, response)
        return response

    def get_investigation(self, investigation_id: str, source_analysis_id: str | None = None) -> dict[str, Any]:
        investigation = self._row("SELECT * FROM investigations WHERE id=?", (investigation_id,))
        if not investigation: raise KeyError("investigation not found")
        result = dict(investigation); result["constraints"] = json.loads(result.pop("constraints_json"))
        condition, args = (" AND id=?", (investigation_id, source_analysis_id)) if source_analysis_id else ("", (investigation_id,))
        analyses = []
        for row in self.conn.execute("SELECT * FROM source_analyses WHERE investigation_id=?" + condition + " ORDER BY created_at,id", args):
            analysis = dict(row); analysis["claims"] = []
            for claim in self.conn.execute("SELECT * FROM investigation_claims WHERE source_analysis_id=? ORDER BY ordinal", (analysis["id"],)):
                item = dict(claim)
                item["evidence"] = [dict(link) for link in self.conn.execute(
                    """SELECT l.relation,c.source_analysis_id,c.claim_key,c.event_id,c.memory_id FROM investigation_claim_links l
                    JOIN investigation_claims c ON c.id=l.from_claim_id WHERE l.to_claim_id=? ORDER BY c.claim_key""", (item["id"],))]
                analysis["claims"].append(item)
            analyses.append(analysis)
        return {"contract_version":"research-provenance/v1","investigation":result,"source_analyses":analyses,
                "idempotent":source_analysis_id is not None}

    def complete_investigation(self, investigation_id: str) -> dict[str, Any]:
        investigation = self._row("SELECT * FROM investigations WHERE id=?", (investigation_id,))
        if not investigation: raise KeyError("investigation not found")
        if investigation["status"] == "completed": return self.get_investigation(investigation_id)["investigation"]
        completed_at = now()
        with self.tx() as cx:
            cx.execute("UPDATE investigations SET status='completed',completed_at=? WHERE id=?", (completed_at,investigation_id))
            result = {**investigation,"status":"completed","completed_at":completed_at}
            self._audit(cx, investigation["project_id"], "investigation", investigation_id, "completed", result)
        result["constraints"] = json.loads(result.pop("constraints_json"))
        return result

    def create_wiki_page(self, project_id: str, topic: str, title: str, scope_id: str | None = None,
                         idempotency_key: str | None = None) -> dict[str, Any]:
        request = {"project_id":project_id,"topic":topic,"title":title,"scope_id":scope_id}
        if hit := self._idem("create_wiki_page", idempotency_key, request): return hit
        if not topic.strip() or not title.strip(): raise ValueError("topic and title cannot be empty")
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)): raise KeyError("project not found")
        if scope_id and not self._row("SELECT id FROM scopes WHERE id=? AND project_id=?", (scope_id,project_id)):
            raise ValueError("scope must belong to project")
        ts = now(); item = {"id":uid(),"project_id":project_id,"scope_id":scope_id,"topic":topic.strip(),
                            "title":title.strip(),"manual_notes":"","created_at":ts,"updated_at":ts}
        with self.tx() as cx:
            cx.execute("INSERT INTO wiki_pages VALUES(:id,:project_id,:scope_id,:topic,:title,:manual_notes,:created_at,:updated_at)", item)
            self._audit(cx, project_id, "wiki_page", item["id"], "created", item)
            self._save_idem(cx, "create_wiki_page", idempotency_key, request, item)
        return item

    def set_wiki_notes(self, page_id: str, manual_notes: str) -> dict[str, Any]:
        page = self._row("SELECT * FROM wiki_pages WHERE id=?", (page_id,))
        if not page: raise KeyError("wiki page not found")
        ts = now()
        with self.tx() as cx:
            cx.execute("UPDATE wiki_pages SET manual_notes=?,updated_at=? WHERE id=?", (manual_notes,ts,page_id))
            result = {**page,"manual_notes":manual_notes,"updated_at":ts}
            self._audit(cx, page["project_id"], "wiki_page", page_id, "manual_notes_updated",
                        {"length":len(manual_notes),"updated_at":ts})
        return result

    @staticmethod
    def _wiki_sections(brief: dict[str, Any]) -> dict[str, Any]:
        return {
            "current_position":brief["current_decisions"], "why_it_exists":brief["rationale"],
            "governing_constraints":brief["constraints"], "considered_alternatives":brief["alternatives"],
            "trade_offs":brief["expected_vs_observed"], "decision_timeline":brief["history"],
            "observed_outcomes":brief["outcomes"], "open_questions":brief["open_questions"],
        }

    def generate_wiki_revision(self, page_id: str, question: str, char_budget: int = 6000,
                               generation_metadata: dict[str, Any] | None = None,
                               idempotency_key: str | None = None) -> dict[str, Any]:
        request = {"page_id":page_id,"question":question,"char_budget":char_budget,
                   "generation_metadata":generation_metadata or {}}
        if hit := self._idem("generate_wiki_revision", idempotency_key, request): return hit
        page = self._row("SELECT * FROM wiki_pages WHERE id=?", (page_id,))
        if not page: raise KeyError("wiki page not found")
        if not question.strip(): raise ValueError("question cannot be empty")
        brief = self.decision_context(page["project_id"], question.strip(), char_budget, page["scope_id"], False)
        sections = self._wiki_sections(brief)
        cited_entries: list[tuple[str, int, str, str]] = []
        for section_name, entries in sections.items():
            for ordinal, entry in enumerate(entries):
                citation_groups = []
                if isinstance(entry, dict) and entry.get("citations"): citation_groups.append(entry["citations"])
                if isinstance(entry, dict):
                    citation_groups.extend(entry[key] for key in ("decision_citation","outcome_citation") if entry.get(key))
                for citation in citation_groups:
                    for event_id in citation.get("source_event_ids", []):
                        cited_entries.append((section_name,ordinal,citation["memory_id"],event_id))
        if not cited_entries: raise ValueError("wiki revision requires at least one cited memory event")
        metadata = {"contract_version":"topic-wiki/v1","generator":"decision_context",
                    "decision_brief_contract":brief["contract_version"],"retrieval_used":brief["retrieval"]["used"],
                    "caller":generation_metadata or {}}
        ts = now(); revision_id = uid()
        with self.tx() as cx:
            revision_no = cx.execute("SELECT coalesce(max(revision_no),0)+1 FROM wiki_revisions WHERE page_id=?", (page_id,)).fetchone()[0]
            item = {"id":revision_id,"page_id":page_id,"revision_no":revision_no,"status":"proposed",
                    "question":question.strip(),"sections_json":canonical(sections),"generation_json":canonical(metadata),
                    "created_at":ts,"published_at":None,"stale_reason":None}
            cx.execute("""INSERT INTO wiki_revisions VALUES(:id,:page_id,:revision_no,:status,:question,:sections_json,
              :generation_json,:created_at,:published_at,:stale_reason)""", item)
            for section_name, ordinal, memory_id, event_id in cited_entries:
                source = cx.execute("""SELECT m.project_id FROM memory_sources s JOIN memories m ON m.id=s.memory_id
                  JOIN events e ON e.id=s.event_id WHERE s.memory_id=? AND s.event_id=? AND m.project_id=e.project_id""",
                  (memory_id,event_id)).fetchone()
                if not source or source["project_id"] != page["project_id"]:
                    raise ValueError("wiki citations must be exact memory sources in the page project")
                cx.execute("INSERT OR IGNORE INTO wiki_revision_citations VALUES(?,?,?,?,?)",
                           (revision_id,section_name,ordinal,memory_id,event_id))
            response = self._wiki_revision_result(item, cited_entries)
            self._audit(cx, page["project_id"], "wiki_revision", revision_id, "created", response)
            self._save_idem(cx, "generate_wiki_revision", idempotency_key, request, response)
        return response

    @staticmethod
    def _wiki_revision_result(item: dict[str, Any], citations: list[tuple[str, int, str, str]]) -> dict[str, Any]:
        result = dict(item)
        result["sections"] = json.loads(result.pop("sections_json")); result["generation"] = json.loads(result.pop("generation_json"))
        result["citations"] = [{"section":s,"ordinal":o,"memory_id":m,"event_id":e}
                               for s,o,m,e in sorted(citations)]
        return result

    def transition_wiki_revision(self, revision_id: str, status: str, reason: str = "") -> dict[str, Any]:
        if status not in {"published","stale","rejected"}: raise ValueError("invalid wiki revision status")
        row = self._row("SELECT r.*,p.project_id FROM wiki_revisions r JOIN wiki_pages p ON p.id=r.page_id WHERE r.id=?", (revision_id,))
        if not row: raise KeyError("wiki revision not found")
        allowed = {"proposed":{"published","rejected"},"published":{"stale"},"stale":set(),"rejected":set()}
        if status not in allowed[row["status"]]: raise ValueError(f"cannot transition {row['status']} revision to {status}")
        ts = now()
        with self.tx() as cx:
            if status == "published":
                replaced = [item[0] for item in cx.execute(
                    "SELECT id FROM wiki_revisions WHERE page_id=? AND status='published'", (row["page_id"],))]
                replacement_reason = f"replaced by revision {row['revision_no']}"
                cx.execute("UPDATE wiki_revisions SET status='stale',stale_reason=? WHERE page_id=? AND status='published'",
                           (replacement_reason,row["page_id"]))
                for replaced_id in replaced:
                    self._audit(cx, row["project_id"], "wiki_revision", replaced_id, "status:stale",
                                {"reason":replacement_reason,"at":ts})
            cx.execute("UPDATE wiki_revisions SET status=?,published_at=?,stale_reason=? WHERE id=?",
                       (status,ts if status == "published" else row["published_at"],
                        reason or None if status == "stale" else None,revision_id))
            self._audit(cx, row["project_id"], "wiki_revision", revision_id, f"status:{status}", {"reason":reason,"at":ts})
        return self.get_wiki_revision(revision_id)

    def get_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        row = self._row("SELECT * FROM wiki_revisions WHERE id=?", (revision_id,))
        if not row: raise KeyError("wiki revision not found")
        citations = [(r["section_name"],r["ordinal"],r["memory_id"],r["event_id"]) for r in self.conn.execute(
            "SELECT * FROM wiki_revision_citations WHERE revision_id=? ORDER BY section_name,ordinal,memory_id,event_id", (revision_id,))]
        return self._wiki_revision_result(row,citations)

    def lint_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        """Deterministically surface evidence and lifecycle gaps in a Wiki revision."""
        revision = self.get_wiki_revision(revision_id)
        page = self._row("SELECT * FROM wiki_pages WHERE id=?", (revision["page_id"],))
        if not page: raise KeyError("wiki page not found")
        findings: list[dict[str, Any]] = []

        def add(code: str, severity: str, message: str, **details: Any) -> None:
            findings.append({"code":code,"severity":severity,"message":message,**details})

        def recommendation_signal(claim: str) -> str | None:
            patterns = (
                ("recommend", r"\b(?:recommend|recommended|advisable)\b"),
                ("should", r"\b(?:should|ought\s+to|best\s+to)\b"),
                ("korean_recommend", r"(?:권장|추천|하는\s*것이\s*좋|해야\s*(?:한다|합니다|함))"),
            )
            folded = claim.casefold()
            return next((name for name, pattern in patterns if re.search(pattern, folded)), None)

        def claim_provenance(memory_id: str) -> tuple[str, bool]:
            claim = self._row("SELECT id,role FROM investigation_claims WHERE memory_id=?", (memory_id,))
            if claim:
                supported = bool(self._row("SELECT 1 AS found FROM investigation_claim_links WHERE to_claim_id=? LIMIT 1",
                                           (claim["id"],)))
                return claim["role"], supported
            memory = self._row("SELECT type FROM memories WHERE id=?", (memory_id,))
            role = "decision" if memory and memory["type"] == "decision" else "evidence"
            supported = bool(self._row("""SELECT 1 AS found FROM edges WHERE to_memory_id=?
              AND relation IN ('supports','depends_on') LIMIT 1""", (memory_id,)))
            return role, supported

        citation_keys = {(item["section"],item["ordinal"],item["memory_id"],item["event_id"])
                         for item in revision["citations"]}
        cited_memories = {item["memory_id"] for item in revision["citations"]}
        for section, entries in revision["sections"].items():
            for ordinal, entry in enumerate(entries):
                embedded = []
                if isinstance(entry, dict):
                    embedded.extend(entry.get(key) for key in ("citations","decision_citation","outcome_citation")
                                    if entry.get(key))
                if not embedded:
                    add("missing_citation", "error", "Wiki claim has no memory citation.",
                        section=section, ordinal=ordinal)
                    continue
                for citation in embedded:
                    memory_id = citation.get("memory_id")
                    event_ids = citation.get("source_event_ids") or []
                    if not memory_id or not event_ids:
                        add("missing_citation", "error", "Wiki citation is incomplete.",
                            section=section, ordinal=ordinal, memory_id=memory_id)
                        continue
                    for event_id in event_ids:
                        if (section,ordinal,memory_id,event_id) not in citation_keys:
                            add("missing_citation", "error", "Embedded citation is absent from the immutable citation index.",
                                section=section, ordinal=ordinal, memory_id=memory_id, event_id=event_id)
                    signal = recommendation_signal(str(entry.get("claim") or entry.get("observed_outcome") or ""))
                    if signal and memory_id:
                        role, supported = claim_provenance(memory_id)
                        if role == "evidence":
                            add("recommendation_mislabeled_as_evidence", "error",
                                "Recommendation-like language must be labeled as inference, not evidence.",
                                section=section, ordinal=ordinal, memory_id=memory_id, detected_signal=signal,
                                current_label=role, required_label="inference")
                        if not supported and role not in {"decision", "action"}:
                            add("unsupported_recommendation", "error",
                                "Recommendation-like claim has no explicit supporting claim or memory relation.",
                                section=section, ordinal=ordinal, memory_id=memory_id, detected_signal=signal,
                                claim_role=role, required_label="inference")

        for memory_id in sorted(cited_memories):
            memory = self._row("SELECT * FROM memories WHERE id=?", (memory_id,))
            if not memory:
                add("missing_source", "error", "Cited memory no longer exists.", memory_id=memory_id)
                continue
            exact_sources = self.conn.execute("""SELECT count(*) FROM wiki_revision_citations c
              JOIN memory_sources s ON s.memory_id=c.memory_id AND s.event_id=c.event_id
              JOIN events e ON e.id=s.event_id AND e.project_id=?
              WHERE c.revision_id=? AND c.memory_id=?""",
              (page["project_id"],revision_id,memory_id)).fetchone()[0]
            indexed = self.conn.execute("SELECT count(*) FROM wiki_revision_citations WHERE revision_id=? AND memory_id=?",
                                        (revision_id,memory_id)).fetchone()[0]
            if exact_sources != indexed:
                add("missing_source", "error", "A citation is not backed by an exact memory source event.",
                    memory_id=memory_id, indexed_citations=indexed, exact_sources=exact_sources)
            if memory["status"] in {"superseded","expired","rejected"}:
                add("terminal_memory", "error", "Revision cites a terminal memory.",
                    memory_id=memory_id, memory_status=memory["status"])
            elif memory["status"] == "disputed":
                add("unresolved_dispute", "warning", "Revision cites a disputed memory.",
                    memory_id=memory_id, memory_status=memory["status"])

        inspected_at = datetime.now(timezone.utc)
        source_versions = self.conn.execute("""SELECT DISTINCT c.memory_id,s.id AS source_analysis_id,
          s.source_type,s.stable_source_id,s.canonical_uri,s.source_version,s.source_updated_at,s.retrieved_at
          FROM investigation_claims c JOIN source_analyses s ON s.id=c.source_analysis_id
          WHERE c.memory_id IN (SELECT memory_id FROM wiki_revision_citations WHERE revision_id=?)
          ORDER BY c.memory_id,s.retrieved_at,s.id""", (revision_id,))
        for source in source_versions:
            try:
                retrieved_at = datetime.fromisoformat(source["retrieved_at"].replace("Z", "+00:00"))
                if retrieved_at.tzinfo is None:
                    retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
                age_days = max(0, (inspected_at - retrieved_at.astimezone(timezone.utc)).days)
            except (AttributeError, TypeError, ValueError):
                continue
            if age_days < SOURCE_REINSPECTION_AGE_DAYS:
                continue
            add("source_reinspection_due", "warning",
                "The cited source version was inspected long enough ago to warrant reinspection; "
                "this does not establish that the external source changed or that the citation is stale.",
                memory_id=source["memory_id"], source_analysis_id=source["source_analysis_id"],
                source_type=source["source_type"], stable_source_id=source["stable_source_id"],
                canonical_uri=source["canonical_uri"], source_version=source["source_version"],
                source_updated_at=source["source_updated_at"], retrieved_at=source["retrieved_at"],
                age_days=age_days, threshold_days=SOURCE_REINSPECTION_AGE_DAYS,
                prompt="reinspect_source_version", external_change_verified=False)

        if revision["status"] == "stale":
            add("stale_revision", "error", "Wiki revision is marked stale.", reason=revision["stale_reason"])

        query = " ".join(part for part in (page["topic"], revision["question"]) if part).strip()
        relevant = self.search(page["project_id"], query, limit=20, statuses=["active"], scope_id=page["scope_id"])
        for memory in relevant:
            if memory["id"] not in cited_memories:
                add("omitted_current_memory", "warning", "Relevant active memory is omitted from the revision.",
                    memory_id=memory["id"], memory_type=memory["type"], title=memory["title"])

        findings.sort(key=lambda item: (item["code"],item.get("section", ""),item.get("ordinal", -1),
                                        item.get("memory_id", ""),item.get("event_id", "")))
        return {"contract_version":"topic-wiki-lint/v1","revision_id":revision_id,
                "page_id":revision["page_id"],"status":"fail" if any(x["severity"] == "error" for x in findings)
                else "warn" if findings else "pass", "finding_count":len(findings),"findings":findings,
                "deterministic":True,"state_changed":False}

    def get_wiki_page(self, page_id: str) -> dict[str, Any]:
        page = self._row("SELECT * FROM wiki_pages WHERE id=?", (page_id,))
        if not page: raise KeyError("wiki page not found")
        page["contract_version"] = "topic-wiki/v1"
        page["revisions"] = [self.get_wiki_revision(row["id"]) for row in self.conn.execute(
            "SELECT id FROM wiki_revisions WHERE page_id=? ORDER BY revision_no", (page_id,))]
        return page

    def render_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        revision = self.get_wiki_revision(revision_id)
        page = self._row("SELECT * FROM wiki_pages WHERE id=?", (revision["page_id"],))
        labels = {"current_position":"Current position","why_it_exists":"Why it exists",
                  "governing_constraints":"Governing constraints","considered_alternatives":"Considered alternatives",
                  "trade_offs":"Trade-offs","decision_timeline":"Decision timeline",
                  "observed_outcomes":"Observed outcomes","open_questions":"Open questions"}
        lines = [f"# {page['title']}","",f"Status: {revision['status']} · Revision {revision['revision_no']}",""]
        for key, label in labels.items():
            lines.extend([f"## {label}",""])
            entries = revision["sections"].get(key, [])
            if not entries: lines.extend(["_No cited material._",""]); continue
            for entry in entries:
                claim = entry.get("claim") or entry.get("observed_outcome") or canonical(entry)
                refs=[]
                for citation_key in ("citations","decision_citation","outcome_citation"):
                    citation=entry.get(citation_key)
                    if citation: refs.append(f"memory:{citation['memory_id']} events:{','.join(citation['source_event_ids'])}")
                lines.append(f"- {claim} ({'; '.join(refs)})")
            lines.append("")
        lines.extend(["## Manual notes","",page["manual_notes"] or "_None._",""])
        return {"contract_version":"topic-wiki-markdown/v1","revision_id":revision_id,"markdown":"\n".join(lines)}

    def _stale_wiki_revisions_for_memory(self, cx: sqlite3.Connection, memory_id: str, reason: str) -> list[str]:
        rows = list(cx.execute("""SELECT DISTINCT r.id,p.project_id FROM wiki_revisions r
          JOIN wiki_pages p ON p.id=r.page_id JOIN wiki_revision_citations c ON c.revision_id=r.id
          WHERE c.memory_id=? AND r.status='published'""", (memory_id,)))
        ids = [row["id"] for row in rows]
        if ids:
            cx.execute(f"UPDATE wiki_revisions SET status='stale',stale_reason=? WHERE id IN ({','.join('?' for _ in ids)})",
                       (reason,*ids))
            for row in rows:
                self._audit(cx, row["project_id"], "wiki_revision", row["id"], "status:stale",
                            {"reason":reason,"memory_id":memory_id,"at":now()})
        return ids

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
                f"Event kind '{item.get('kind')}' is preserved as immutable evidence but is not automatically "
                "converted to a proposed memory at session_end. Record new evidence with a promotable kind "
                "if a memory candidate is intended; do not rewrite this event."
            )

    def create_checkpoint(self, project_id: str, mode: str, reason: str, goal: str,
                          idempotency_key: str, session_id: str | None = None,
                          scope_id: str | None = None, completed: list[str] | None = None,
                          next_step: str | None = None, blockers: list[str] | None = None,
                          source_event_cursor: int | None = None,
                          context_usage: float | None = None,
                          repository_path: str | None = None,
                          test_results: list[dict[str, Any]] | None = None,
                          verified_event_ids: list[str] | None = None,
                          handoff_title: str | None = None,
                          handoff_content: str | None = None,
                          previous_handoff_memory_id: str | None = None,
                          commit: str | None = None) -> dict[str, Any]:
        """Record one explicit, client-neutral recovery checkpoint.

        Interim checkpoints only record recovery state. Final checkpoints atomically
        publish an evidence-backed handoff, replace its predecessor, and end the
        referenced session. Neither mode mutates Git.
        """
        request = {"project_id":project_id, "mode":mode, "reason":reason, "goal":goal,
                   "session_id":session_id, "scope_id":scope_id, "completed":completed,
                   "next_step":next_step, "blockers":blockers,
                   "source_event_cursor":source_event_cursor, "context_usage":context_usage,
                   "repository_path":repository_path, "test_results":test_results,
                   "verified_event_ids":verified_event_ids, "handoff_title":handoff_title,
                   "handoff_content":handoff_content,
                   "previous_handoff_memory_id":previous_handoff_memory_id, "commit":commit}
        if hit := self._idem("create_checkpoint", idempotency_key, request): return hit
        if mode not in CHECKPOINT_MODES: raise ValueError("mode must be interim or final")
        if reason not in CHECKPOINT_REASONS:
            raise ValueError("reason must be context_budget, elapsed, material_change, completed, or manual")
        if mode == "interim" and reason == "completed":
            raise ValueError("interim checkpoints cannot claim completed work")
        if not goal.strip(): raise ValueError("goal cannot be empty")
        if not idempotency_key.strip(): raise ValueError("idempotency_key cannot be empty")
        completed = completed or []; blockers = blockers or []
        if any(not item.strip() for item in completed): raise ValueError("completed must contain non-empty values")
        if any(not item.strip() for item in blockers): raise ValueError("blockers must contain non-empty values")
        if next_step is not None and not next_step.strip(): raise ValueError("next_step cannot be empty")
        if context_usage is not None and not 0 <= context_usage <= 1:
            raise ValueError("context_usage must be between 0 and 1")
        tests = self._normalize_test_results(test_results or [])
        verified_event_ids = list(dict.fromkeys(verified_event_ids or []))
        repository = self._repository_facts(repository_path) if repository_path else None
        project = self._row("SELECT id FROM projects WHERE id=?", (project_id,))
        if not project: raise KeyError("project not found")
        if session_id:
            session = self._row("SELECT project_id,scope_id,ended_at FROM sessions WHERE id=?", (session_id,))
            if not session: raise KeyError("session not found")
            if session["project_id"] != project_id: raise ValueError("session belongs to a different project")
            if mode == "interim" and session["ended_at"] is not None:
                raise ValueError("interim checkpoints require an active session")
            if mode == "final" and session["ended_at"] is not None:
                raise ValueError("final checkpoints require an active session")
            if scope_id is None: scope_id = session["scope_id"]
        if mode == "final":
            if not session_id: raise ValueError("final checkpoints require an active session")
            if not verified_event_ids: raise ValueError("final checkpoints require verified_event_ids")
            if not handoff_title or not handoff_title.strip(): raise ValueError("final checkpoints require handoff_title")
            if not handoff_content or not handoff_content.strip(): raise ValueError("final checkpoints require handoff_content")
            if commit:
                if not repository_path: raise ValueError("commit requires repository_path")
                try:
                    commit = subprocess.run(
                        ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"], cwd=repository_path,
                        check=True, capture_output=True, text=True).stdout.strip()
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise ValueError("commit must identify an existing repository commit") from exc
        if scope_id:
            scope = self._row("SELECT project_id FROM scopes WHERE id=?", (scope_id,))
            if not scope: raise KeyError("scope not found")
            if scope["project_id"] != project_id: raise ValueError("scope belongs to a different project")
        cursor = self._row("SELECT next_seq-1 AS value FROM project_event_cursors WHERE project_id=?", (project_id,))["value"]
        if source_event_cursor is None: source_event_cursor = cursor
        if source_event_cursor < 0 or source_event_cursor > cursor:
            raise ValueError("source_event_cursor must reference an existing project event cursor")
        recovery_hash = self._checkpoint_recovery_hash(
            project_id, source_event_cursor, goal, completed, next_step, blockers, repository)
        payload = {"schema_version": 5, "mode": mode, "reason": reason, "goal": goal.strip(),
                   "completed": [item.strip() for item in completed],
                   "next_step": next_step.strip() if next_step else None,
                   "blockers": [item.strip() for item in blockers],
                   "source_event_cursor": source_event_cursor, "context_usage": context_usage,
                   "recovery_hash": recovery_hash,
                   "claims": {"completion": False, "verification": False} if mode == "interim" else {"completion": True, "verification": True},
                   "verified_event_ids": verified_event_ids, "commit": commit,
                   "objective": {"repository": repository, "test_results": tests}}
        content = canonical(payload); created_at = now()
        with self.tx() as cx:
            existing = cx.execute(
                "SELECT request_hash,response_json FROM idempotency_keys WHERE operation=? AND key=?",
                ("create_checkpoint", idempotency_key)).fetchone()
            if existing:
                digest = hashlib.sha256(canonical(request).encode()).hexdigest()
                if digest != existing["request_hash"]:
                    raise ValueError("idempotency key reused with a different request")
                return json.loads(existing["response_json"])
            next_cursor = cx.execute(
                "UPDATE project_event_cursors SET next_seq=next_seq+1 WHERE project_id=? RETURNING next_seq-1",
                (project_id,)).fetchone()
            if not next_cursor: raise KeyError("project not found")
            event = {"id":uid(), "project_id":project_id, "scope_id":scope_id, "session_id":session_id,
                     "kind":"checkpoint", "content":content, "source_uri":None,
                     "metadata_json":canonical({"checkpoint":payload}),
                     "content_hash":hashlib.sha256(content.encode()).hexdigest(),
                     "created_at":created_at, "event_seq":next_cursor[0]}
            cx.execute("""INSERT INTO events(id,project_id,scope_id,session_id,kind,content,source_uri,metadata_json,content_hash,created_at,event_seq)
              VALUES(:id,:project_id,:scope_id,:session_id,:kind,:content,:source_uri,:metadata_json,:content_hash,:created_at,:event_seq)""", event)
            self._audit(cx, project_id, "event", event["id"], "recorded", event)
            handoff = None
            if mode == "final":
                for event_id in verified_event_ids:
                    source = cx.execute("SELECT project_id FROM events WHERE id=?", (event_id,)).fetchone()
                    if not source or source["project_id"] != project_id:
                        raise ValueError(f"invalid verified event: {event_id}")
                previous = None
                if previous_handoff_memory_id:
                    previous = cx.execute("SELECT * FROM memories WHERE id=?", (previous_handoff_memory_id,)).fetchone()
                    if not previous or previous["project_id"] != project_id:
                        raise ValueError("previous handoff must belong to the same project")
                    if previous["status"] != "active": raise ValueError("previous handoff must be active")
                handoff_id, handoff_ts = uid(), now()
                handoff = {"id":handoff_id, "project_id":project_id, "scope_id":scope_id, "type":"task", "status":"active",
                           "title":handoff_title.strip(), "content":handoff_content.strip(), "confidence":1.0, "importance":1.0,
                           "valid_from":None, "valid_until":None, "tags_json":canonical(["handoff", "checkpoint"]),
                           "created_at":handoff_ts, "updated_at":handoff_ts, "observed_at":handoff_ts,
                           "last_confirmed_at":handoff_ts, "visibility":"project"}
                cx.execute("""INSERT INTO memories(id,project_id,scope_id,type,status,title,content,confidence,importance,valid_from,valid_until,
                  tags_json,created_at,updated_at,observed_at,last_confirmed_at,visibility)
                  VALUES(:id,:project_id,:scope_id,:type,:status,:title,:content,:confidence,:importance,:valid_from,:valid_until,
                  :tags_json,:created_at,:updated_at,:observed_at,:last_confirmed_at,:visibility)""", handoff)
                for event_id in [event["id"], *verified_event_ids]:
                    cx.execute("INSERT INTO memory_sources VALUES(?,?,?,?)", (handoff_id, event_id, "", handoff_ts))
                self._index_embedding(cx, handoff)
                self._audit(cx, project_id, "memory", handoff_id, "created", handoff)
                if previous:
                    cx.execute("UPDATE memories SET status='superseded',updated_at=? WHERE id=?", (handoff_ts, previous_handoff_memory_id))
                    cx.execute("INSERT INTO edges VALUES(?,?,?,?,?,?,?)", (uid(), project_id, handoff_id,
                               previous_handoff_memory_id, "supersedes", "replaced by final checkpoint", handoff_ts))
                    self._audit(cx, project_id, "memory", previous_handoff_memory_id, "status:superseded",
                                {"replacement_memory_id":handoff_id})
                cx.execute("UPDATE sessions SET ended_at=? WHERE id=?", (handoff_ts, session_id))
                self._audit(cx, project_id, "session", session_id, "ended", {"checkpoint_id":event["id"], "ended_at":handoff_ts})
            result = {"checkpoint_id":event["id"], "event_seq":event["event_seq"],
                      "created_at":created_at, "handoff_memory_id":handoff["id"] if handoff else None,
                      "previous_handoff_memory_id":previous_handoff_memory_id if handoff else None,
                      "session_ended":mode == "final", **payload}
            self._save_idem(cx, "create_checkpoint", idempotency_key, request, result)
        return result

    def evaluate_checkpoint(self, project_id: str, context_usage: float | None = None,
                            session_id: str | None = None,
                            repository_path: str | None = None, goal: str = "",
                            completed: list[str] | None = None, next_step: str | None = None,
                            blockers: list[str] | None = None) -> dict[str, Any]:
        """Evaluate portable checkpoint triggers without writing a checkpoint."""
        if context_usage is not None and not 0 <= context_usage <= 1:
            raise ValueError("context_usage must be between 0 and 1")
        policy = self.get_policy(project_id)
        session = None
        if session_id:
            session = self._row("SELECT project_id,started_at FROM sessions WHERE id=?", (session_id,))
            if not session: raise KeyError("session not found")
            if session["project_id"] != project_id: raise ValueError("session belongs to a different project")
        latest = self._row("SELECT * FROM events WHERE project_id=? AND kind='checkpoint' ORDER BY event_seq DESC LIMIT 1", (project_id,))
        cursor = self._row("SELECT next_seq-1 AS value FROM project_event_cursors WHERE project_id=?", (project_id,))
        if not cursor: raise KeyError("project not found")
        current_repository = self._repository_facts(repository_path) if repository_path else None
        latest_payload = json.loads(latest["metadata_json"]).get("checkpoint", {}) if latest else {}
        prior_repository = None
        if latest:
            prior_repository = latest_payload.get("objective", {}).get("repository")
        repository_changed = bool(current_repository and (
            prior_repository is None or any(current_repository.get(key) != prior_repository.get(key)
                                             for key in ("head", "dirty", "changed_files"))))
        baseline_cursor = latest["event_seq"] if latest else 0
        durable_event_count = self.conn.execute(
            "SELECT count(*) FROM events WHERE project_id=? AND event_seq>? AND kind<>'checkpoint'",
            (project_id, baseline_cursor)).fetchone()[0]
        current_time = datetime.now(timezone.utc)
        checkpoint_age = int((current_time - datetime.fromisoformat(latest["created_at"])).total_seconds()) if latest else None
        session_elapsed = int((current_time - datetime.fromisoformat(session["started_at"])).total_seconds()) if session else None
        material_change = repository_changed or durable_event_count > 0
        recovery_hash = self._checkpoint_recovery_hash(
            project_id, cursor["value"], goal, completed or [], next_step, blockers or [], current_repository)
        unchanged = latest_payload.get("recovery_hash") == recovery_hash
        signals = {
            "context_usage": context_usage,
            "material_change": material_change,
            "repository_changed": repository_changed,
            "durable_event_count": durable_event_count,
            "session_elapsed_seconds": session_elapsed,
            "checkpoint_age_seconds": checkpoint_age,
            "recoverable_state_changed": not unchanged,
        }
        trigger = None
        mode = None
        if context_usage is not None:
            if context_usage >= policy["checkpoint_hard_usage"]:
                trigger, mode = "hard_context_usage", "interim"
            elif context_usage >= policy["checkpoint_soft_usage"] and material_change:
                trigger, mode = "soft_context_usage_after_material_change", "interim"
        else:
            fallback = [
                (session_elapsed is not None and session_elapsed >= policy["checkpoint_elapsed_seconds"], "elapsed"),
                (durable_event_count >= policy["checkpoint_event_count"], "event_count"),
                (repository_changed, "repository_change"),
                (checkpoint_age is not None and checkpoint_age >= policy["checkpoint_max_age_seconds"] and material_change, "checkpoint_age"),
            ]
            trigger = next((name for matched, name in fallback if matched), None)
            if trigger: mode = "interim"
        suppression = None
        if trigger and unchanged:
            suppression = "unchanged_recovery_state"
        elif trigger and checkpoint_age is not None and checkpoint_age < policy["checkpoint_cooldown_seconds"]:
            suppression = "cooldown"
        elif trigger == "soft_context_usage_after_material_change" and latest_payload.get("context_usage") is not None:
            rearm_usage = min(1.0, latest_payload["context_usage"] + policy["checkpoint_hysteresis"])
            if context_usage < rearm_usage: suppression = "hysteresis"
        if suppression:
            trigger, mode = None, None
        suggested_key = f"checkpoint:{project_id}:{recovery_hash}"
        return {"project_id": project_id, "should_checkpoint": trigger is not None,
                "recommended_mode": mode, "recommended_reason": "context_budget" if context_usage is not None and trigger else ("elapsed" if trigger in {"elapsed", "checkpoint_age"} else "material_change" if trigger else None),
                "trigger": trigger, "suppression": suppression, "signals": signals,
                "thresholds": {key: policy[key] for key in ("checkpoint_soft_usage", "checkpoint_hard_usage", "checkpoint_elapsed_seconds", "checkpoint_event_count", "checkpoint_max_age_seconds", "checkpoint_cooldown_seconds", "checkpoint_hysteresis")},
                "recovery_hash": recovery_hash, "suggested_idempotency_key": suggested_key,
                "latest_checkpoint_id": latest["id"] if latest else None, "event_cursor": cursor["value"]}

    def _checkpoint_recovery_hash(self, project_id: str, cursor: int, goal: str,
                                  completed: list[str], next_step: str | None,
                                  blockers: list[str], repository: dict[str, Any] | None) -> str:
        event_hashes = [row[0] for row in self.conn.execute(
            "SELECT content_hash FROM events WHERE project_id=? AND event_seq<=? AND kind<>'checkpoint' ORDER BY event_seq",
            (project_id, cursor))]
        state = {"goal":goal.strip(), "completed":[item.strip() for item in completed],
                 "next_step":next_step.strip() if next_step else None,
                 "blockers":[item.strip() for item in blockers], "repository":repository,
                 "event_hashes":event_hashes}
        return hashlib.sha256(canonical(state).encode()).hexdigest()

    @staticmethod
    def _normalize_test_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        allowed = {"name", "status", "command", "details"}
        normalized = []
        for result in results:
            if not isinstance(result, dict) or set(result) - allowed:
                raise ValueError("test_results must contain objects with name, status, command, and details only")
            name, status = result.get("name"), result.get("status")
            if not isinstance(name, str) or not name.strip(): raise ValueError("test result name cannot be empty")
            if status not in {"passed", "failed", "skipped"}: raise ValueError("test result status must be passed, failed, or skipped")
            item = {"name": name.strip(), "status": status}
            for field in ("command", "details"):
                value = result.get(field)
                if value is not None:
                    if not isinstance(value, str) or not value.strip(): raise ValueError(f"test result {field} cannot be empty")
                    item[field] = value.strip()
            normalized.append(item)
        return normalized

    @staticmethod
    def _repository_facts(path: str) -> dict[str, Any]:
        root = Path(path).expanduser().resolve()
        if not root.is_dir(): raise ValueError("repository_path must be an existing directory")
        def git(*args: str) -> str:
            completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
            if completed.returncode != 0: raise ValueError("repository_path must identify a Git worktree")
            return completed.stdout.rstrip("\n")
        top_level = str(Path(git("rev-parse", "--show-toplevel")).resolve())
        head = git("rev-parse", "HEAD")
        branch_value = git("symbolic-ref", "--quiet", "--short", "HEAD") if subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "--quiet", "HEAD"], capture_output=True).returncode == 0 else None
        changed = []
        entries = git("status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            if not entry: break
            status, path_value = entry[:2], entry[3:]
            changed.append({"path": path_value, "status": status})
            index += 2 if "R" in status or "C" in status else 1
        return {"root": top_level, "head": head, "branch": branch_value, "dirty": bool(changed), "changed_files": changed}

    def read_events_since(self, project_id: str, cursor: int = 0, kinds: list[str] | None = None,
                          scope_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Read immutable events after a project cursor without mixing them into memory ranking."""
        if cursor < 0: raise ValueError("cursor must be non-negative")
        if not 1 <= limit <= 1000: raise ValueError("limit must be 1..1000")
        if kinds is not None and (not kinds or any(not kind.strip() for kind in kinds)):
            raise ValueError("kinds must contain non-empty values")
        state = self._row("SELECT next_seq-1 AS snapshot_cursor FROM project_event_cursors WHERE project_id=?", (project_id,))
        if not state: raise KeyError("project not found")
        snapshot = state["snapshot_cursor"]
        sql = "SELECT * FROM events WHERE project_id=? AND event_seq>? AND event_seq<=?"
        args: list[Any] = [project_id, cursor, snapshot]
        if kinds:
            unique_kinds = list(dict.fromkeys(kinds))
            sql += " AND kind IN (" + ",".join("?" for _ in unique_kinds) + ")"; args.extend(unique_kinds)
        if scope_id:
            sql += " AND (scope_id=? OR scope_id IS NULL)"; args.append(scope_id)
        sql += " ORDER BY event_seq LIMIT ?"; args.append(limit + 1)
        rows = [dict(row) for row in self.conn.execute(sql, args)]
        has_more = len(rows) > limit
        rows = rows[:limit]
        page_cursor = rows[-1]["event_seq"] if has_more and rows else snapshot
        visible = []
        current = datetime.now(timezone.utc)
        for row in rows:
            row["metadata"] = json.loads(row.pop("metadata_json"))
            expires_at = row["metadata"].get("expires_at") if row["kind"] == "message" else None
            if expires_at:
                try:
                    expired = datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= current
                except (TypeError, ValueError):
                    expired = False
                if expired: continue
            visible.append(row)
        rows = visible
        next_cursor = page_cursor if has_more else snapshot
        return {"project_id":project_id,"cursor":cursor,"snapshot_cursor":snapshot,"next_cursor":next_cursor,
                "has_more":has_more,"events":rows}

    @staticmethod
    def _receipt_stream(kinds: list[str] | None, scope_id: str | None) -> tuple[str, str, list[str] | None]:
        normalized = sorted(set(kinds)) if kinds else None
        return scope_id or "", canonical(normalized or []), normalized

    def poll_events(self, project_id: str, consumer_id: str, kinds: list[str] | None = None,
                    scope_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Read from a durable per-consumer receipt without acknowledging delivery."""
        consumer_id = consumer_id.strip()
        if not consumer_id: raise ValueError("consumer_id cannot be empty")
        scope_key, kinds_json, normalized = self._receipt_stream(kinds, scope_id)
        receipt = self._row("""SELECT * FROM event_receipts WHERE project_id=? AND consumer_id=?
          AND scope_key=? AND kinds_json=?""", (project_id, consumer_id, scope_key, kinds_json))
        cursor = receipt["acknowledged_cursor"] if receipt else 0
        result = self.read_events_since(project_id, cursor, normalized, scope_id, limit)
        delivered = max(cursor, result["next_cursor"])
        ts = now()
        with self.tx() as cx:
            cx.execute("""INSERT INTO event_receipts(project_id,consumer_id,scope_key,kinds_json,acknowledged_cursor,delivered_cursor,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id,consumer_id,scope_key,kinds_json)
              DO UPDATE SET delivered_cursor=max(event_receipts.delivered_cursor,excluded.delivered_cursor),updated_at=excluded.updated_at""",
              (project_id, consumer_id, scope_key, kinds_json, cursor, delivered, ts, ts))
        result.update({"consumer_id":consumer_id,"acknowledged_cursor":cursor,"delivered_cursor":delivered})
        return result

    def acknowledge_events(self, project_id: str, consumer_id: str, cursor: int,
                           kinds: list[str] | None = None, scope_id: str | None = None) -> dict[str, Any]:
        """Monotonically acknowledge a cursor previously delivered for this exact stream."""
        consumer_id = consumer_id.strip()
        if not consumer_id: raise ValueError("consumer_id cannot be empty")
        if cursor < 0: raise ValueError("cursor must be non-negative")
        scope_key, kinds_json, _ = self._receipt_stream(kinds, scope_id)
        with self.tx() as cx:
            row = cx.execute("""SELECT * FROM event_receipts WHERE project_id=? AND consumer_id=?
              AND scope_key=? AND kinds_json=?""", (project_id, consumer_id, scope_key, kinds_json)).fetchone()
            if not row: raise KeyError("event receipt not found; poll this stream before acknowledging")
            if cursor < row["acknowledged_cursor"]: raise ValueError("acknowledged cursor cannot move backwards")
            if cursor > row["delivered_cursor"]: raise ValueError("cannot acknowledge beyond the delivered cursor")
            ts = now()
            cx.execute("""UPDATE event_receipts SET acknowledged_cursor=?,updated_at=? WHERE project_id=?
              AND consumer_id=? AND scope_key=? AND kinds_json=?""",
              (cursor, ts, project_id, consumer_id, scope_key, kinds_json))
            item = dict(cx.execute("""SELECT * FROM event_receipts WHERE project_id=? AND consumer_id=?
              AND scope_key=? AND kinds_json=?""", (project_id, consumer_id, scope_key, kinds_json)).fetchone())
            item["kinds"] = json.loads(item.pop("kinds_json")); item["scope_id"] = item.pop("scope_key") or None
            self._audit(cx, project_id, "event_receipt", f"{consumer_id}:{scope_key}:{kinds_json}", "acknowledged", item)
        return item

    def upsert_memory(self, project_id: str, title: str, content: str, memory_type: str = "other", status: str = "proposed",
                      confidence: float = .5, importance: float = .5, scope_id: str | None = None, source_event_ids: list[str] | None = None,
                      valid_from: str | None = None, valid_until: str | None = None, tags: list[str] | None = None,
                      observed_at: str | None = None, last_confirmed_at: str | None = None, visibility: str | None = None,
                      memory_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        request = locals().copy(); request.pop("self"); request.pop("idempotency_key")
        if hit := self._idem("upsert_memory", idempotency_key, request): return hit
        ts, mid = now(), memory_id or uid()
        existing = self._row("SELECT * FROM memories WHERE id=?", (mid,))
        if memory_type not in TYPES or status not in STATUSES: raise ValueError("invalid memory type or status")
        resolved_visibility = visibility or (existing["visibility"] if existing else "project")
        if resolved_visibility not in {"project", "global"}: raise ValueError("visibility must be project or global")
        if resolved_visibility == "global" and scope_id is not None: raise ValueError("global memories cannot be path-scoped")
        if not (0 <= confidence <= 1 and 0 <= importance <= 1): raise ValueError("confidence and importance must be 0..1")
        item = {"id": mid, "project_id": project_id, "scope_id": scope_id, "type": memory_type, "status": status, "title": title,
                "content": content, "confidence": confidence, "importance": importance, "valid_from": valid_from, "valid_until": valid_until,
                "tags_json": canonical(tags or []), "created_at": existing["created_at"] if existing else ts, "updated_at": ts,
                "observed_at": observed_at or (existing["observed_at"] if existing else ts), "visibility": resolved_visibility,
                "last_confirmed_at": last_confirmed_at or (existing["last_confirmed_at"] if existing else (ts if status == "active" else None))}
        with self.tx() as cx:
            if existing:
                if existing["project_id"] != project_id: raise ValueError("memory belongs to another project")
                cx.execute("""UPDATE memories SET scope_id=:scope_id,type=:type,status=:status,title=:title,content=:content,confidence=:confidence,
                  importance=:importance,valid_from=:valid_from,valid_until=:valid_until,tags_json=:tags_json,updated_at=:updated_at,
                  observed_at=:observed_at,last_confirmed_at=:last_confirmed_at,visibility=:visibility WHERE id=:id""", item)
                action = "updated"
                if any(existing[name] != item[name] for name in ("title","content","type","status","valid_from","valid_until","tags_json")):
                    self._stale_wiki_revisions_for_memory(cx, mid, "cited memory materially updated")
            else:
                cx.execute("""INSERT INTO memories(id,project_id,scope_id,type,status,title,content,confidence,importance,valid_from,valid_until,
                  tags_json,created_at,updated_at,observed_at,last_confirmed_at,visibility)
                  VALUES(:id,:project_id,:scope_id,:type,:status,:title,:content,:confidence,:importance,:valid_from,:valid_until,
                  :tags_json,:created_at,:updated_at,:observed_at,:last_confirmed_at,:visibility)""", item)
                action = "created"
            for eid in source_event_ids or []:
                event = cx.execute("SELECT project_id FROM events WHERE id=?", (eid,)).fetchone()
                if not event or event["project_id"] != project_id: raise ValueError(f"invalid source event: {eid}")
                cx.execute("INSERT OR IGNORE INTO memory_sources VALUES(?,?,?,?)", (mid, eid, "", ts))
            self._index_embedding(cx, item)
            self._audit(cx, project_id, "memory", mid, action, item)
            self._save_idem(cx, "upsert_memory", idempotency_key, request, item)
        return item

    def _provider_name(self) -> str | None:
        if not self.embedding_provider:
            return None
        return str(getattr(self.embedding_provider, "name", self.embedding_provider.__class__.__name__))

    def _index_embedding(self, cx: sqlite3.Connection, memory: dict[str, Any]) -> None:
        if not self.embedding_provider:
            return
        text = f"{memory['title']}\n{memory['content']}\n{' '.join(json.loads(memory['tags_json']))}"
        digest = hashlib.sha256(text.encode()).hexdigest()
        provider = self._provider_name()
        existing = cx.execute("SELECT provider,content_hash FROM memory_embeddings WHERE memory_id=?", (memory["id"],)).fetchone()
        if existing and existing["provider"] == provider and existing["content_hash"] == digest:
            return
        vector = self.embedding_provider.embed([text])[0]
        cx.execute("""INSERT INTO memory_embeddings(memory_id,provider,dimensions,content_hash,vector_json,updated_at)
          VALUES(?,?,?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET provider=excluded.provider,dimensions=excluded.dimensions,
          content_hash=excluded.content_hash,vector_json=excluded.vector_json,updated_at=excluded.updated_at""",
          (memory["id"], provider, self.embedding_provider.dimensions, digest, canonical(vector), now()))

    def transition(self, memory_id: str, status: str, related_memory_id: str | None = None, note: str = "") -> dict[str, Any]:
        if status not in {"active", "superseded", "disputed", "expired", "rejected"}: raise ValueError("invalid transition status")
        with self.tx() as cx:
            row = cx.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row: raise KeyError("memory not found")
            ts = now()
            if status == "active":
                cx.execute("UPDATE memories SET status=?,updated_at=?,last_confirmed_at=? WHERE id=?", (status, ts, ts, memory_id))
            else:
                cx.execute("UPDATE memories SET status=?,updated_at=? WHERE id=?", (status, ts, memory_id))
            relation = {"superseded": "supersedes", "disputed": "disputes"}.get(status)
            if relation and related_memory_id:
                other = cx.execute("SELECT project_id FROM memories WHERE id=?", (related_memory_id,)).fetchone()
                if not other or other["project_id"] != row["project_id"]: raise ValueError("related memory must be in same project")
                # New/contesting memory points to old/disputed memory.
                cx.execute("INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?,?,?)", (uid(), row["project_id"], related_memory_id, memory_id, relation, note, ts))
            result = dict(row); result["status"] = status; result["updated_at"] = ts
            if status in {"superseded","disputed","expired","rejected"}:
                result["stale_wiki_revision_ids"] = self._stale_wiki_revisions_for_memory(
                    cx, memory_id, f"cited memory became {status}")
            self._audit(cx, row["project_id"], "memory", memory_id, f"status:{status}", {"note": note, **result})
        return result

    def set_search_aliases(self, project_id: str, term: str, aliases: list[str]) -> dict[str, Any]:
        normalized = term.strip().casefold()
        values = sorted({value.strip().casefold() for value in aliases if value.strip()} - {normalized})
        if not normalized or not values:
            raise ValueError("term and at least one distinct alias are required")
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError("project not found")
        item = {"project_id":project_id,"term":normalized,"aliases_json":canonical(values),"updated_at":now()}
        existing = self._row("SELECT created_at FROM search_aliases WHERE project_id=? AND term=?", (project_id,normalized))
        item["created_at"] = existing["created_at"] if existing else item["updated_at"]
        with self.tx() as cx:
            cx.execute("""INSERT INTO search_aliases(project_id,term,aliases_json,created_at,updated_at) VALUES(:project_id,:term,:aliases_json,:created_at,:updated_at)
              ON CONFLICT(project_id,term) DO UPDATE SET aliases_json=excluded.aliases_json,updated_at=excluded.updated_at""", item)
            self._audit(cx, project_id, "search_alias", normalized, "updated" if existing else "created", item)
        return {**item, "aliases": values}

    def list_search_aliases(self, project_id: str) -> list[dict[str, Any]]:
        rows=[]
        for row in self.conn.execute("SELECT * FROM search_aliases WHERE project_id=? ORDER BY term", (project_id,)):
            item=dict(row); item["aliases"]=json.loads(item.pop("aliases_json")); rows.append(item)
        return rows

    def create_relation(self, project_id: str, from_memory_id: str, to_memory_id: str, relation: str, note: str = "") -> dict[str, Any]:
        if relation not in RELATIONS:
            raise ValueError("invalid relation")
        if from_memory_id == to_memory_id:
            raise ValueError("self relations are not allowed")
        endpoints = list(self.conn.execute("SELECT id,project_id FROM memories WHERE id IN (?,?)", (from_memory_id,to_memory_id)))
        if len(endpoints) != 2 or any(row["project_id"] != project_id for row in endpoints):
            raise ValueError("relation endpoints must be memories in the same project")
        item={"id":uid(),"project_id":project_id,"from_memory_id":from_memory_id,"to_memory_id":to_memory_id,"relation":relation,"note":note,"created_at":now()}
        with self.tx() as cx:
            cx.execute("INSERT INTO edges VALUES(:id,:project_id,:from_memory_id,:to_memory_id,:relation,:note,:created_at)", item)
            self._audit(cx, project_id, "edge", item["id"], "created", item)
        return item

    def traverse(self, project_id: str, memory_id: str, max_depth: int = 2, direction: str = "both",
                 relations: list[str] | None = None, statuses: list[str] | None = None) -> dict[str, Any]:
        if direction not in {"outgoing","incoming","both"}: raise ValueError("invalid direction")
        if not 1 <= max_depth <= 5: raise ValueError("max_depth must be 1..5")
        if relations and any(value not in RELATIONS for value in relations): raise ValueError("invalid relation filter")
        allowed_statuses = statuses or ["active","disputed"]
        start = self._row("SELECT * FROM memories WHERE id=? AND project_id=?", (memory_id,project_id))
        if not start: raise KeyError("memory not found")
        nodes={memory_id:{**start,"depth":0}}; selected_edges=[]; frontier={memory_id}
        for depth in range(1,max_depth+1):
            next_frontier=set()
            for current in frontier:
                clauses=[]; args=[]
                if direction in {"outgoing","both"}: clauses.append("from_memory_id=?"); args.append(current)
                if direction in {"incoming","both"}: clauses.append("to_memory_id=?"); args.append(current)
                sql="SELECT * FROM edges WHERE project_id=? AND ("+" OR ".join(clauses)+")"; params=[project_id,*args]
                if relations:
                    sql += " AND relation IN ("+",".join("?" for _ in relations)+")"; params.extend(relations)
                for edge_row in self.conn.execute(sql,params):
                    edge=dict(edge_row); other=edge["to_memory_id"] if edge["from_memory_id"]==current else edge["from_memory_id"]
                    node=self._row("SELECT * FROM memories WHERE id=? AND project_id=?",(other,project_id))
                    if not node or node["status"] not in allowed_statuses: continue
                    if edge["id"] not in {e["id"] for e in selected_edges}: selected_edges.append(edge)
                    if other not in nodes: nodes[other]={**node,"depth":depth}; next_frontier.add(other)
            frontier=next_frontier
            if not frontier: break
        return {"start_memory_id":memory_id,"max_depth":max_depth,"direction":direction,
                "nodes":sorted(nodes.values(),key=lambda x:(x["depth"],x["id"])),"edges":selected_edges}

    def search(self, project_id: str, query: str, limit: int = 10, statuses: list[str] | None = None,
               scope_id: str | None = None, discover_projects: bool = False) -> list[dict[str, Any]]:
        if not query.strip(): return []
        query_tokens = list(dict.fromkeys(re.findall(r"[\w-]+", query.casefold(), flags=re.UNICODE)))
        if not query_tokens: return []
        token_alternatives: list[list[str]] = []
        for token in query_tokens:
            alternatives = [token]
            row=self._row("SELECT aliases_json FROM search_aliases WHERE project_id=? AND term=?",(project_id,token))
            if row:
                for alias in json.loads(row["aliases_json"]):
                    alternatives.extend(re.findall(r"[\w-]+",alias,flags=re.UNICODE))
            token_alternatives.append(list(dict.fromkeys(alternatives)))
        quote = lambda value: '"' + value.replace('"', '""') + '"'
        strict_match = " AND ".join(
            ("(" + " OR ".join(quote(value) for value in alternatives) + ")")
            if len(alternatives) > 1 else quote(alternatives[0])
            for alternatives in token_alternatives)
        tokens = list(dict.fromkeys(value for alternatives in token_alternatives for value in alternatives))
        broad_match = " OR ".join(quote(token) for token in tokens)
        allowed = statuses or ["active", "proposed", "disputed"]
        placeholders = ",".join("?" for _ in allowed)
        timestamp = now()
        # Discovery is deliberately whole-database. Project identity hints are a
        # later prior, not a candidate-generation boundary: filtering here can
        # make the actually relevant project impossible to retrieve.
        boundary = "1=1" if discover_projects else "(m.project_id=? OR m.visibility='global')"
        boundary_args: list[Any] = [] if discover_projects else [project_id]
        lexical_sql = f"""SELECT m.*, bm25(memories_fts, 0, 5, 1, .5) AS fts_rank
          FROM memories_fts JOIN memories m ON m.id=memories_fts.memory_id
          WHERE memories_fts MATCH ? AND {boundary} AND m.status IN ({placeholders})
          AND (m.valid_from IS NULL OR m.valid_from<=?) AND (m.valid_until IS NULL OR m.valid_until>?)"""
        lexical_args: list[Any] = [*boundary_args, *allowed, timestamp, timestamp]
        if scope_id and not discover_projects:
            lexical_sql += " AND (m.scope_id=? OR m.scope_id IS NULL)"; lexical_args.append(scope_id)
        candidate_limit = max(20, min(max(1, limit) * 4, 200))
        lexical_sql += " ORDER BY bm25(memories_fts,0,5,1,.5) ASC LIMIT ?"
        strict = [dict(r) for r in self.conn.execute(
            lexical_sql, [strict_match, *lexical_args, candidate_limit])]
        strict_target = min(max(1, limit), candidate_limit)
        lexical_strategy = "strict"
        if len(strict) >= strict_target or strict_match == broad_match:
            lexical = strict
        else:
            lexical = [dict(r) for r in self.conn.execute(
                lexical_sql, [broad_match, *lexical_args, candidate_limit])]
            lexical_strategy = "broad_fallback"
        candidates = {row["id"]: row for row in lexical}
        components: dict[str, dict[str, float]] = {
            row["id"]: {"lexical_rrf": 1.0 / (60 + rank), "semantic_rrf": 0.0}
            for rank, row in enumerate(lexical, 1)
        }
        semantic_scores: dict[str, float] = {}
        semantic_scan = {"mode":"disabled", "candidate_limit":0, "time_limit_ms":0,
                         "evaluated":0, "truncated":False}
        if self.embedding_provider:
            query_vector = self.embedding_provider.embed([query])[0]
            vector_only_threshold = getattr(self.embedding_provider, "vector_only_threshold", None)
            supplements_lexical = bool(getattr(self.embedding_provider, "supplements_lexical_results", False))
            discovery_project_ids: list[str] | None = None
            sem_boundary = boundary
            if discover_projects:
                discovery_project_ids = self._discovery_project_candidates(project_id, query_tokens, lexical)
                if discovery_project_ids:
                    sem_boundary = "(m.project_id IN (" + ",".join("?" for _ in discovery_project_ids) + ") OR m.visibility='global')"
                else:
                    sem_boundary = "m.visibility='global'"
            sem_sql = f"""SELECT m.id, e.vector_json FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id
              WHERE {sem_boundary} AND m.status IN ({placeholders}) AND e.provider=? AND e.dimensions=?
              AND (m.valid_from IS NULL OR m.valid_from<=?) AND (m.valid_until IS NULL OR m.valid_until>?)"""
            sem_boundary_args = discovery_project_ids or [] if discover_projects else boundary_args
            sem_args: list[Any] = [*sem_boundary_args, *allowed, self._provider_name(), self.embedding_provider.dimensions, timestamp, timestamp]
            if scope_id and not discover_projects: sem_sql += " AND (m.scope_id=? OR m.scope_id IS NULL)"; sem_args.append(scope_id)
            if lexical and not supplements_lexical:
                lexical_ids = sorted(candidates)
                sem_sql += " AND m.id IN (" + ",".join("?" for _ in lexical_ids) + ")"
                sem_args.extend(lexical_ids)
                semantic_scan = {"mode":"lexical_rerank", "candidate_limit":len(lexical_ids),
                                 "time_limit_ms":0, "evaluated":0, "truncated":False}
                scan_deadline = None
            else:
                sem_sql += " ORDER BY m.id LIMIT ?"
                sem_args.append(LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT + 1)
                semantic_scan = {"mode":"vector_fallback", "candidate_limit":LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT,
                                 "time_limit_ms":LOCAL_HASH_FALLBACK_TIME_LIMIT_MS,
                                 "evaluated":0, "truncated":False}
                scan_deadline = time.perf_counter() + LOCAL_HASH_FALLBACK_TIME_LIMIT_MS / 1000
            if discover_projects:
                semantic_scan.update({"project_candidate_limit":DISCOVERY_PROJECT_CANDIDATE_LIMIT,
                                      "project_candidate_count":len(discovery_project_ids or []),
                                      "project_candidate_ids":discovery_project_ids or []})
            semantic: list[tuple[float, str]] = []
            for row in self.conn.execute(sem_sql, sem_args):
                if semantic_scan["evaluated"] >= semantic_scan["candidate_limit"]:
                    semantic_scan["truncated"] = True
                    break
                if scan_deadline is not None and time.perf_counter() >= scan_deadline:
                    semantic_scan["truncated"] = True
                    break
                semantic_scan["evaluated"] += 1
                vector = json.loads(row["vector_json"])
                similarity = sum(a * b for a, b in zip(query_vector, vector))
                # Weak similarities may rerank lexical hits. A provider may also
                # opt into vector-only recall with an explicit calibrated threshold;
                # this must remain available even when FTS returns an unrelated hit.
                if similarity > 0.05 and (
                    row["id"] in candidates
                    or (vector_only_threshold is not None
                        and (not lexical or supplements_lexical)
                        and len(query_tokens) >= 2
                        and similarity >= vector_only_threshold)
                ):
                    semantic.append((similarity, row["id"]))
            semantic.sort(key=lambda value: (-value[0], value[1]))
            selected_semantic = semantic[:candidate_limit]
            missing_ids = [memory_id for _, memory_id in selected_semantic if memory_id not in candidates]
            if missing_ids:
                missing_placeholders = ",".join("?" for _ in missing_ids)
                candidates.update({row["id"]: dict(row) for row in self.conn.execute(
                    f"SELECT * FROM memories WHERE id IN ({missing_placeholders})", missing_ids)})
            for rank, (similarity, memory_id) in enumerate(selected_semantic, 1):
                component = components.setdefault(memory_id, {"lexical_rrf": 0.0, "semantic_rrf": 0.0})
                component["semantic_rrf"] = 1.0 / (60 + rank)
                semantic_scores[memory_id] = similarity
        candidate_ids = list(candidates)
        usage: dict[str, dict[str, Any]] = {}
        sources: dict[str, list[dict[str, Any]]] = {memory_id: [] for memory_id in candidate_ids}
        if candidate_ids:
            candidate_placeholders = ",".join("?" for _ in candidate_ids)
            usage = {row["memory_id"]: dict(row) for row in self.conn.execute(
                f"SELECT * FROM memory_usage WHERE memory_id IN ({candidate_placeholders})", candidate_ids)}
            for source in self.conn.execute(f"""SELECT s.memory_id,e.id,e.kind,e.source_uri,e.created_at
              FROM memory_sources s JOIN events e ON e.id=s.event_id
              WHERE s.memory_id IN ({candidate_placeholders}) ORDER BY s.memory_id,e.id""", candidate_ids):
                item = dict(source)
                sources[item.pop("memory_id")].append(item)
        current = datetime.now(timezone.utc)
        for memory_id, row in candidates.items():
            confirmed = row.get("last_confirmed_at") or row.get("updated_at")
            try: age_days = max(0.0, (current - datetime.fromisoformat(confirmed)).total_seconds() / 86400) if confirmed else 3650.0
            except ValueError: age_days = 3650.0
            freshness = 1.0 / (1.0 + age_days / 180.0)
            stats = usage.get(memory_id, {})
            helpful = stats.get("helpful_count", 0) - stats.get("incorrect_count", 0) * 2
            component = components.setdefault(memory_id, {"lexical_rrf": 0.0, "semantic_rrf": 0.0})
            component.update({"importance": row["importance"] * .0015,
                              "confidence": row["confidence"] * .001,
                              "freshness": freshness * .0005,
                              "feedback": max(-5, min(5, helpful)) * .0002})
            component["total"] = sum(value for name, value in component.items() if name != "total")
        rows = sorted(candidates.values(), key=lambda row: (-components[row["id"]]["total"], row["id"]))[:max(1, min(limit, 100))]
        lexical_ranks = {row["id"]: rank for rank, row in enumerate(lexical, 1)}
        for r in rows:
            searchable_tokens = set(re.findall(
                r"[\w-]+", f"{r['title']} {r['content']} {r['tags_json']}".casefold(), flags=re.UNICODE))
            query_coverage = sum(token in searchable_tokens for token in query_tokens) / len(query_tokens)
            r["retrieval"] = {"score": components[r["id"]]["total"], "components": components[r["id"]],
                              "lexical_rank": lexical_ranks.get(r["id"]),
                              "lexical_strategy": lexical_strategy,
                              "semantic_scan":semantic_scan,
                              "query_coverage":query_coverage,
                              "semantic_similarity": semantic_scores.get(r["id"]), "embedding_provider": self._provider_name()}
            r["usage"] = usage.get(r["id"], {"retrieved_count":0,"used_count":0,"helpful_count":0,"incorrect_count":0})
            r["sources"] = sources[r["id"]]
        return rows

    def _aggregate_project_candidates(self, memories: list[dict[str, Any]], current_project_id: str) -> list[dict[str, Any]]:
        """Aggregate whole-DB memory relevance and recent project activity."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for memory in memories:
            if memory["project_id"] != current_project_id and memory["visibility"] == "project":
                grouped.setdefault(memory["project_id"], []).append(memory)
        candidates = []
        source_aliases = {(row["kind"], row["normalized"]) for row in self.conn.execute(
            "SELECT kind,normalized FROM project_aliases WHERE project_id=?", (current_project_id,))}
        current = datetime.now(timezone.utc)
        for candidate_id, matches in grouped.items():
            project = self._row("SELECT id,slug,name,description FROM projects WHERE id=?", (candidate_id,))
            activity = self._row("""SELECT MAX(activity_at) AS activity_at FROM (
              SELECT MAX(COALESCE(ended_at,started_at)) AS activity_at FROM sessions WHERE project_id=?
              UNION ALL SELECT MAX(created_at) FROM events WHERE project_id=?
            )""", (candidate_id, candidate_id))
            checkpoint = self._row("""SELECT id,title,type,status,updated_at FROM memories
              WHERE project_id=? AND status IN ('active','disputed') AND type IN ('task','summary')
              ORDER BY updated_at DESC,id LIMIT 1""", (candidate_id,))
            # Hybrid RRF should improve ordering within a project, but counting
            # the same hit twice would dilute path/name priors during project
            # selection. Collapse the overlapping lexical/vector contribution.
            relevance_scores = sorted((
                m["retrieval"]["score"] - min(
                    m["retrieval"]["components"].get("lexical_rrf", 0.0),
                    m["retrieval"]["components"].get("semantic_rrf", 0.0),
                ) for m in matches
            ), reverse=True)
            relevance = sum(score / (index + 1) for index, score in enumerate(relevance_scores))
            evidence_quality = max(max(m["retrieval"].get("query_coverage", 0.0),
                                       m["retrieval"].get("semantic_similarity") or 0.0) for m in matches)
            activity_at = activity["activity_at"] if activity else None
            try:
                age_days = max(0.0, (current - datetime.fromisoformat(activity_at)).total_seconds() / 86400) if activity_at else None
            except ValueError:
                age_days = None
            recency = 0.0 if age_days is None else 1.0 / (1.0 + age_days / 30.0)
            candidate_aliases = {(row["kind"], row["normalized"]) for row in self.conn.execute(
                "SELECT kind,normalized FROM project_aliases WHERE project_id=?", (candidate_id,))}
            shared_aliases = source_aliases & candidate_aliases
            identity_prior = .35 if any(kind == "path" for kind, _ in shared_aliases) else (
                .15 if any(kind == "name" for kind, _ in shared_aliases) else 0.0)
            # A single strong lexical/local-vector hit is approximately 1/61.
            # Normalize the aggregate before adding bounded identity and activity
            # priors so registry size and raw RRF scale do not leak into confidence.
            relevance_confidence = min(1.0, relevance / .02) * evidence_quality
            confidence = min(1.0, relevance_confidence * .75 + identity_prior + recency * .05)
            reasons = ["memory_relevance"]
            if identity_prior: reasons.append("shared_path" if identity_prior == .35 else "shared_name")
            if recency: reasons.append("recent_activity")
            candidates.append({**project, "relevance":relevance, "matching_memory_count":len(matches),
                               "top_memory_score":relevance_scores[0], "recent_activity_at":activity_at,
                               "recency":recency, "identity_prior":identity_prior,
                               "evidence_quality":evidence_quality,
                               "confidence":confidence, "confidence_reasons":reasons,
                               "latest_checkpoint":checkpoint})
        return sorted(candidates, key=lambda item: (-item["confidence"], -item["relevance"], item["id"]))

    @staticmethod
    def _select_project_candidate(candidates: list[dict[str, Any]]) -> tuple[str | None, str, float]:
        """Select only a sufficiently strong and separated project candidate."""
        if not candidates: return None, "no_candidates", 0.0
        top = candidates[0]
        if top["confidence"] < DISCOVERY_MIN_CONFIDENCE:
            return None, "low_confidence", top["confidence"]
        if len(candidates) == 1:
            return top["id"], "single_confident_candidate", top["confidence"]
        margin = top["confidence"] - candidates[1]["confidence"]
        if top["confidence"] >= DISCOVERY_AUTO_SELECT_CONFIDENCE and margin >= DISCOVERY_MIN_MARGIN:
            return top["id"], "dominant_candidate", top["confidence"]
        return None, "ambiguous_candidates", top["confidence"]

    def record_memory_feedback(self, memory_id: str, signal: str) -> dict[str, Any]:
        if signal not in {"retrieved", "used", "helpful", "incorrect"}:
            raise ValueError("signal must be retrieved, used, helpful, or incorrect")
        memory = self._row("SELECT project_id FROM memories WHERE id=?", (memory_id,))
        if not memory: raise KeyError("memory not found")
        ts = now(); column = signal + "_count"
        with self.tx() as cx:
            cx.execute("""INSERT OR IGNORE INTO memory_usage(memory_id,updated_at) VALUES(?,?)""", (memory_id, ts))
            updates = f"{column}={column}+1,updated_at=?"
            if signal == "retrieved": updates += ",last_retrieved_at=?"
            if signal == "used": updates += ",last_used_at=?"
            values: list[Any] = [ts]
            if signal in {"retrieved", "used"}: values.append(ts)
            values.append(memory_id)
            cx.execute(f"UPDATE memory_usage SET {updates} WHERE memory_id=?", values)
            result = dict(cx.execute("SELECT * FROM memory_usage WHERE memory_id=?", (memory_id,)).fetchone())
            delta = {"used":.005, "helpful":.02, "incorrect":-.05}.get(signal, 0.0)
            if delta:
                cx.execute("UPDATE memories SET importance=max(0,min(1,importance+?)),updated_at=? WHERE id=?", (delta, ts, memory_id))
                result["importance"] = cx.execute("SELECT importance FROM memories WHERE id=?", (memory_id,)).fetchone()[0]
            self._audit(cx, memory["project_id"], "memory_feedback", memory_id, signal, result)
        return result

    @staticmethod
    def _retrieval_gate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """Reject weak vector-only retrieval while leaving lexical recall unchanged."""
        thresholds = {"vector_only_similarity":NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY,
                      "vector_only_separation":NEGATIVE_VECTOR_ONLY_MIN_SEPARATION}
        if not candidates:
            return {"status":"no_confident_match", "reason":"no_candidates", "components":{
                "lexical_rank":None, "query_coverage":0.0, "semantic_similarity":None,
                "lexical_vector_agreement":False, "top_score":None, "runner_up_score":None,
                "score_margin":None, "semantic_separation":None}, "thresholds":thresholds}
        top = candidates[0]["retrieval"]
        runner = candidates[1]["retrieval"] if len(candidates) > 1 else None
        top_score = float(top["score"]); runner_score = float(runner["score"]) if runner else None
        similarity = top.get("semantic_similarity")
        runner_similarity = runner.get("semantic_similarity") if runner else None
        semantic_separation = (float(similarity) - float(runner_similarity)
                               if similarity is not None and runner_similarity is not None
                               else float(similarity or 0.0))
        components = {"lexical_rank":top.get("lexical_rank"), "query_coverage":top.get("query_coverage", 0.0),
                      "semantic_similarity":similarity,
                      "lexical_vector_agreement":bool(top.get("lexical_rank") is not None and similarity is not None),
                      "top_score":top_score, "runner_up_score":runner_score,
                      "score_margin":top_score - runner_score if runner_score is not None else top_score,
                      "semantic_separation":semantic_separation}
        if top.get("lexical_rank") is not None:
            return {"status":"accepted", "reason":"lexical_match", "components":components, "thresholds":thresholds}
        if similarity is None or similarity < NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY:
            return {"status":"no_confident_match", "reason":"weak_vector_only_similarity",
                    "components":components, "thresholds":thresholds}
        if runner is not None and semantic_separation < NEGATIVE_VECTOR_ONLY_MIN_SEPARATION:
            return {"status":"no_confident_match", "reason":"weak_vector_only_separation",
                    "components":components, "thresholds":thresholds}
        return {"status":"accepted", "reason":"strong_vector_only_match", "components":components,
                "thresholds":thresholds}

    def get_context(self, project_id: str, query: str, char_budget: int = 6000, statuses: list[str] | None = None,
                    scope_id: str | None = None, event_cursor: int | None = None, event_kinds: list[str] | None = None,
                    event_limit: int = 20, event_char_budget: int = 2000, discover_projects: bool = True,
                    response_format: str = "legacy") -> dict[str, Any]:
        if response_format not in {"legacy", "compact"}:
            raise ValueError("response_format must be legacy or compact")
        policy = self.get_policy(project_id)
        requested = max(0, char_budget)
        budget = min(requested, policy["max_context_chars"]); selected, used = [], 0
        recent_events: list[dict[str, Any]] = []; event_used = 0; event_result = None
        reserved = 0
        if event_cursor is not None:
            reserved = min(max(0, event_char_budget), 4000, budget)
            selected_kinds = ["message"] if event_kinds is None else event_kinds
            event_result = self.read_events_since(project_id, event_cursor, selected_kinds, scope_id, event_limit)
            for event in event_result["events"]:
                prefix = f"[{event['event_seq']}/{event['kind']}] "
                remaining = reserved - event_used - len(prefix)
                if remaining <= 0: break
                content = event["content"]
                truncated = len(content) > remaining
                text = prefix + (content[:max(0, remaining - 1)] + "…" if truncated and remaining else content)
                recent_events.append({"event_id":event["id"],"event_seq":event["event_seq"],"kind":event["kind"],
                                      "text":text,"created_at":event["created_at"],"session_id":event["session_id"],
                                      "scope_id":event["scope_id"],"metadata":event["metadata"],"content_truncated":truncated})
                event_used += len(text)
            fully_consumed = len(recent_events) == len(event_result["events"])
            event_result["next_cursor"] = event_result["next_cursor"] if fully_consumed else (recent_events[-1]["event_seq"] if recent_events else event_cursor)
            event_result["has_more"] = event_result["has_more"] or not fully_consumed
        memory_budget = budget - event_used
        selected_texts: list[str] = []
        candidates = self.search(project_id, query, policy["max_context_items"] * 3, statuses or ["active", "disputed"], scope_id)
        retrieval_gate = self._retrieval_gate(candidates)
        if retrieval_gate["status"] == "no_confident_match": candidates = []
        local_matches = [m for m in candidates if m["project_id"] == project_id]
        discovery_used = bool(discover_projects and not local_matches)
        discovery_candidates: list[dict[str, Any]] = []
        if discovery_used:
            discovery_candidates = self.search(project_id, query, policy["max_context_items"] * 3, statuses or ["active", "disputed"], None, True)
            discovery_gate = self._retrieval_gate(discovery_candidates)
            if discovery_gate["status"] == "no_confident_match": discovery_candidates = []
            seen = {m["id"] for m in candidates}
            candidates.extend(m for m in discovery_candidates if m["id"] not in seen)
        project_candidates = self._aggregate_project_candidates(discovery_candidates, project_id)
        selected_project_id, selection_reason, discovery_confidence = self._select_project_candidate(project_candidates)
        discovery_ambiguous = selection_reason == "ambiguous_candidates"
        if discovery_used:
            candidates = [m for m in candidates if m["project_id"] == project_id or m["visibility"] == "global"
                          or m["project_id"] == selected_project_id]
        eligible = 0
        for m in candidates:
            block = f"[{m['status']}/{m['type']}] {m['title']}\n{m['content']}\nsource_events: {', '.join(s['id'] for s in m['sources']) or 'none'}"
            comparable = f"{m['title']} {m['content']}"
            if any(self._text_similarity(comparable, previous) >= .8 for previous in selected_texts): continue
            eligible += 1
            if len(selected) >= policy["max_context_items"]: continue
            if used + len(block) + 2 > memory_budget: continue
            item = {"memory_id": m["id"], "project_id":m["project_id"], "visibility":m["visibility"],
                    "confidence":m["confidence"], "importance":m["importance"]}
            if response_format == "legacy":
                item["text"] = block
            else:
                item.update({"status":m["status"], "type":m["type"], "title":m["title"], "content":m["content"],
                             "source_event_ids":[s["id"] for s in m["sources"]], "tags":json.loads(m["tags_json"]),
                             "observed_at":m["observed_at"], "valid_from":m["valid_from"], "valid_until":m["valid_until"],
                             "last_confirmed_at":m["last_confirmed_at"], "truncated":False})
            selected.append(item)
            selected_texts.append(comparable)
            used += len(block) + 2
        result = {"query": query, "requested_budget": requested, "budget": budget, "budget_capped": requested > budget,
                "max_items": policy["max_context_items"], "memory_budget":memory_budget,"event_budget":reserved,
                "used": used + event_used, "memory_used":used,"event_used":event_used,
                "items": selected, "recent_events":recent_events,
                "retrieval_gate":retrieval_gate,
                "project_discovery":{"enabled":discover_projects,"used":discovery_used,"ambiguous":discovery_ambiguous,
                                     "project_ids":list(dict.fromkeys(i["project_id"] for i in selected if i["project_id"] != project_id)),
                                     "selected_project_id":selected_project_id,"confidence":discovery_confidence,
                                     "selection_reason":selection_reason,"candidates":project_candidates},
                "event_cursor":event_cursor,"next_event_cursor":event_result["next_cursor"] if event_result else None,
                "event_snapshot_cursor":event_result["snapshot_cursor"] if event_result else None,
                "has_more_events":event_result["has_more"] if event_result else False,
                "response_format":response_format,"truncated":eligible > len(selected),"has_more":eligible > len(selected)}
        if response_format == "legacy":
            result["context"] = "\n\n".join(i["text"] for i in selected)
        return result

    def decision_context(self, project_id: str, question: str, char_budget: int = 6000,
                         scope_id: str | None = None, discover_projects: bool = True) -> dict[str, Any]:
        """Compose a cited, read-only Decision Brief from the existing retrieval path."""
        context = self.get_context(
            project_id, question, char_budget,
            statuses=["active", "disputed", "proposed", "superseded", "rejected", "expired"],
            scope_id=scope_id, discover_projects=discover_projects, response_format="compact",
        )
        context["items"] = self._rerank_decision_candidates(question, context["items"])
        context["decision_rerank"] = {
            "mode":"bounded_post_retrieval", "candidate_count":len(context["items"]),
            "general_search_unchanged":True,
        }
        self._expand_decision_seeds(project_id, context, scope_id)
        sections: dict[str, list[dict[str, Any]]] = {
            "current_decisions": [], "rationale": [], "constraints": [], "alternatives": [],
            "outcomes": [], "history": [], "disputes": [], "open_questions": [],
        }
        citations: dict[str, dict[str, Any]] = {}
        uncertain: list[dict[str, Any]] = []
        for memory in context["items"]:
            tags = {tag.casefold().replace("_", "-") for tag in memory.get("tags", [])}
            entry = {
                "claim": memory["content"], "title": memory["title"], "status": memory["status"],
                "memory_type": memory["type"], "observed_at": memory.get("observed_at"),
                "citations": {"memory_id": memory["memory_id"], "source_event_ids": memory["source_event_ids"]},
            }
            citations[memory["memory_id"]] = entry["citations"]
            if memory["status"] == "disputed":
                sections["disputes"].append(entry)
            if memory["status"] == "proposed":
                uncertain.append({**entry, "reason": "unreviewed_proposed_memory", "kind": "evidence_state"})
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
            if memory["type"] == "decision" and memory["status"] in {"active", "superseded", "rejected", "disputed"}:
                sections["history"].append(entry)
            if not memory["source_event_ids"]:
                uncertain.append({**entry, "reason": "missing_source_event", "kind": "evidence_gap"})
        sections["history"].sort(key=lambda item: (item["observed_at"] or "", item["citations"]["memory_id"]))
        if not sections["current_decisions"]:
            uncertain.append({"kind":"retrieval_gap", "reason":"no_current_decision_retrieved", "citations":None})
        elif not sections["rationale"]:
            uncertain.append({"kind":"evidence_gap", "reason":"missing_rationale", "citations":None})
        return {
            "contract_version": "decision-brief/v1", "question": question, **sections,
            "expected_vs_observed": self._decision_outcome_comparisons(
                [item["memory_id"] for item in context["items"]]
            ),
            "uncertainty": uncertain, "citation_index": citations,
            "retrieval": context,
            "recommendation": None,
        }

    @staticmethod
    def _rerank_decision_candidates(question: str, memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rerank only a Decision Brief's already bounded retrieval results."""
        question_tokens = set(re.findall(r"[\w-]+", question.casefold(), flags=re.UNICODE))
        intent_terms = {
            "decision":{"choose","choice","decision","decide","selected","선택","결정"},
            "rationale":{"why","reason","rationale","because","근거","이유"},
            "constraint":{"constraint","requirement","limit","must","제약","요구사항"},
            "alternative":{"alternative","option","instead","rejected","대안","후보"},
            "outcome":{"outcome","result","impact","effect","measured","결과","효과","성과"},
        }
        requested_roles = {role for role, terms in intent_terms.items() if question_tokens & terms}
        current = datetime.now(timezone.utc)
        ranked: list[dict[str, Any]] = []
        for base_rank, memory in enumerate(memories, 1):
            tags = {tag.casefold().replace("_", "-") for tag in memory.get("tags", [])}
            roles: set[str] = set()
            if memory["type"] == "decision" and memory["status"] == "active": roles.add("decision")
            if memory["type"] == "constraint": roles.add("constraint")
            if memory["status"] == "rejected" or "alternative" in tags: roles.add("alternative")
            if tags & {"rationale","reason"}: roles.add("rationale")
            if tags & {"outcome","observed-outcome"}: roles.add("outcome")
            components = {
                "base_reciprocal_rank":1.0 / (60 + base_rank),
                "question_intent":.006 if requested_roles & roles else 0.0,
                "memory_type_status":.005 if "decision" in roles else (
                    .003 if memory["status"] in {"active","disputed"} else 0.0),
                "direct_provenance":.004 if memory.get("source_event_ids") else 0.0,
                "decision_role":.004 if roles else 0.0,
                "unsupported_penalty":-.006 if not memory.get("source_event_ids") else 0.0,
                "stale_proposed_penalty":0.0,
                "repetitive_handoff_penalty":0.0,
            }
            if memory["status"] == "proposed":
                confirmed = memory.get("last_confirmed_at") or memory.get("observed_at")
                try:
                    stale = not confirmed or (current - datetime.fromisoformat(confirmed)).total_seconds() > 180 * 86400
                except ValueError:
                    stale = True
                if stale: components["stale_proposed_penalty"] = -.005
            handoff_markers = {"handoff","checkpoint","summary","next-step"}
            if (memory["type"] in {"task","summary"}
                    and (tags & handoff_markers or any(marker in memory["title"].casefold() for marker in handoff_markers))):
                components["repetitive_handoff_penalty"] = -.004
            components["total"] = sum(value for name, value in components.items() if name != "total")
            item = dict(memory)
            item["decision_rerank"] = {"score":components["total"], "components":components,
                                       "roles":sorted(roles), "base_rank":base_rank}
            ranked.append(item)
        return sorted(ranked, key=lambda item: (-item["decision_rerank"]["score"],
                                                item["decision_rerank"]["base_rank"], item["memory_id"]))

    def _expand_decision_seeds(self, project_id: str, context: dict[str, Any],
                               scope_id: str | None) -> None:
        """Add a bounded one-hop evidence expansion without escaping context budgets."""
        seed_limit = 3
        candidate_limit = 50
        seeds = [item for item in context["items"]
                 if item["type"] == "decision" and item["status"] == "active"][:seed_limit]
        seed_ids = [item["memory_id"] for item in seeds]
        diagnostics = {
            "mode":"one_hop", "seed_limit":seed_limit, "candidate_limit":candidate_limit,
            "seed_memory_ids":seed_ids, "considered":0, "added":0,
            "item_limit":context["max_items"], "depth":1, "truncated":False,
        }
        context["decision_expansion"] = diagnostics
        if not seed_ids:
            diagnostics["reason"] = "no_current_decision_seeds"
            return
        placeholders = ",".join("?" for _ in seed_ids)
        candidates: dict[str, dict[str, Any]] = {}

        def add_candidate(memory_id: str, priority: int, path: dict[str, Any]) -> None:
            if memory_id in seed_ids: return
            candidate = candidates.setdefault(memory_id, {"priority":priority, "paths":[]})
            candidate["priority"] = min(candidate["priority"], priority)
            if path not in candidate["paths"]: candidate["paths"].append(path)

        relation_priority = {"supports":0, "depends_on":1, "supersedes":2}
        for edge in self.conn.execute(f"""SELECT * FROM edges WHERE project_id=?
          AND relation IN ('supports','depends_on','supersedes')
          AND (from_memory_id IN ({placeholders}) OR to_memory_id IN ({placeholders}))
          ORDER BY relation,created_at,id LIMIT ?""", (project_id, *seed_ids, *seed_ids, candidate_limit + 1)):
            seed_id = edge["from_memory_id"] if edge["from_memory_id"] in seed_ids else edge["to_memory_id"]
            other_id = edge["to_memory_id"] if seed_id == edge["from_memory_id"] else edge["from_memory_id"]
            add_candidate(other_id, relation_priority[edge["relation"]], {
                "kind":"memory_relation", "relation":edge["relation"], "seed_memory_id":seed_id,
                "direction":"outgoing" if seed_id == edge["from_memory_id"] else "incoming",
            })
        for row in self.conn.execute(f"""SELECT DISTINCT sc.memory_id seed_memory_id,oc.memory_id,
          i.id investigation_id,l.relation
          FROM investigation_claims sc
          JOIN investigations i ON i.id=sc.investigation_id
          JOIN investigation_claims oc ON oc.investigation_id=sc.investigation_id AND oc.memory_id<>sc.memory_id
          LEFT JOIN investigation_claim_links l ON
            (l.from_claim_id=sc.id AND l.to_claim_id=oc.id) OR (l.to_claim_id=sc.id AND l.from_claim_id=oc.id)
          WHERE i.project_id=? AND sc.memory_id IN ({placeholders})
          ORDER BY i.id,oc.created_at,oc.id LIMIT ?""", (project_id, *seed_ids, candidate_limit + 1)):
            add_candidate(row["memory_id"], 3 if row["relation"] else 4, {
                "kind":"investigation_relation" if row["relation"] else "shared_investigation",
                "relation":row["relation"], "seed_memory_id":row["seed_memory_id"],
                "investigation_id":row["investigation_id"],
            })
        ordered = sorted(candidates.items(), key=lambda item: (item[1]["priority"], item[0]))
        if len(ordered) > candidate_limit:
            diagnostics["truncated"] = True
            ordered = ordered[:candidate_limit]
        diagnostics["considered"] = len(ordered)
        existing_ids = {item["memory_id"] for item in context["items"]}
        existing_by_id = {item["memory_id"]:item for item in context["items"]}
        for memory_id, expansion in ordered:
            if memory_id in existing_by_id:
                existing_by_id[memory_id]["decision_expansion"] = {
                    "depth":1, "already_retrieved":True, "paths":expansion["paths"],
                }
        remaining_ids = [memory_id for memory_id, _ in ordered if memory_id not in existing_ids]
        rows: dict[str, dict[str, Any]] = {}
        sources: dict[str, list[str]] = {memory_id: [] for memory_id in remaining_ids}
        if remaining_ids:
            remaining_placeholders = ",".join("?" for _ in remaining_ids)
            scope_clause = "" if scope_id is None else " AND (scope_id=? OR scope_id IS NULL)"
            timestamp = now()
            params: list[Any] = [*remaining_ids, timestamp, timestamp]
            if scope_id is not None: params.append(scope_id)
            rows = {row["id"]:dict(row) for row in self.conn.execute(
                f"""SELECT * FROM memories WHERE id IN ({remaining_placeholders})
                AND (valid_from IS NULL OR valid_from<=?) AND (valid_until IS NULL OR valid_until>?)
                {scope_clause}""", params)}
            for source in self.conn.execute(f"""SELECT s.memory_id,s.event_id FROM memory_sources s
              WHERE s.memory_id IN ({remaining_placeholders}) ORDER BY s.memory_id,s.event_id""", remaining_ids):
                sources[source["memory_id"]].append(source["event_id"])
        path_by_id = dict(ordered)
        for memory_id in remaining_ids:
            row = rows.get(memory_id)
            if not row: continue
            block = f"[{row['status']}/{row['type']}] {row['title']}\n{row['content']}\nsource_events: {', '.join(sources[memory_id]) or 'none'}"
            if len(context["items"]) >= context["max_items"] or context["memory_used"] + len(block) + 2 > context["memory_budget"]:
                diagnostics["truncated"] = True
                continue
            context["items"].append({
                "memory_id":row["id"], "project_id":row["project_id"], "visibility":row["visibility"],
                "confidence":row["confidence"], "importance":row["importance"], "status":row["status"],
                "type":row["type"], "title":row["title"], "content":row["content"],
                "source_event_ids":sources[memory_id], "tags":json.loads(row["tags_json"]),
                "observed_at":row["observed_at"], "valid_from":row["valid_from"], "valid_until":row["valid_until"],
                "last_confirmed_at":row["last_confirmed_at"], "truncated":False,
                "decision_expansion":{"depth":1, "paths":path_by_id[memory_id]["paths"]},
            })
            context["memory_used"] += len(block) + 2
            context["used"] += len(block) + 2
            diagnostics["added"] += 1
        if diagnostics["truncated"]:
            context["has_more"] = context["truncated"] = True

    def _decision_outcome_comparisons(self, retrieved_memory_ids: list[str]) -> list[dict[str, Any]]:
        if not retrieved_memory_ids: return []
        placeholders = ",".join("?" for _ in retrieved_memory_ids)
        rows = self.conn.execute(f"""SELECT d.memory_id decision_memory_id,d.expected_outcome,
          o.memory_id outcome_memory_id,o.outcome_effect,om.content observed_outcome,
          d.event_id decision_event_id,o.event_id outcome_event_id
          FROM investigation_claim_links l
          JOIN investigation_claims d ON d.id=l.from_claim_id AND d.role='decision'
          JOIN investigation_claims o ON o.id=l.to_claim_id AND o.role='outcome'
          JOIN memories om ON om.id=o.memory_id
          WHERE (d.memory_id IN ({placeholders}) OR o.memory_id IN ({placeholders}))
          ORDER BY o.created_at,o.id""", (*retrieved_memory_ids, *retrieved_memory_ids))
        return [{"expected_outcome":row["expected_outcome"],"observed_outcome":row["observed_outcome"],
                 "effect":row["outcome_effect"],"decision_citation":{"memory_id":row["decision_memory_id"],
                 "source_event_ids":[row["decision_event_id"]]},"outcome_citation":{"memory_id":row["outcome_memory_id"],
                 "source_event_ids":[row["outcome_event_id"]]}} for row in rows]

    def get_policy(self, project_id: str) -> dict[str, Any]:
        item = self._row("SELECT * FROM project_policies WHERE project_id=?", (project_id,))
        if not item: raise KeyError("project not found")
        return item

    def set_policy(self, project_id: str, max_context_chars: int | None = None, max_context_items: int | None = None,
                   audit_keep_entries: int | None = None, terminal_memory_days: int | None = None,
                   checkpoint_soft_usage: float | None = None, checkpoint_hard_usage: float | None = None,
                   checkpoint_elapsed_seconds: int | None = None, checkpoint_event_count: int | None = None,
                   checkpoint_max_age_seconds: int | None = None, checkpoint_cooldown_seconds: int | None = None,
                   checkpoint_hysteresis: float | None = None, maintenance_interval_seconds: int | None = None,
                   message_ttl_seconds: int | None = None) -> dict[str, Any]:
        current = self.get_policy(project_id)
        values = {"max_context_chars":max_context_chars,"max_context_items":max_context_items,
                  "audit_keep_entries":audit_keep_entries,"terminal_memory_days":terminal_memory_days,
                  "checkpoint_soft_usage":checkpoint_soft_usage,"checkpoint_hard_usage":checkpoint_hard_usage,
                  "checkpoint_elapsed_seconds":checkpoint_elapsed_seconds,"checkpoint_event_count":checkpoint_event_count,
                  "checkpoint_max_age_seconds":checkpoint_max_age_seconds,"checkpoint_cooldown_seconds":checkpoint_cooldown_seconds,
                  "checkpoint_hysteresis":checkpoint_hysteresis,"maintenance_interval_seconds":maintenance_interval_seconds,
                  "message_ttl_seconds":message_ttl_seconds}
        limits = {"max_context_chars":(1000,20000),"max_context_items":(1,50),"audit_keep_entries":(100,100000),"terminal_memory_days":(1,3650),
                  "checkpoint_soft_usage":(0,1),"checkpoint_hard_usage":(0,1),"checkpoint_elapsed_seconds":(60,86400),
                  "checkpoint_event_count":(1,10000),"checkpoint_max_age_seconds":(60,604800)}
        limits.update({"checkpoint_cooldown_seconds":(0,86400),"checkpoint_hysteresis":(0,.5),"message_ttl_seconds":(0,2592000)})
        for key, value in values.items():
            if value is not None:
                if key == "maintenance_interval_seconds":
                    if value != 0 and not 300 <= value <= 2592000: raise ValueError(f"{key} must be 0 or 300..2592000")
                else:
                    low, high = limits[key]
                    if not low <= value <= high: raise ValueError(f"{key} must be {low}..{high}")
                current[key] = value
        if current["checkpoint_soft_usage"] >= current["checkpoint_hard_usage"]:
            raise ValueError("checkpoint_soft_usage must be less than checkpoint_hard_usage")
        current["updated_at"] = now()
        with self.tx() as cx:
            cx.execute("""UPDATE project_policies SET max_context_chars=:max_context_chars,max_context_items=:max_context_items,
              audit_keep_entries=:audit_keep_entries,terminal_memory_days=:terminal_memory_days,
              checkpoint_soft_usage=:checkpoint_soft_usage,checkpoint_hard_usage=:checkpoint_hard_usage,
              checkpoint_elapsed_seconds=:checkpoint_elapsed_seconds,checkpoint_event_count=:checkpoint_event_count,
              checkpoint_max_age_seconds=:checkpoint_max_age_seconds,checkpoint_cooldown_seconds=:checkpoint_cooldown_seconds,
              checkpoint_hysteresis=:checkpoint_hysteresis,maintenance_interval_seconds=:maintenance_interval_seconds,
              message_ttl_seconds=:message_ttl_seconds,
              updated_at=:updated_at WHERE project_id=:project_id""", current)
            self._audit(cx, project_id, "policy", project_id, "updated", current)
        return current

    def search_health(self, project_id: str) -> dict[str, Any]:
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)): raise KeyError("project not found")
        memories = self.conn.execute("SELECT count(*) FROM memories WHERE project_id=?", (project_id,)).fetchone()[0]
        indexed = self.conn.execute("SELECT count(*) FROM memories_fts f JOIN memories m ON m.id=f.memory_id WHERE m.project_id=?", (project_id,)).fetchone()[0]
        missing = self.conn.execute("SELECT count(*) FROM memories m WHERE m.project_id=? AND NOT EXISTS(SELECT 1 FROM memories_fts f WHERE f.memory_id=m.id)", (project_id,)).fetchone()[0]
        duplicate = self.conn.execute("""SELECT count(*) FROM (SELECT f.memory_id FROM memories_fts f JOIN memories m ON m.id=f.memory_id
          WHERE m.project_id=? GROUP BY f.memory_id HAVING count(*)<>1)""", (project_id,)).fetchone()[0]
        orphan = self.conn.execute("SELECT count(*) FROM memories_fts f LEFT JOIN memories m ON m.id=f.memory_id WHERE m.id IS NULL").fetchone()[0]
        embedding = {"enabled":bool(self.embedding_provider),"provider":self._provider_name(),"indexed_rows":0,"missing":0,"stale":0}
        if self.embedding_provider:
            embedding["indexed_rows"] = self.conn.execute("""SELECT count(*) FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id
              WHERE m.project_id=? AND e.provider=? AND e.dimensions=?""", (project_id,self._provider_name(),self.embedding_provider.dimensions)).fetchone()[0]
            embedding["missing"] = memories - embedding["indexed_rows"]
            for row in self.conn.execute("""SELECT m.*,e.content_hash FROM memories m JOIN memory_embeddings e ON e.memory_id=m.id
              WHERE m.project_id=? AND e.provider=?""", (project_id,self._provider_name())):
                text=f"{row['title']}\n{row['content']}\n{' '.join(json.loads(row['tags_json']))}"
                if hashlib.sha256(text.encode()).hexdigest() != row["content_hash"]: embedding["stale"] += 1
        ok = missing==0 and duplicate==0 and orphan==0 and indexed==memories and (not self.embedding_provider or (embedding["missing"]==0 and embedding["stale"]==0))
        return {"ok":ok,"project_id":project_id,"memories":memories,"indexed_rows":indexed,"missing":missing,
                "duplicate_memory_ids":duplicate,"orphan_rows":orphan,"embeddings":embedding}

    def get_source(self, event_id: str) -> dict[str, Any]:
        item = self._row("SELECT * FROM events WHERE id=?", (event_id,))
        if not item: raise KeyError("source event not found")
        return item

    def maintain(self, project_id: str, apply: bool = False) -> dict[str, Any]:
        """Bound operational state while preserving events and checkpointing pruned audit detail."""
        policy = self.get_policy(project_id)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=policy["terminal_memory_days"])).isoformat()
        terminal = [dict(row) for row in self.conn.execute("""SELECT * FROM memories WHERE project_id=?
          AND status IN ('superseded','rejected','expired') AND updated_at<? ORDER BY updated_at,id""", (project_id,cutoff))]
        audit_total = self.conn.execute("SELECT count(*) FROM audit_log WHERE project_id=?", (project_id,)).fetchone()[0]
        # Purge audit records only after accounting for one purge audit entry per terminal memory.
        projected_total = audit_total + len(terminal)
        prune_count = max(0, projected_total - policy["audit_keep_entries"])
        plan = {"project_id":project_id,"apply":apply,"policy":policy,"terminal_cutoff":cutoff,
                "terminal_memories":len(terminal),"audit_entries":audit_total,"audit_entries_to_checkpoint":prune_count}
        if not apply: return plan
        checkpoint = None
        with self.tx() as cx:
            for memory in terminal:
                sources = [row[0] for row in cx.execute("SELECT event_id FROM memory_sources WHERE memory_id=? ORDER BY event_id", (memory["id"],))]
                self._audit(cx, project_id, "memory", memory["id"], "purged_terminal", {**memory,"source_event_ids":sources})
                cx.execute("DELETE FROM memories WHERE id=?", (memory["id"],))
            total = cx.execute("SELECT count(*) FROM audit_log WHERE project_id=?", (project_id,)).fetchone()[0]
            prune_count = max(0, total - policy["audit_keep_entries"])
            if prune_count:
                rows = [dict(row) for row in cx.execute("SELECT * FROM audit_log WHERE project_id=? ORDER BY seq LIMIT ?", (project_id,prune_count))]
                previous = cx.execute("SELECT digest FROM audit_checkpoints WHERE project_id=? ORDER BY through_seq DESC LIMIT 1", (project_id,)).fetchone()
                previous_digest = previous[0] if previous else None
                payload = (previous_digest or "") + "\n" + "\n".join(canonical(row) for row in rows)
                checkpoint = {"id":uid(),"project_id":project_id,"from_seq":rows[0]["seq"],"through_seq":rows[-1]["seq"],
                              "entry_count":len(rows),"previous_digest":previous_digest,
                              "digest":hashlib.sha256(payload.encode()).hexdigest(),"created_at":now()}
                cx.execute("INSERT INTO audit_checkpoints VALUES(:id,:project_id,:from_seq,:through_seq,:entry_count,:previous_digest,:digest,:created_at)", checkpoint)
                cx.execute("UPDATE maintenance_control SET audit_prune_enabled=1 WHERE id=1")
                cx.execute("DELETE FROM audit_log WHERE project_id=? AND seq<=?", (project_id,rows[-1]["seq"]))
                cx.execute("UPDATE maintenance_control SET audit_prune_enabled=0 WHERE id=1")
        return {**plan,"terminal_memories_purged":len(terminal),"audit_entries_checkpointed":prune_count,"checkpoint":checkpoint}

    def maintenance_status(self, project_id: str) -> dict[str, Any]:
        policy = self.get_policy(project_id)
        counts = {"events":self.conn.execute("SELECT count(*) FROM events WHERE project_id=?",(project_id,)).fetchone()[0],
                  "memories":self.conn.execute("SELECT count(*) FROM memories WHERE project_id=?",(project_id,)).fetchone()[0],
                  "terminal_memories":self.conn.execute("SELECT count(*) FROM memories WHERE project_id=? AND status IN ('superseded','rejected','expired')",(project_id,)).fetchone()[0],
                  "audit_entries":self.conn.execute("SELECT count(*) FROM audit_log WHERE project_id=?",(project_id,)).fetchone()[0]}
        checkpoints = [dict(row) for row in self.conn.execute("SELECT * FROM audit_checkpoints WHERE project_id=? ORDER BY through_seq",(project_id,))]
        schedule = self._row("SELECT * FROM maintenance_runs WHERE project_id=?", (project_id,))
        return {"project_id":project_id,"policy":policy,"counts":counts,"audit_checkpoints":checkpoints,
                "schedule":schedule,"search":self.search_health(project_id)}

    def export_audit_chain(self, project_id: str) -> dict[str, Any]:
        """Return a deterministic bundle suitable for offline audit-chain verification."""
        if not self._row("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError("project not found")
        checkpoints = [dict(row) for row in self.conn.execute(
            "SELECT * FROM audit_checkpoints WHERE project_id=? ORDER BY through_seq,id", (project_id,))]
        entries = [dict(row) for row in self.conn.execute(
            "SELECT * FROM audit_log WHERE project_id=? ORDER BY seq", (project_id,))]
        return {"format":"context-memory-audit-chain","version":1,"project_id":project_id,
                "checkpoints":checkpoints,"audit_entries":entries,
                "head_digest":checkpoints[-1]["digest"] if checkpoints else None}

    @staticmethod
    def verify_audit_chain(bundle: dict[str, Any], expected_head_digest: str | None = None) -> dict[str, Any]:
        """Verify an exported chain without opening its source database.

        Checkpoint digests commit to compacted audit rows that are intentionally no longer
        present. Supplying a separately recorded head digest anchors the exported chain and
        detects replacement of the bundle as a whole.
        """
        errors: list[str] = []
        if bundle.get("format") != "context-memory-audit-chain" or bundle.get("version") != 1:
            errors.append("unsupported audit-chain format or version")
        project_id = bundle.get("project_id")
        checkpoints = bundle.get("checkpoints")
        entries = bundle.get("audit_entries")
        if not isinstance(project_id, str) or not project_id:
            errors.append("missing project_id")
        if not isinstance(checkpoints, list) or not isinstance(entries, list):
            return {"ok":False,"errors":errors + ["checkpoints and audit_entries must be arrays"]}
        previous_digest = None
        previous_through = None
        for index, checkpoint in enumerate(checkpoints):
            label = f"checkpoint[{index}]"
            if not isinstance(checkpoint, dict):
                errors.append(f"{label} must be an object"); continue
            digest = checkpoint.get("digest")
            if checkpoint.get("project_id") != project_id: errors.append(f"{label} project_id mismatch")
            if checkpoint.get("previous_digest") != previous_digest: errors.append(f"{label} previous_digest mismatch")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest): errors.append(f"{label} invalid digest")
            start, end, count = checkpoint.get("from_seq"), checkpoint.get("through_seq"), checkpoint.get("entry_count")
            if not all(isinstance(value, int) and not isinstance(value, bool) for value in (start,end,count)):
                errors.append(f"{label} range and entry_count must be integers")
            elif start > end or count < 1 or count > end - start + 1:
                errors.append(f"{label} invalid range or entry_count")
            elif previous_through is not None and start <= previous_through:
                errors.append(f"{label} overlaps or reorders the previous checkpoint")
            previous_digest, previous_through = digest, end
        head = previous_digest
        if bundle.get("head_digest") != head: errors.append("head_digest does not match the checkpoint chain")
        if expected_head_digest is not None and expected_head_digest != head: errors.append("expected head digest mismatch")
        previous_seq = previous_through
        for index, entry in enumerate(entries):
            label = f"audit_entries[{index}]"
            if not isinstance(entry, dict): errors.append(f"{label} must be an object"); continue
            if entry.get("project_id") != project_id: errors.append(f"{label} project_id mismatch")
            seq = entry.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool): errors.append(f"{label} invalid seq")
            elif previous_seq is not None and seq <= previous_seq: errors.append(f"{label} is not strictly ordered after prior audit data")
            previous_seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else previous_seq
        return {"ok":not errors,"project_id":project_id,"head_digest":head,
                "checkpoint_count":len(checkpoints),"audit_entry_count":len(entries),
                "anchored":expected_head_digest is not None,"errors":errors}

    def maintain_scheduled(self, project_id: str) -> dict[str, Any]:
        """Run maintenance once when its persisted interval is due; safe for repeated scheduler invocations."""
        policy = self.get_policy(project_id); interval = policy["maintenance_interval_seconds"]
        if not interval: return {"project_id":project_id,"scheduled":True,"ran":False,"reason":"disabled"}
        ts = now()
        with self.tx() as cx:
            state = dict(cx.execute("SELECT * FROM maintenance_runs WHERE project_id=?", (project_id,)).fetchone())
            baseline = state["last_completed_at"] or state["last_started_at"]
            if baseline and datetime.fromisoformat(baseline) + timedelta(seconds=interval) > datetime.fromisoformat(ts):
                return {"project_id":project_id,"scheduled":True,"ran":False,"reason":"not_due","next_due_at":
                        (datetime.fromisoformat(baseline) + timedelta(seconds=interval)).isoformat()}
            cx.execute("UPDATE maintenance_runs SET last_started_at=?,last_error=NULL WHERE project_id=?", (ts,project_id))
        try:
            result = self.maintain(project_id, True)
        except Exception as exc:
            self.conn.execute("UPDATE maintenance_runs SET last_error=? WHERE project_id=?", (str(exc),project_id))
            raise
        completed = now()
        self.conn.execute("UPDATE maintenance_runs SET last_completed_at=?,last_error=NULL WHERE project_id=?", (completed,project_id))
        return {**result,"scheduled":True,"ran":True,"completed_at":completed}

    def backup_to(self, output_path: str | Path, encryption_passphrase: str | None = None) -> dict[str, Any]:
        """Create one consistent SQLite snapshot using the Online Backup API, including committed WAL data."""
        destination = Path(output_path).expanduser().resolve()
        if destination == self.path: raise ValueError("backup output must differ from the live database")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(f".{destination.name}.{uid()}.tmp")
        target = sqlite3.connect(temporary)
        try:
            self.conn.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok": raise RuntimeError(f"backup integrity check failed: {integrity}")
        except Exception:
            target.close()
            temporary.unlink(missing_ok=True)
            raise
        else:
            target.close()
        os.chmod(temporary, 0o600)
        encryption = {"encrypted":False}
        if encryption_passphrase is not None:
            from .backup_crypto import encrypt_file
            plaintext = temporary
            encrypted = temporary.with_suffix(temporary.suffix + ".enc")
            try:
                encryption = encrypt_file(plaintext, encrypted, encryption_passphrase)
                os.chmod(encrypted, 0o600); temporary = encrypted
            except Exception:
                encrypted.unlink(missing_ok=True)
                raise
            finally:
                plaintext.unlink(missing_ok=True)
        os.replace(temporary, destination)
        digest = hashlib.sha256()
        with destination.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b""): digest.update(chunk)
        return {"ok":True,"source":str(self.path),"output":str(destination),"bytes":destination.stat().st_size,
                "sha256":digest.hexdigest(),"created_at":now(),"integrity":"ok",**encryption}

    def export_project(self, project_id: str) -> list[dict[str, Any]]:
        """Return a deterministic, portable snapshot without SQLite internals."""
        project = self._row("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise KeyError("project not found")
        records: list[dict[str, Any]] = [{"record_type": "project", "data": project}]
        queries = [
            ("scope", "SELECT * FROM scopes WHERE project_id=? ORDER BY created_at,id"),
            ("session", "SELECT * FROM sessions WHERE project_id=? ORDER BY started_at,id"),
            ("event", "SELECT * FROM events WHERE project_id=? ORDER BY event_seq"),
            ("memory", "SELECT * FROM memories WHERE project_id=? ORDER BY created_at,id"),
            ("memory_source", "SELECT ms.* FROM memory_sources ms JOIN memories m ON m.id=ms.memory_id WHERE m.project_id=? ORDER BY ms.created_at,ms.memory_id,ms.event_id"),
            ("investigation", "SELECT * FROM investigations WHERE project_id=? ORDER BY started_at,id"),
            ("source_analysis", "SELECT s.* FROM source_analyses s JOIN investigations i ON i.id=s.investigation_id WHERE i.project_id=? ORDER BY s.created_at,s.id"),
            ("investigation_claim", "SELECT c.* FROM investigation_claims c JOIN investigations i ON i.id=c.investigation_id WHERE i.project_id=? ORDER BY c.created_at,c.source_analysis_id,c.ordinal"),
            ("investigation_claim_link", "SELECT l.* FROM investigation_claim_links l JOIN investigation_claims c ON c.id=l.from_claim_id JOIN investigations i ON i.id=c.investigation_id WHERE i.project_id=? ORDER BY l.created_at,l.from_claim_id,l.to_claim_id"),
            ("wiki_page", "SELECT * FROM wiki_pages WHERE project_id=? ORDER BY created_at,id"),
            ("wiki_revision", "SELECT r.* FROM wiki_revisions r JOIN wiki_pages p ON p.id=r.page_id WHERE p.project_id=? ORDER BY r.created_at,r.id"),
            ("wiki_revision_citation", "SELECT c.* FROM wiki_revision_citations c JOIN wiki_revisions r ON r.id=c.revision_id JOIN wiki_pages p ON p.id=r.page_id WHERE p.project_id=? ORDER BY c.revision_id,c.section_name,c.ordinal,c.memory_id,c.event_id"),
            ("memory_usage", "SELECT u.* FROM memory_usage u JOIN memories m ON m.id=u.memory_id WHERE m.project_id=? ORDER BY u.memory_id"),
            ("review_conflict", "SELECT c.* FROM review_conflicts c JOIN memories m ON m.id=c.candidate_memory_id WHERE m.project_id=? ORDER BY c.created_at,c.candidate_memory_id,c.existing_memory_id"),
            ("edge", "SELECT * FROM edges WHERE project_id=? ORDER BY created_at,id"),
            ("search_alias", "SELECT * FROM search_aliases WHERE project_id=? ORDER BY term"),
            ("project_alias", "SELECT * FROM project_aliases WHERE project_id=? ORDER BY kind,normalized"),
            ("event_receipt", "SELECT * FROM event_receipts WHERE project_id=? ORDER BY consumer_id,scope_key,kinds_json"),
            ("policy", "SELECT * FROM project_policies WHERE project_id=?"),
            ("audit_checkpoint", "SELECT * FROM audit_checkpoints WHERE project_id=? ORDER BY through_seq"),
            ("audit", "SELECT * FROM audit_log WHERE project_id=? ORDER BY seq"),
        ]
        for record_type, sql in queries:
            records.extend({"record_type": record_type, "data": dict(row)} for row in self.conn.execute(sql, (project_id,)))
        return records

    def import_project(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Restore one exported project. Existing project IDs are never overwritten."""
        if not records or records[0].get("record_type") != "project":
            raise ValueError("export must begin with a project record")
        allowed = {"project", "scope", "session", "event", "memory", "memory_source", "investigation", "source_analysis", "investigation_claim", "investigation_claim_link", "wiki_page", "wiki_revision", "wiki_revision_citation", "memory_usage", "review_conflict", "edge", "search_alias", "project_alias", "event_receipt", "policy", "audit_checkpoint", "audit"}
        if any(record.get("record_type") not in allowed or not isinstance(record.get("data"), dict) for record in records):
            raise ValueError("invalid export record")
        project = records[0]["data"]
        if self._row("SELECT id FROM projects WHERE id=? OR slug=?", (project.get("id"), project.get("slug"))):
            raise ValueError("project id or slug already exists")
        columns = {
            "project": ("projects", ["id","slug","name","description","created_at"]),
            "scope": ("scopes", ["id","project_id","name","path","created_at"]),
            "session": ("sessions", ["id","project_id","scope_id","client","external_id","started_at","ended_at","metadata_json"]),
            "event": ("events", ["id","project_id","scope_id","session_id","kind","content","source_uri","metadata_json","content_hash","created_at","event_seq"]),
            "memory": ("memories", ["id","project_id","scope_id","type","status","title","content","confidence","importance","valid_from","valid_until","tags_json","created_at","updated_at","observed_at","last_confirmed_at","visibility"]),
            "memory_source": ("memory_sources", ["memory_id","event_id","note","created_at"]),
            "investigation": ("investigations", ["id","project_id","scope_id","question","reason","decision_to_inform","constraints_json","initiator","status","started_at","completed_at"]),
            "source_analysis": ("source_analyses", ["id","investigation_id","source_type","stable_source_id","canonical_uri","source_version","source_updated_at","retrieved_at","section_anchor","access_reason","analysis_method","content_fingerprint","identity_key","created_at"]),
            "investigation_claim": ("investigation_claims", ["id","investigation_id","source_analysis_id","claim_key","ordinal","role","event_id","memory_id","created_at","expected_outcome","outcome_effect"]),
            "investigation_claim_link": ("investigation_claim_links", ["from_claim_id","to_claim_id","relation","created_at"]),
            "wiki_page": ("wiki_pages", ["id","project_id","scope_id","topic","title","manual_notes","created_at","updated_at"]),
            "wiki_revision": ("wiki_revisions", ["id","page_id","revision_no","status","question","sections_json","generation_json","created_at","published_at","stale_reason"]),
            "wiki_revision_citation": ("wiki_revision_citations", ["revision_id","section_name","ordinal","memory_id","event_id"]),
            "memory_usage": ("memory_usage", ["memory_id","retrieved_count","used_count","helpful_count","incorrect_count","last_retrieved_at","last_used_at","updated_at"]),
            "review_conflict": ("review_conflicts", ["candidate_memory_id","existing_memory_id","similarity","reason","created_at"]),
            "edge": ("edges", ["id","project_id","from_memory_id","to_memory_id","relation","note","created_at"]),
            "search_alias": ("search_aliases", ["project_id","term","aliases_json","created_at","updated_at"]),
            "project_alias": ("project_aliases", ["project_id","kind","value","normalized","created_at","updated_at"]),
            "event_receipt": ("event_receipts", ["project_id","consumer_id","scope_key","kinds_json","acknowledged_cursor","delivered_cursor","created_at","updated_at"]),
            "audit_checkpoint": ("audit_checkpoints", ["id","project_id","from_seq","through_seq","entry_count","previous_digest","digest","created_at"]),
        }
        counts: dict[str, int] = {}
        imported_event_seq = 0
        with self.tx() as cx:
            for record in records:
                kind, data = record["record_type"], dict(record["data"])
                if kind == "event":
                    imported_event_seq += 1
                    data["event_seq"] = data.get("event_seq") or imported_event_seq
                if kind == "memory": data.setdefault("visibility", "project")
                if kind == "investigation_claim":
                    data.setdefault("expected_outcome", None)
                    data.setdefault("outcome_effect", None)
                if kind == "audit":
                    names = ["project_id","entity_type","entity_id","action","snapshot_json","created_at"]
                    cx.execute(f"INSERT INTO audit_log({','.join(names)}) VALUES({','.join('?' for _ in names)})", tuple(data[name] for name in names))
                elif kind == "policy":
                    defaults = {"checkpoint_soft_usage":.60,"checkpoint_hard_usage":.75,
                                "checkpoint_elapsed_seconds":1800,"checkpoint_event_count":25,
                                "checkpoint_max_age_seconds":3600,"checkpoint_cooldown_seconds":300,
                                "checkpoint_hysteresis":.05,"maintenance_interval_seconds":0,"message_ttl_seconds":0}
                    for name, value in defaults.items(): data.setdefault(name, value)
                    names = ["max_context_chars","max_context_items","audit_keep_entries","terminal_memory_days",
                             "checkpoint_soft_usage","checkpoint_hard_usage","checkpoint_elapsed_seconds",
                             "checkpoint_event_count","checkpoint_max_age_seconds","checkpoint_cooldown_seconds",
                             "checkpoint_hysteresis","maintenance_interval_seconds","message_ttl_seconds","updated_at","project_id"]
                    cx.execute("""UPDATE project_policies SET max_context_chars=?,max_context_items=?,audit_keep_entries=?,
                      terminal_memory_days=?,checkpoint_soft_usage=?,checkpoint_hard_usage=?,checkpoint_elapsed_seconds=?,
                      checkpoint_event_count=?,checkpoint_max_age_seconds=?,checkpoint_cooldown_seconds=?,checkpoint_hysteresis=?,maintenance_interval_seconds=?,message_ttl_seconds=?,
                      updated_at=? WHERE project_id=?""", tuple(data[name] for name in names))
                else:
                    table, names = columns[kind]
                    cx.execute(f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)})", tuple(data[name] for name in names))
                counts[kind] = counts.get(kind, 0) + 1
            cx.execute("UPDATE project_event_cursors SET next_seq=? WHERE project_id=?", (imported_event_seq + 1, project["id"]))
        return {"project_id": project["id"], "slug": project["slug"], "records": len(records), "counts": counts}

    def rebuild_fts(self, project_id: str | None = None) -> dict[str, Any]:
        if project_id and not self._row("SELECT id FROM projects WHERE id=?", (project_id,)):
            raise KeyError("project not found")
        condition = " WHERE project_id=?" if project_id else ""
        args = (project_id,) if project_id else ()
        with self.tx() as cx:
            if project_id:
                ids = [row[0] for row in cx.execute("SELECT id FROM memories WHERE project_id=?", args)]
                if ids: cx.execute("DELETE FROM memories_fts WHERE memory_id IN ("+",".join("?" for _ in ids)+")", ids)
            else:
                cx.execute("DELETE FROM memories_fts")
            rows = list(cx.execute("SELECT id,title,content,tags_json FROM memories"+condition, args))
            for row in rows:
                cx.execute("INSERT INTO memories_fts(memory_id,title,content,tags) VALUES(?,?,?,?)",
                           (row["id"],row["title"],row["content"]," ".join(json.loads(row["tags_json"]))))
            if self.embedding_provider:
                memories = list(cx.execute("SELECT * FROM memories"+condition, args))
                for memory in memories: self._index_embedding(cx, dict(memory))
        return {"ok": True, "project_id": project_id, "indexed_memories": len(rows),
                "embedding_provider":self._provider_name(),"embedded_memories":len(rows) if self.embedding_provider else 0}

    def audit(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM audit_log WHERE entity_type=? AND entity_id=? ORDER BY seq", (entity_type, entity_id))]
