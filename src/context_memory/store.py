from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

TYPES = {"fact", "decision", "preference", "constraint", "procedure", "summary", "task", "other"}
STATUSES = {"proposed", "active", "superseded", "disputed", "expired", "rejected"}
RELATIONS = {"supersedes", "disputes", "supports", "depends_on", "related_to"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class MemoryStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).expanduser().resolve()
        self._secure_directory()
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.migrate()

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
            self._audit(cx, item["id"], "project", item["id"], "created", item)
            self._save_idem(cx, "create_project", idempotency_key, request, item)
        return item

    def list_projects(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM projects ORDER BY slug")]

    def create_scope(self, project_id: str, name: str, path: str | None = None) -> dict[str, Any]:
        item = {"id": uid(), "project_id": project_id, "name": name, "path": path, "created_at": now()}
        with self.tx() as cx:
            cx.execute("INSERT INTO scopes VALUES(:id,:project_id,:name,:path,:created_at)", item)
            self._audit(cx, project_id, "scope", item["id"], "created", item)
        return item

    def resolve_project(self, cwd: str) -> dict[str, Any]:
        """Resolve one canonical workspace folder to one memory project."""
        path = str(Path(cwd).expanduser().resolve())
        row = self.conn.execute("""SELECT p.*, s.id AS scope_id FROM scopes s
          JOIN projects p ON p.id=s.project_id WHERE s.path=?""", (path,)).fetchone()
        if row:
            item = dict(row); scope_id = item.pop("scope_id")
            return {"project": item, "scope_id": scope_id, "created": False}
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

    def end_session(self, session_id: str, summary: str | None = None) -> dict[str, Any]:
        with self.tx() as cx:
            row = cx.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row: raise KeyError("session not found")
            ended = row["ended_at"] or now()
            cx.execute("UPDATE sessions SET ended_at=? WHERE id=?", (ended, session_id))
            result = dict(row); result["ended_at"] = ended
            self._audit(cx, row["project_id"], "session", session_id, "ended", {"summary": summary, **result})
        return result

    def record_event(self, project_id: str, kind: str, content: str, session_id: str | None = None,
                     scope_id: str | None = None, source_uri: str | None = None, metadata: dict | None = None,
                     idempotency_key: str | None = None) -> dict[str, Any]:
        request = locals().copy(); request.pop("self"); request.pop("idempotency_key")
        if hit := self._idem("record_event", idempotency_key, request): return hit
        if not content.strip(): raise ValueError("event content cannot be empty")
        item = {"id": uid(), "project_id": project_id, "scope_id": scope_id, "session_id": session_id, "kind": kind,
                "content": content, "source_uri": source_uri, "metadata_json": canonical(metadata or {}),
                "content_hash": hashlib.sha256(content.encode()).hexdigest(), "created_at": now()}
        with self.tx() as cx:
            cx.execute("INSERT INTO events VALUES(:id,:project_id,:scope_id,:session_id,:kind,:content,:source_uri,:metadata_json,:content_hash,:created_at)", item)
            self._audit(cx, project_id, "event", item["id"], "recorded", item)
            self._save_idem(cx, "record_event", idempotency_key, request, item)
        return item

    def upsert_memory(self, project_id: str, title: str, content: str, memory_type: str = "other", status: str = "proposed",
                      confidence: float = .5, importance: float = .5, scope_id: str | None = None, source_event_ids: list[str] | None = None,
                      valid_from: str | None = None, valid_until: str | None = None, tags: list[str] | None = None,
                      memory_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        request = locals().copy(); request.pop("self"); request.pop("idempotency_key")
        if hit := self._idem("upsert_memory", idempotency_key, request): return hit
        if memory_type not in TYPES or status not in STATUSES: raise ValueError("invalid memory type or status")
        if not (0 <= confidence <= 1 and 0 <= importance <= 1): raise ValueError("confidence and importance must be 0..1")
        ts, mid = now(), memory_id or uid()
        existing = self._row("SELECT * FROM memories WHERE id=?", (mid,))
        item = {"id": mid, "project_id": project_id, "scope_id": scope_id, "type": memory_type, "status": status, "title": title,
                "content": content, "confidence": confidence, "importance": importance, "valid_from": valid_from, "valid_until": valid_until,
                "tags_json": canonical(tags or []), "created_at": existing["created_at"] if existing else ts, "updated_at": ts}
        with self.tx() as cx:
            if existing:
                if existing["project_id"] != project_id: raise ValueError("memory belongs to another project")
                cx.execute("""UPDATE memories SET scope_id=:scope_id,type=:type,status=:status,title=:title,content=:content,confidence=:confidence,
                  importance=:importance,valid_from=:valid_from,valid_until=:valid_until,tags_json=:tags_json,updated_at=:updated_at WHERE id=:id""", item)
                cx.execute("DELETE FROM memories_fts WHERE memory_id=?", (mid,))
                action = "updated"
            else:
                cx.execute("INSERT INTO memories VALUES(:id,:project_id,:scope_id,:type,:status,:title,:content,:confidence,:importance,:valid_from,:valid_until,:tags_json,:created_at,:updated_at)", item)
                action = "created"
            cx.execute("INSERT INTO memories_fts(memory_id,title,content,tags) VALUES(?,?,?,?)", (mid, title, content, " ".join(tags or [])))
            for eid in source_event_ids or []:
                event = cx.execute("SELECT project_id FROM events WHERE id=?", (eid,)).fetchone()
                if not event or event["project_id"] != project_id: raise ValueError(f"invalid source event: {eid}")
                cx.execute("INSERT OR IGNORE INTO memory_sources VALUES(?,?,?,?)", (mid, eid, "", ts))
            self._audit(cx, project_id, "memory", mid, action, item)
            self._save_idem(cx, "upsert_memory", idempotency_key, request, item)
        return item

    def transition(self, memory_id: str, status: str, related_memory_id: str | None = None, note: str = "") -> dict[str, Any]:
        if status not in {"active", "superseded", "disputed", "expired", "rejected"}: raise ValueError("invalid transition status")
        with self.tx() as cx:
            row = cx.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row: raise KeyError("memory not found")
            ts = now(); cx.execute("UPDATE memories SET status=?,updated_at=? WHERE id=?", (status, ts, memory_id))
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

    def search(self, project_id: str, query: str, limit: int = 10, statuses: list[str] | None = None, scope_id: str | None = None) -> list[dict[str, Any]]:
        if not query.strip(): return []
        tokens = re.findall(r"[\w-]+", query.casefold(), flags=re.UNICODE)
        if not tokens: return []
        expanded=list(tokens)
        for token in tokens:
            row=self._row("SELECT aliases_json FROM search_aliases WHERE project_id=? AND term=?",(project_id,token))
            if row:
                for alias in json.loads(row["aliases_json"]): expanded.extend(re.findall(r"[\w-]+",alias,flags=re.UNICODE))
        tokens=list(dict.fromkeys(expanded))
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        allowed = statuses or ["active", "proposed", "disputed"]
        placeholders = ",".join("?" for _ in allowed)
        sql = f"""SELECT m.*, bm25(memories_fts, 0, 5, 1, .5) AS fts_rank
          FROM memories_fts JOIN memories m ON m.id=memories_fts.memory_id
          WHERE memories_fts MATCH ? AND m.project_id=? AND m.status IN ({placeholders})"""
        args: list[Any] = [match, project_id, *allowed]
        if scope_id: sql += " AND (m.scope_id=? OR m.scope_id IS NULL)"; args.append(scope_id)
        sql += " ORDER BY (bm25(memories_fts,0,5,1,.5) - m.importance - m.confidence*.25) ASC LIMIT ?"; args.append(max(1, min(limit, 100)))
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        for r in rows:
            r["sources"] = [dict(x) for x in self.conn.execute("SELECT e.id,e.kind,e.source_uri,e.created_at FROM memory_sources s JOIN events e ON e.id=s.event_id WHERE s.memory_id=?", (r["id"],))]
        return rows

    def get_context(self, project_id: str, query: str, char_budget: int = 6000, statuses: list[str] | None = None, scope_id: str | None = None) -> dict[str, Any]:
        budget = max(0, min(char_budget, 100_000)); selected, used = [], 0
        for m in self.search(project_id, query, 50, statuses or ["active", "disputed"], scope_id):
            block = f"[{m['status']}/{m['type']}] {m['title']}\n{m['content']}\nsource_events: {', '.join(s['id'] for s in m['sources']) or 'none'}"
            if used + len(block) + 2 > budget: continue
            selected.append({"memory_id": m["id"], "text": block, "confidence": m["confidence"], "importance": m["importance"]})
            used += len(block) + 2
        return {"query": query, "budget": budget, "used": used, "items": selected, "context": "\n\n".join(i["text"] for i in selected)}

    def get_source(self, event_id: str) -> dict[str, Any]:
        item = self._row("SELECT * FROM events WHERE id=?", (event_id,))
        if not item: raise KeyError("source event not found")
        return item

    def export_project(self, project_id: str) -> list[dict[str, Any]]:
        """Return a deterministic, portable snapshot without SQLite internals."""
        project = self._row("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise KeyError("project not found")
        records: list[dict[str, Any]] = [{"record_type": "project", "data": project}]
        queries = [
            ("scope", "SELECT * FROM scopes WHERE project_id=? ORDER BY created_at,id"),
            ("session", "SELECT * FROM sessions WHERE project_id=? ORDER BY started_at,id"),
            ("event", "SELECT * FROM events WHERE project_id=? ORDER BY created_at,id"),
            ("memory", "SELECT * FROM memories WHERE project_id=? ORDER BY created_at,id"),
            ("memory_source", "SELECT ms.* FROM memory_sources ms JOIN memories m ON m.id=ms.memory_id WHERE m.project_id=? ORDER BY ms.created_at,ms.memory_id,ms.event_id"),
            ("edge", "SELECT * FROM edges WHERE project_id=? ORDER BY created_at,id"),
            ("search_alias", "SELECT * FROM search_aliases WHERE project_id=? ORDER BY term"),
            ("audit", "SELECT * FROM audit_log WHERE project_id=? ORDER BY seq"),
        ]
        for record_type, sql in queries:
            records.extend({"record_type": record_type, "data": dict(row)} for row in self.conn.execute(sql, (project_id,)))
        return records

    def import_project(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Restore one exported project. Existing project IDs are never overwritten."""
        if not records or records[0].get("record_type") != "project":
            raise ValueError("export must begin with a project record")
        allowed = {"project", "scope", "session", "event", "memory", "memory_source", "edge", "search_alias", "audit"}
        if any(record.get("record_type") not in allowed or not isinstance(record.get("data"), dict) for record in records):
            raise ValueError("invalid export record")
        project = records[0]["data"]
        if self._row("SELECT id FROM projects WHERE id=? OR slug=?", (project.get("id"), project.get("slug"))):
            raise ValueError("project id or slug already exists")
        columns = {
            "project": ("projects", ["id","slug","name","description","created_at"]),
            "scope": ("scopes", ["id","project_id","name","path","created_at"]),
            "session": ("sessions", ["id","project_id","scope_id","client","external_id","started_at","ended_at","metadata_json"]),
            "event": ("events", ["id","project_id","scope_id","session_id","kind","content","source_uri","metadata_json","content_hash","created_at"]),
            "memory": ("memories", ["id","project_id","scope_id","type","status","title","content","confidence","importance","valid_from","valid_until","tags_json","created_at","updated_at"]),
            "memory_source": ("memory_sources", ["memory_id","event_id","note","created_at"]),
            "edge": ("edges", ["id","project_id","from_memory_id","to_memory_id","relation","note","created_at"]),
            "search_alias": ("search_aliases", ["project_id","term","aliases_json","created_at","updated_at"]),
        }
        counts: dict[str, int] = {}
        with self.tx() as cx:
            for record in records:
                kind, data = record["record_type"], record["data"]
                if kind == "audit":
                    names = ["project_id","entity_type","entity_id","action","snapshot_json","created_at"]
                    cx.execute(f"INSERT INTO audit_log({','.join(names)}) VALUES({','.join('?' for _ in names)})", tuple(data[name] for name in names))
                else:
                    table, names = columns[kind]
                    cx.execute(f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)})", tuple(data[name] for name in names))
                    if kind == "memory":
                        tags = " ".join(json.loads(data["tags_json"]))
                        cx.execute("INSERT INTO memories_fts(memory_id,title,content,tags) VALUES(?,?,?,?)", (data["id"],data["title"],data["content"],tags))
                counts[kind] = counts.get(kind, 0) + 1
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
        return {"ok": True, "project_id": project_id, "indexed_memories": len(rows)}

    def audit(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM audit_log WHERE entity_type=? AND entity_id=? ORDER BY seq", (entity_type, entity_id))]
