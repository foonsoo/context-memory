"""Maintenance policy and audit persistence queries."""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable

from ..audit_serialization import build_audit_checkpoint, serialize_audit_chain


class MaintenanceRepository:
    """Own maintenance policy and audit-chain persistence."""

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

    def get_policy(self, project_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM project_policies WHERE project_id=?",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None

    def audit_checkpoints(self, project_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM audit_checkpoints WHERE project_id=? ORDER BY"
                " through_seq,id",
                (project_id,),
            )
        ]

    def audit_entries(self, project_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM audit_log WHERE project_id=? ORDER BY seq",
                (project_id,),
            )
        ]

    def export_audit_chain(self, project_id: str) -> dict[str, Any]:
        """Return a bundle for offline audit-chain verification."""
        row = self.connection.execute(
            "SELECT id FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if not row:
            raise KeyError("project not found")
        return serialize_audit_chain(
            project_id,
            self.audit_checkpoints(project_id),
            self.audit_entries(project_id),
        )

    def maintain(self, project_id: str, apply: bool = False) -> dict[str, Any]:
        """Bound state while preserving events and audit detail."""
        policy = self.store.get_policy(project_id)
        cutoff = (
            self.current_datetime()
            - timedelta(days=policy["terminal_memory_days"])
        ).isoformat()
        terminal = [
            dict(row)
            for row in self.connection.execute(
                """SELECT * FROM memories WHERE project_id=?
          AND status IN ('superseded','rejected','expired')
          AND updated_at<? ORDER BY updated_at,id""",
                (project_id, cutoff),
            )
        ]
        audit_total = self.connection.execute(
            "SELECT count(*) FROM audit_log WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
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
        with self.store.tx() as connection:
            for memory in terminal:
                sources = [
                    row[0]
                    for row in connection.execute(
                        "SELECT event_id FROM memory_sources WHERE"
                        " memory_id=? ORDER BY event_id",
                        (memory["id"],),
                    )
                ]
                self.store._audit(
                    connection,
                    project_id,
                    "memory",
                    memory["id"],
                    "purged_terminal",
                    {**memory, "source_event_ids": sources},
                )
                connection.execute(
                    "DELETE FROM memories WHERE id=?", (memory["id"],)
                )
            total = connection.execute(
                "SELECT count(*) FROM audit_log WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            prune_count = max(0, total - policy["audit_keep_entries"])
            if prune_count:
                rows = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM audit_log WHERE project_id=? ORDER BY"
                        " seq LIMIT ?",
                        (project_id, prune_count),
                    )
                ]
                previous = connection.execute(
                    "SELECT digest FROM audit_checkpoints WHERE project_id=?"
                    " ORDER BY through_seq DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
                previous_digest = previous[0] if previous else None
                checkpoint = build_audit_checkpoint(
                    project_id,
                    rows,
                    previous_digest,
                    checkpoint_id=self.uid(),
                    created_at=self.now(),
                )
                connection.execute(
                    "INSERT INTO audit_checkpoints"
                    " VALUES(:id,:project_id,:from_seq,:through_seq,"
                    ":entry_count,:previous_digest,:digest,:created_at)",
                    checkpoint,
                )
                connection.execute(
                    "UPDATE maintenance_control SET audit_prune_enabled=1"
                    " WHERE id=1"
                )
                connection.execute(
                    "DELETE FROM audit_log WHERE project_id=? AND seq<=?",
                    (project_id, rows[-1]["seq"]),
                )
                connection.execute(
                    "UPDATE maintenance_control SET audit_prune_enabled=0"
                    " WHERE id=1"
                )
        return {
            **plan,
            "terminal_memories_purged": len(terminal),
            "audit_entries_checkpointed": prune_count,
            "checkpoint": checkpoint,
        }

    def status(self, project_id: str) -> dict[str, Any]:
        policy = self.store.get_policy(project_id)
        counts = {
            "events": self._count("events", project_id),
            "memories": self._count("memories", project_id),
            "terminal_memories": self.connection.execute(
                "SELECT count(*) FROM memories WHERE project_id=? AND"
                " status IN ('superseded','rejected','expired')",
                (project_id,),
            ).fetchone()[0],
            "audit_entries": self._count("audit_log", project_id),
        }
        checkpoints = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM audit_checkpoints WHERE project_id=? ORDER BY"
                " through_seq",
                (project_id,),
            )
        ]
        schedule = self.store._row(
            "SELECT * FROM maintenance_runs WHERE project_id=?",
            (project_id,),
        )
        return {
            "project_id": project_id,
            "policy": policy,
            "counts": counts,
            "audit_checkpoints": checkpoints,
            "schedule": schedule,
            "search": self.store.search_health(project_id),
        }

    def _count(self, table: str, project_id: str) -> int:
        return self.connection.execute(
            f"SELECT count(*) FROM {table} WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]

    def maintain_scheduled(self, project_id: str) -> dict[str, Any]:
        """Run maintenance once when its persisted interval is due."""
        policy = self.store.get_policy(project_id)
        interval = policy["maintenance_interval_seconds"]
        if not interval:
            return {
                "project_id": project_id,
                "scheduled": True,
                "ran": False,
                "reason": "disabled",
            }
        timestamp = self.now()
        with self.store.tx() as connection:
            state = dict(
                connection.execute(
                    "SELECT * FROM maintenance_runs WHERE project_id=?",
                    (project_id,),
                ).fetchone()
            )
            baseline = state["last_completed_at"] or state["last_started_at"]
            if baseline and datetime.fromisoformat(baseline) + timedelta(
                seconds=interval
            ) > datetime.fromisoformat(timestamp):
                return {
                    "project_id": project_id,
                    "scheduled": True,
                    "ran": False,
                    "reason": "not_due",
                    "next_due_at": (
                        datetime.fromisoformat(baseline)
                        + timedelta(seconds=interval)
                    ).isoformat(),
                }
            connection.execute(
                "UPDATE maintenance_runs SET last_started_at=?,"
                "last_error=NULL WHERE project_id=?",
                (timestamp, project_id),
            )
        try:
            result = self.maintain(project_id, True)
        except Exception as exc:
            self.connection.execute(
                "UPDATE maintenance_runs SET last_error=? WHERE project_id=?",
                (str(exc), project_id),
            )
            raise
        completed = self.now()
        self.connection.execute(
            "UPDATE maintenance_runs SET last_completed_at=?,"
            "last_error=NULL WHERE project_id=?",
            (completed, project_id),
        )
        return {
            **result,
            "scheduled": True,
            "ran": True,
            "completed_at": completed,
        }

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
        current = self.store.get_policy(project_id)
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
        current["updated_at"] = self.now()
        with self.store.tx() as cx:
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
            self.store._audit(
                cx, project_id, "policy", project_id, "updated", current
            )
        return current

    def search_health(self, project_id: str) -> dict[str, Any]:
        if not self.store._row(
            "SELECT id FROM projects WHERE id=?", (project_id,)
        ):
            raise KeyError("project not found")
        memories = self.store.conn.execute(
            "SELECT count(*) FROM memories WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        indexed = self.store.conn.execute(
            "SELECT count(*) FROM memories_fts f JOIN memories m ON"
            " m.id=f.memory_id WHERE m.project_id=?",
            (project_id,),
        ).fetchone()[0]
        missing = self.store.conn.execute(
            "SELECT count(*) FROM memories m WHERE m.project_id=? AND NOT"
            " EXISTS(SELECT 1 FROM memories_fts f WHERE f.memory_id=m.id)",
            (project_id,),
        ).fetchone()[0]
        duplicate = self.store.conn.execute(
            """SELECT count(*) FROM (SELECT f.memory_id
          FROM memories_fts f JOIN memories m ON m.id=f.memory_id
          WHERE m.project_id=? GROUP BY f.memory_id HAVING count(*)<>1)""",
            (project_id,),
        ).fetchone()[0]
        orphan = self.store.conn.execute(
            "SELECT count(*) FROM memories_fts f LEFT JOIN memories m ON"
            " m.id=f.memory_id WHERE m.id IS NULL"
        ).fetchone()[0]
        embedding = {
            "enabled": bool(self.store.embedding_provider),
            "provider": self.store._provider_name(),
            "indexed_rows": 0,
            "missing": 0,
            "stale": 0,
        }
        if self.store.embedding_provider:
            embedding["indexed_rows"] = self.store.conn.execute(
                """SELECT count(*) FROM memory_embeddings e
              JOIN memories m ON m.id=e.memory_id
              WHERE m.project_id=? AND e.provider=? AND e.dimensions=?""",
                (
                    project_id,
                    self.store._provider_name(),
                    self.store.embedding_provider.dimensions,
                ),
            ).fetchone()[0]
            embedding["missing"] = memories - embedding["indexed_rows"]
            for row in self.store.conn.execute(
                """SELECT m.*,e.content_hash FROM memories m
              JOIN memory_embeddings e ON e.memory_id=m.id
              WHERE m.project_id=? AND e.provider=?""",
                (project_id, self.store._provider_name()),
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
                not self.store.embedding_provider
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
