"""Memory lifecycle and retrieval persistence orchestration."""

import hashlib
import json
import sqlite3
from typing import Any, Callable

from ..contracts import MEMORY_TYPES
from ..serialization import canonical

STATUSES = {
    "proposed",
    "active",
    "superseded",
    "disputed",
    "expired",
    "rejected",
}
TYPES = MEMORY_TYPES


class MemoryRepository:
    """Own durable memory operations behind the store facade."""

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

    def get(self, memory_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_proposed(self, memory_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM memories WHERE id=? AND status='proposed'",
            (memory_id,),
        ).fetchone()
        return dict(row) if row else None

    def provider_name(self) -> str | None:
        if not self.store.embedding_provider:
            return None
        return str(
            getattr(
                self.store.embedding_provider,
                "name",
                self.store.embedding_provider.__class__.__name__,
            )
        )

    def index_embedding(
        self, connection: sqlite3.Connection, memory: dict[str, Any]
    ) -> None:
        provider_adapter = self.store.embedding_provider
        if not provider_adapter:
            return
        tags = " ".join(json.loads(memory["tags_json"]))
        text = f"{memory['title']}\n{memory['content']}\n{tags}"
        digest = hashlib.sha256(text.encode()).hexdigest()
        provider = self.provider_name()
        existing = connection.execute(
            "SELECT provider,content_hash FROM memory_embeddings WHERE"
            " memory_id=?",
            (memory["id"],),
        ).fetchone()
        if (
            existing
            and existing["provider"] == provider
            and existing["content_hash"] == digest
        ):
            return
        vector = provider_adapter.embed([text])[0]
        connection.execute(
            """INSERT INTO memory_embeddings(memory_id,provider,dimensions,
          content_hash,vector_json,updated_at) VALUES(?,?,?,?,?,?)
          ON CONFLICT(memory_id) DO UPDATE SET provider=excluded.provider,
          dimensions=excluded.dimensions,content_hash=excluded.content_hash,
          vector_json=excluded.vector_json,updated_at=excluded.updated_at""",
            (
                memory["id"],
                provider,
                provider_adapter.dimensions,
                digest,
                canonical(vector),
                self.now(),
            ),
        )

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
        request = locals().copy()
        request.pop("self")
        request.pop("idempotency_key")
        if hit := self.store._idem("upsert_memory", idempotency_key, request):
            return hit
        ts, mid = self.now(), memory_id or self.uid()
        existing = self.get(mid)
        if memory_type not in TYPES or status not in STATUSES:
            raise ValueError("invalid memory type or status")
        resolved_visibility = visibility or (
            existing["visibility"] if existing else "project"
        )
        if resolved_visibility not in {"project", "global"}:
            raise ValueError("visibility must be project or global")
        if resolved_visibility == "global" and scope_id is not None:
            raise ValueError("global memories cannot be path-scoped")
        if not (0 <= confidence <= 1 and 0 <= importance <= 1):
            raise ValueError("confidence and importance must be 0..1")
        item = {
            "id": mid,
            "project_id": project_id,
            "scope_id": scope_id,
            "type": memory_type,
            "status": status,
            "title": title,
            "content": content,
            "confidence": confidence,
            "importance": importance,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "tags_json": canonical(tags or []),
            "created_at": existing["created_at"] if existing else ts,
            "updated_at": ts,
            "observed_at": (
                observed_at or (existing["observed_at"] if existing else ts)
            ),
            "visibility": resolved_visibility,
            "last_confirmed_at": (
                last_confirmed_at
                or (
                    existing["last_confirmed_at"]
                    if existing
                    else (ts if status == "active" else None)
                )
            ),
        }
        with self.store.tx() as connection:
            if existing:
                if existing["project_id"] != project_id:
                    raise ValueError("memory belongs to another project")
                connection.execute(
                    """UPDATE memories SET scope_id=:scope_id,type=:type,
                  status=:status,title=:title,content=:content,
                  confidence=:confidence,importance=:importance,
                  valid_from=:valid_from,valid_until=:valid_until,
                  tags_json=:tags_json,updated_at=:updated_at,
                  observed_at=:observed_at,
                  last_confirmed_at=:last_confirmed_at,
                  visibility=:visibility WHERE id=:id""",
                    item,
                )
                action = "updated"
                if any(
                    existing[name] != item[name]
                    for name in (
                        "title",
                        "content",
                        "type",
                        "status",
                        "valid_from",
                        "valid_until",
                        "tags_json",
                    )
                ):
                    self.store._stale_wiki_revisions_for_memory(
                        connection, mid, "cited memory materially updated"
                    )
            else:
                connection.execute(
                    """INSERT INTO memories(id,project_id,scope_id,type,
                  status,title,content,confidence,importance,valid_from,
                  valid_until,tags_json,created_at,updated_at,observed_at,
                  last_confirmed_at,visibility) VALUES(:id,:project_id,
                  :scope_id,:type,:status,:title,:content,:confidence,
                  :importance,:valid_from,:valid_until,:tags_json,
                  :created_at,:updated_at,:observed_at,:last_confirmed_at,
                  :visibility)""",
                    item,
                )
                action = "created"
            for event_id in source_event_ids or []:
                event = connection.execute(
                    "SELECT project_id FROM events WHERE id=?", (event_id,)
                ).fetchone()
                if not event or event["project_id"] != project_id:
                    raise ValueError(f"invalid source event: {event_id}")
                connection.execute(
                    "INSERT OR IGNORE INTO memory_sources VALUES(?,?,?,?)",
                    (mid, event_id, "", ts),
                )
            self.index_embedding(connection, item)
            self.store._audit(
                connection, project_id, "memory", mid, action, item
            )
            self.store._save_idem(
                connection,
                "upsert_memory",
                idempotency_key,
                request,
                item,
            )
        return item

    def transition(
        self,
        memory_id: str,
        status: str,
        related_memory_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        if status not in {
            "active",
            "superseded",
            "disputed",
            "expired",
            "rejected",
        }:
            raise ValueError("invalid transition status")
        with self.store.tx() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
            if not row:
                raise KeyError("memory not found")
            ts = self.now()
            if status == "active":
                connection.execute(
                    "UPDATE memories SET"
                    " status=?,updated_at=?,last_confirmed_at=? WHERE id=?",
                    (status, ts, ts, memory_id),
                )
            else:
                connection.execute(
                    "UPDATE memories SET status=?,updated_at=? WHERE id=?",
                    (status, ts, memory_id),
                )
            relation = {
                "superseded": "supersedes",
                "disputed": "disputes",
            }.get(status)
            if relation and related_memory_id:
                other = connection.execute(
                    "SELECT project_id FROM memories WHERE id=?",
                    (related_memory_id,),
                ).fetchone()
                if not other or other["project_id"] != row["project_id"]:
                    raise ValueError("related memory must be in same project")
                connection.execute(
                    "INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?,?,?)",
                    (
                        self.uid(),
                        row["project_id"],
                        related_memory_id,
                        memory_id,
                        relation,
                        note,
                        ts,
                    ),
                )
            result = dict(row)
            result["status"] = status
            result["updated_at"] = ts
            if status in {
                "superseded",
                "disputed",
                "expired",
                "rejected",
            }:
                result["stale_wiki_revision_ids"] = (
                    self.store._stale_wiki_revisions_for_memory(
                        connection,
                        memory_id,
                        f"cited memory became {status}",
                    )
                )
            self.store._audit(
                connection,
                row["project_id"],
                "memory",
                memory_id,
                f"status:{status}",
                {"note": note, **result},
            )
        return result
