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

from .embeddings import EmbeddingProvider, LocalHashEmbedding

TYPES = {"fact", "decision", "preference", "constraint", "procedure", "summary", "task", "other"}
STATUSES = {"proposed", "active", "superseded", "disputed", "expired", "rejected"}
RELATIONS = {"supersedes", "disputes", "supports", "depends_on", "related_to"}
CHECKPOINT_MODES = {"interim", "final"}
CHECKPOINT_REASONS = {"context_budget", "elapsed", "material_change", "completed", "manual"}
DISCOVERY_MIN_CONFIDENCE = .45
DISCOVERY_AUTO_SELECT_CONFIDENCE = .60
DISCOVERY_MIN_MARGIN = .12


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
        mode = os.environ.get("CONTEXT_MEMORY_EMBEDDINGS", "").strip().casefold()
        self.embedding_provider = embedding_provider or (LocalHashEmbedding() if mode in {"local", "local-hash", "hash"} else None)
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
        kinds = {"fact", "decision", "preference", "constraint", "procedure", "task", "summary"}
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
        rows = []
        for row in self.conn.execute("SELECT * FROM memories WHERE project_id=? AND status='proposed' ORDER BY created_at,id", (project_id,)):
            item = dict(row)
            item["conflicts"] = [dict(x) for x in self.conn.execute("SELECT * FROM review_conflicts WHERE candidate_memory_id=? ORDER BY similarity DESC", (item["id"],))]
            item["sources"] = [dict(x) for x in self.conn.execute("SELECT e.id,e.kind,e.source_uri,e.created_at FROM memory_sources s JOIN events e ON e.id=s.event_id WHERE s.memory_id=?", (item["id"],))]
            rows.append(item)
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
            return hit
        if not content.strip(): raise ValueError("event content cannot be empty")
        with self.tx() as cx:
            cursor = cx.execute("UPDATE project_event_cursors SET next_seq=next_seq+1 WHERE project_id=? RETURNING next_seq-1", (project_id,)).fetchone()
            if not cursor: raise KeyError("project not found")
            item = {"id": uid(), "project_id": project_id, "scope_id": scope_id, "session_id": session_id, "kind": kind,
                    "content": content, "source_uri": source_uri, "metadata_json": canonical(metadata or {}),
                    "content_hash": hashlib.sha256(content.encode()).hexdigest(), "created_at": now(), "event_seq": cursor[0]}
            cx.execute("""INSERT INTO events(id,project_id,scope_id,session_id,kind,content,source_uri,metadata_json,content_hash,created_at,event_seq)
              VALUES(:id,:project_id,:scope_id,:session_id,:kind,:content,:source_uri,:metadata_json,:content_hash,:created_at,:event_seq)""", item)
            self._audit(cx, project_id, "event", item["id"], "recorded", item)
            self._save_idem(cx, "record_event", idempotency_key, request, item)
        return item

    def create_checkpoint(self, project_id: str, mode: str, reason: str, goal: str,
                          idempotency_key: str, session_id: str | None = None,
                          scope_id: str | None = None, completed: list[str] | None = None,
                          next_step: str | None = None, blockers: list[str] | None = None,
                          source_event_cursor: int | None = None,
                          context_usage: float | None = None,
                          repository_path: str | None = None,
                          test_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Record one explicit, client-neutral recovery checkpoint.

        Lifecycle automation and final-session closure are deliberately layered on
        top of this primitive; this operation only durably records caller-supplied
        recovery state and never mutates Git or inferred memories.
        """
        request = {"project_id":project_id, "mode":mode, "reason":reason, "goal":goal,
                   "session_id":session_id, "scope_id":scope_id, "completed":completed,
                   "next_step":next_step, "blockers":blockers,
                   "source_event_cursor":source_event_cursor, "context_usage":context_usage,
                   "repository_path":repository_path, "test_results":test_results}
        if hit := self._idem("create_checkpoint", idempotency_key, request): return hit
        if mode not in CHECKPOINT_MODES: raise ValueError("mode must be interim or final")
        if reason not in CHECKPOINT_REASONS:
            raise ValueError("reason must be context_budget, elapsed, material_change, completed, or manual")
        if not goal.strip(): raise ValueError("goal cannot be empty")
        if not idempotency_key.strip(): raise ValueError("idempotency_key cannot be empty")
        completed = completed or []; blockers = blockers or []
        if any(not item.strip() for item in completed): raise ValueError("completed must contain non-empty values")
        if any(not item.strip() for item in blockers): raise ValueError("blockers must contain non-empty values")
        if next_step is not None and not next_step.strip(): raise ValueError("next_step cannot be empty")
        if context_usage is not None and not 0 <= context_usage <= 1:
            raise ValueError("context_usage must be between 0 and 1")
        tests = self._normalize_test_results(test_results or [])
        repository = self._repository_facts(repository_path) if repository_path else None
        project = self._row("SELECT id FROM projects WHERE id=?", (project_id,))
        if not project: raise KeyError("project not found")
        if session_id:
            session = self._row("SELECT project_id,scope_id FROM sessions WHERE id=?", (session_id,))
            if not session: raise KeyError("session not found")
            if session["project_id"] != project_id: raise ValueError("session belongs to a different project")
            if scope_id is None: scope_id = session["scope_id"]
        if scope_id:
            scope = self._row("SELECT project_id FROM scopes WHERE id=?", (scope_id,))
            if not scope: raise KeyError("scope not found")
            if scope["project_id"] != project_id: raise ValueError("scope belongs to a different project")
        cursor = self._row("SELECT next_seq-1 AS value FROM project_event_cursors WHERE project_id=?", (project_id,))["value"]
        if source_event_cursor is None: source_event_cursor = cursor
        if source_event_cursor < 0 or source_event_cursor > cursor:
            raise ValueError("source_event_cursor must reference an existing project event cursor")
        payload = {"schema_version": 2, "mode": mode, "reason": reason, "goal": goal.strip(),
                   "completed": [item.strip() for item in completed],
                   "next_step": next_step.strip() if next_step else None,
                   "blockers": [item.strip() for item in blockers],
                   "source_event_cursor": source_event_cursor, "context_usage": context_usage,
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
            result = {"checkpoint_id":event["id"], "event_seq":event["event_seq"],
                      "created_at":created_at, **payload}
            self._save_idem(cx, "create_checkpoint", idempotency_key, request, result)
        return result

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
        for row in rows: row["metadata"] = json.loads(row.pop("metadata_json"))
        next_cursor = rows[-1]["event_seq"] if has_more else snapshot
        return {"project_id":project_id,"cursor":cursor,"snapshot_cursor":snapshot,"next_cursor":next_cursor,
                "has_more":has_more,"events":rows}

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
        expanded=list(query_tokens)
        for token in query_tokens:
            row=self._row("SELECT aliases_json FROM search_aliases WHERE project_id=? AND term=?",(project_id,token))
            if row:
                for alias in json.loads(row["aliases_json"]): expanded.extend(re.findall(r"[\w-]+",alias,flags=re.UNICODE))
        tokens=list(dict.fromkeys(expanded))
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        allowed = statuses or ["active", "proposed", "disputed"]
        placeholders = ",".join("?" for _ in allowed)
        timestamp = now()
        # Discovery is deliberately whole-database. Project identity hints are a
        # later prior, not a candidate-generation boundary: filtering here can
        # make the actually relevant project impossible to retrieve.
        boundary = "1=1" if discover_projects else "(m.project_id=? OR m.visibility='global')"
        boundary_args: list[Any] = [] if discover_projects else [project_id]
        sql = f"""SELECT m.*, bm25(memories_fts, 0, 5, 1, .5) AS fts_rank
          FROM memories_fts JOIN memories m ON m.id=memories_fts.memory_id
          WHERE memories_fts MATCH ? AND {boundary} AND m.status IN ({placeholders})
          AND (m.valid_from IS NULL OR m.valid_from<=?) AND (m.valid_until IS NULL OR m.valid_until>?)"""
        args: list[Any] = [match, *boundary_args, *allowed, timestamp, timestamp]
        if scope_id and not discover_projects: sql += " AND (m.scope_id=? OR m.scope_id IS NULL)"; args.append(scope_id)
        candidate_limit = max(20, min(max(1, limit) * 4, 200))
        sql += " ORDER BY bm25(memories_fts,0,5,1,.5) ASC LIMIT ?"; args.append(candidate_limit)
        lexical = [dict(r) for r in self.conn.execute(sql, args)]
        candidates = {row["id"]: row for row in lexical}
        components: dict[str, dict[str, float]] = {
            row["id"]: {"lexical_rrf": 1.0 / (60 + rank), "semantic_rrf": 0.0}
            for rank, row in enumerate(lexical, 1)
        }
        semantic_scores: dict[str, float] = {}
        if self.embedding_provider:
            query_vector = self.embedding_provider.embed([query])[0]
            sem_boundary = boundary
            sem_sql = f"""SELECT m.*, e.vector_json FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id
              WHERE {sem_boundary} AND m.status IN ({placeholders}) AND e.provider=? AND e.dimensions=?
              AND (m.valid_from IS NULL OR m.valid_from<=?) AND (m.valid_until IS NULL OR m.valid_until>?)"""
            sem_args: list[Any] = [*boundary_args, *allowed, self._provider_name(), self.embedding_provider.dimensions, timestamp, timestamp]
            if scope_id and not discover_projects: sem_sql += " AND (m.scope_id=? OR m.scope_id IS NULL)"; sem_args.append(scope_id)
            semantic = []
            for row in self.conn.execute(sem_sql, sem_args):
                item = dict(row); vector = json.loads(item.pop("vector_json"))
                similarity = sum(a * b for a, b in zip(query_vector, vector))
                if similarity > 0.05: semantic.append((similarity, item))
            semantic.sort(key=lambda value: (-value[0], value[1]["id"]))
            for rank, (similarity, row) in enumerate(semantic[:candidate_limit], 1):
                candidates.setdefault(row["id"], row)
                component = components.setdefault(row["id"], {"lexical_rrf": 0.0, "semantic_rrf": 0.0})
                component["semantic_rrf"] = 1.0 / (60 + rank)
                semantic_scores[row["id"]] = similarity
        usage = {row["memory_id"]: dict(row) for row in self.conn.execute("SELECT * FROM memory_usage")}
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
                              "query_coverage":query_coverage,
                              "semantic_similarity": semantic_scores.get(r["id"]), "embedding_provider": self._provider_name()}
            r["usage"] = usage.get(r["id"], {"retrieved_count":0,"used_count":0,"helpful_count":0,"incorrect_count":0})
            r["sources"] = [dict(x) for x in self.conn.execute("SELECT e.id,e.kind,e.source_uri,e.created_at FROM memory_sources s JOIN events e ON e.id=s.event_id WHERE s.memory_id=?", (r["id"],))]
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
            relevance_scores = sorted((m["retrieval"]["score"] for m in matches), reverse=True)
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

    def get_context(self, project_id: str, query: str, char_budget: int = 6000, statuses: list[str] | None = None,
                    scope_id: str | None = None, event_cursor: int | None = None, event_kinds: list[str] | None = None,
                    event_limit: int = 20, event_char_budget: int = 2000, discover_projects: bool = True) -> dict[str, Any]:
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
        local_matches = [m for m in candidates if m["project_id"] == project_id]
        discovery_used = bool(discover_projects and not local_matches)
        discovery_candidates: list[dict[str, Any]] = []
        if discovery_used:
            discovery_candidates = self.search(project_id, query, policy["max_context_items"] * 3, statuses or ["active", "disputed"], None, True)
            seen = {m["id"] for m in candidates}
            candidates.extend(m for m in discovery_candidates if m["id"] not in seen)
        project_candidates = self._aggregate_project_candidates(discovery_candidates, project_id)
        selected_project_id, selection_reason, discovery_confidence = self._select_project_candidate(project_candidates)
        discovery_ambiguous = selection_reason == "ambiguous_candidates"
        if discovery_used:
            candidates = [m for m in candidates if m["project_id"] == project_id or m["visibility"] == "global"
                          or m["project_id"] == selected_project_id]
        for m in candidates:
            block = f"[{m['status']}/{m['type']}] {m['title']}\n{m['content']}\nsource_events: {', '.join(s['id'] for s in m['sources']) or 'none'}"
            comparable = f"{m['title']} {m['content']}"
            if any(self._text_similarity(comparable, previous) >= .8 for previous in selected_texts): continue
            if used + len(block) + 2 > memory_budget: continue
            selected.append({"memory_id": m["id"], "project_id":m["project_id"], "visibility":m["visibility"],
                             "text": block, "confidence": m["confidence"], "importance": m["importance"]})
            selected_texts.append(comparable)
            used += len(block) + 2
            if len(selected) >= policy["max_context_items"]: break
        return {"query": query, "requested_budget": requested, "budget": budget, "budget_capped": requested > budget,
                "max_items": policy["max_context_items"], "memory_budget":memory_budget,"event_budget":reserved,
                "used": used + event_used, "memory_used":used,"event_used":event_used,
                "items": selected, "context": "\n\n".join(i["text"] for i in selected),"recent_events":recent_events,
                "project_discovery":{"enabled":discover_projects,"used":discovery_used,"ambiguous":discovery_ambiguous,
                                     "project_ids":list(dict.fromkeys(i["project_id"] for i in selected if i["project_id"] != project_id)),
                                     "selected_project_id":selected_project_id,"confidence":discovery_confidence,
                                     "selection_reason":selection_reason,"candidates":project_candidates},
                "event_cursor":event_cursor,"next_event_cursor":event_result["next_cursor"] if event_result else None,
                "event_snapshot_cursor":event_result["snapshot_cursor"] if event_result else None,
                "has_more_events":event_result["has_more"] if event_result else False}

    def get_policy(self, project_id: str) -> dict[str, Any]:
        item = self._row("SELECT * FROM project_policies WHERE project_id=?", (project_id,))
        if not item: raise KeyError("project not found")
        return item

    def set_policy(self, project_id: str, max_context_chars: int | None = None, max_context_items: int | None = None,
                   audit_keep_entries: int | None = None, terminal_memory_days: int | None = None) -> dict[str, Any]:
        current = self.get_policy(project_id)
        values = {"max_context_chars":max_context_chars,"max_context_items":max_context_items,
                  "audit_keep_entries":audit_keep_entries,"terminal_memory_days":terminal_memory_days}
        limits = {"max_context_chars":(1000,20000),"max_context_items":(1,50),"audit_keep_entries":(100,100000),"terminal_memory_days":(1,3650)}
        for key, value in values.items():
            if value is not None:
                low, high = limits[key]
                if not low <= value <= high: raise ValueError(f"{key} must be {low}..{high}")
                current[key] = value
        current["updated_at"] = now()
        with self.tx() as cx:
            cx.execute("""UPDATE project_policies SET max_context_chars=:max_context_chars,max_context_items=:max_context_items,
              audit_keep_entries=:audit_keep_entries,terminal_memory_days=:terminal_memory_days,updated_at=:updated_at WHERE project_id=:project_id""", current)
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
        return {"project_id":project_id,"policy":policy,"counts":counts,"audit_checkpoints":checkpoints,"search":self.search_health(project_id)}

    def backup_to(self, output_path: str | Path) -> dict[str, Any]:
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
        os.replace(temporary, destination)
        digest = hashlib.sha256()
        with destination.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b""): digest.update(chunk)
        return {"ok":True,"source":str(self.path),"output":str(destination),"bytes":destination.stat().st_size,
                "sha256":digest.hexdigest(),"created_at":now(),"integrity":"ok"}

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
            ("memory_usage", "SELECT u.* FROM memory_usage u JOIN memories m ON m.id=u.memory_id WHERE m.project_id=? ORDER BY u.memory_id"),
            ("review_conflict", "SELECT c.* FROM review_conflicts c JOIN memories m ON m.id=c.candidate_memory_id WHERE m.project_id=? ORDER BY c.created_at,c.candidate_memory_id,c.existing_memory_id"),
            ("edge", "SELECT * FROM edges WHERE project_id=? ORDER BY created_at,id"),
            ("search_alias", "SELECT * FROM search_aliases WHERE project_id=? ORDER BY term"),
            ("project_alias", "SELECT * FROM project_aliases WHERE project_id=? ORDER BY kind,normalized"),
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
        allowed = {"project", "scope", "session", "event", "memory", "memory_source", "memory_usage", "review_conflict", "edge", "search_alias", "project_alias", "policy", "audit_checkpoint", "audit"}
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
            "memory_usage": ("memory_usage", ["memory_id","retrieved_count","used_count","helpful_count","incorrect_count","last_retrieved_at","last_used_at","updated_at"]),
            "review_conflict": ("review_conflicts", ["candidate_memory_id","existing_memory_id","similarity","reason","created_at"]),
            "edge": ("edges", ["id","project_id","from_memory_id","to_memory_id","relation","note","created_at"]),
            "search_alias": ("search_aliases", ["project_id","term","aliases_json","created_at","updated_at"]),
            "project_alias": ("project_aliases", ["project_id","kind","value","normalized","created_at","updated_at"]),
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
                if kind == "audit":
                    names = ["project_id","entity_type","entity_id","action","snapshot_json","created_at"]
                    cx.execute(f"INSERT INTO audit_log({','.join(names)}) VALUES({','.join('?' for _ in names)})", tuple(data[name] for name in names))
                elif kind == "policy":
                    names = ["max_context_chars","max_context_items","audit_keep_entries","terminal_memory_days","updated_at","project_id"]
                    cx.execute("""UPDATE project_policies SET max_context_chars=?,max_context_items=?,audit_keep_entries=?,
                      terminal_memory_days=?,updated_at=? WHERE project_id=?""", tuple(data[name] for name in names))
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
