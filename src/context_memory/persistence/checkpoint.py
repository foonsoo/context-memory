"""Checkpoint state observation queries."""

import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime
from typing import Any, Callable

from ..checkpoint_policy import evaluate_checkpoint_policy
from ..serialization import canonical

CHECKPOINT_MODES = {"interim", "final"}
CHECKPOINT_REASONS = {
    "context_budget",
    "elapsed",
    "material_change",
    "completed",
    "manual",
}


class CheckpointRepository:
    """Observe durable state used by checkpoint policy evaluation."""

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

    def session_start(self, session_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT project_id,started_at FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def recovery_hash(
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
            for row in self.connection.execute(
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

    def latest(self, project_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM events WHERE project_id=? AND kind='checkpoint'"
            " ORDER BY event_seq DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None

    def event_cursor(self, project_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT next_seq-1 AS value FROM project_event_cursors WHERE"
            " project_id=?",
            (project_id,),
        ).fetchone()
        return row["value"] if row else None

    def durable_events_after(self, project_id: str, event_seq: int) -> int:
        return self.connection.execute(
            "SELECT count(*) FROM events WHERE project_id=? AND event_seq>?"
            " AND kind<>'checkpoint'",
            (project_id, event_seq),
        ).fetchone()[0]

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
        """Record one explicit, client-neutral recovery checkpoint.

        Interim checkpoints record recovery state. Final checkpoints
        atomically publish an evidence-backed handoff, replace its
        predecessor, and end the
        referenced session. Neither mode mutates Git.
        """
        request = {
            "project_id": project_id,
            "mode": mode,
            "reason": reason,
            "goal": goal,
            "session_id": session_id,
            "scope_id": scope_id,
            "completed": completed,
            "next_step": next_step,
            "blockers": blockers,
            "source_event_cursor": source_event_cursor,
            "context_usage": context_usage,
            "repository_path": repository_path,
            "test_results": test_results,
            "verified_event_ids": verified_event_ids,
            "handoff_title": handoff_title,
            "handoff_content": handoff_content,
            "previous_handoff_memory_id": previous_handoff_memory_id,
            "commit": commit,
        }
        if hit := self.store._idem(
            "create_checkpoint", idempotency_key, request
        ):
            return hit
        if mode not in CHECKPOINT_MODES:
            raise ValueError("mode must be interim or final")
        if reason not in CHECKPOINT_REASONS:
            raise ValueError(
                "reason must be context_budget, elapsed, material_change,"
                " completed, or manual"
            )
        if mode == "interim" and reason == "completed":
            raise ValueError("interim checkpoints cannot claim completed work")
        if not goal.strip():
            raise ValueError("goal cannot be empty")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key cannot be empty")
        completed = completed or []
        blockers = blockers or []
        if any(not item.strip() for item in completed):
            raise ValueError("completed must contain non-empty values")
        if any(not item.strip() for item in blockers):
            raise ValueError("blockers must contain non-empty values")
        if next_step is not None and not next_step.strip():
            raise ValueError("next_step cannot be empty")
        if context_usage is not None and not 0 <= context_usage <= 1:
            raise ValueError("context_usage must be between 0 and 1")
        tests = self.store._normalize_test_results(test_results or [])
        verified_event_ids = list(dict.fromkeys(verified_event_ids or []))
        repository = (
            self.store._repository_facts(repository_path)
            if repository_path
            else None
        )
        project = self.store._row(
            "SELECT id FROM projects WHERE id=?", (project_id,)
        )
        if not project:
            raise KeyError("project not found")
        if session_id:
            session = self.store._row(
                "SELECT project_id,scope_id,ended_at FROM sessions WHERE id=?",
                (session_id,),
            )
            if not session:
                raise KeyError("session not found")
            if session["project_id"] != project_id:
                raise ValueError("session belongs to a different project")
            if mode == "interim" and session["ended_at"] is not None:
                raise ValueError(
                    "interim checkpoints require an active session"
                )
            if mode == "final" and session["ended_at"] is not None:
                raise ValueError("final checkpoints require an active session")
            if scope_id is None:
                scope_id = session["scope_id"]
        if mode == "final":
            if not session_id:
                raise ValueError("final checkpoints require an active session")
            if not verified_event_ids:
                raise ValueError(
                    "final checkpoints require verified_event_ids"
                )
            if not handoff_title or not handoff_title.strip():
                raise ValueError("final checkpoints require handoff_title")
            if not handoff_content or not handoff_content.strip():
                raise ValueError("final checkpoints require handoff_content")
            if commit:
                if not repository_path:
                    raise ValueError("commit requires repository_path")
                try:
                    commit = subprocess.run(
                        [
                            "git",
                            "rev-parse",
                            "--verify",
                            f"{commit}^{{commit}}",
                        ],
                        cwd=repository_path,
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise ValueError(
                        "commit must identify an existing repository commit"
                    ) from exc
        if scope_id:
            scope = self.store._row(
                "SELECT project_id FROM scopes WHERE id=?", (scope_id,)
            )
            if not scope:
                raise KeyError("scope not found")
            if scope["project_id"] != project_id:
                raise ValueError("scope belongs to a different project")
        cursor = self.store._row(
            "SELECT next_seq-1 AS value FROM project_event_cursors WHERE"
            " project_id=?",
            (project_id,),
        )["value"]
        if source_event_cursor is None:
            source_event_cursor = cursor
        if source_event_cursor < 0 or source_event_cursor > cursor:
            raise ValueError(
                "source_event_cursor must reference an existing project event"
                " cursor"
            )
        recovery_hash = self.store._checkpoint_recovery_hash(
            project_id,
            source_event_cursor,
            goal,
            completed,
            next_step,
            blockers,
            repository,
        )
        payload = {
            "schema_version": 5,
            "mode": mode,
            "reason": reason,
            "goal": goal.strip(),
            "completed": [item.strip() for item in completed],
            "next_step": next_step.strip() if next_step else None,
            "blockers": [item.strip() for item in blockers],
            "source_event_cursor": source_event_cursor,
            "context_usage": context_usage,
            "recovery_hash": recovery_hash,
            "claims": (
                {"completion": False, "verification": False}
                if mode == "interim"
                else {"completion": True, "verification": True}
            ),
            "verified_event_ids": verified_event_ids,
            "commit": commit,
            "objective": {"repository": repository, "test_results": tests},
        }
        content = canonical(payload)
        created_at = self.now()
        with self.store.tx() as cx:
            existing = cx.execute(
                "SELECT request_hash,response_json FROM idempotency_keys WHERE"
                " operation=? AND key=?",
                ("create_checkpoint", idempotency_key),
            ).fetchone()
            if existing:
                digest = hashlib.sha256(
                    canonical(request).encode()
                ).hexdigest()
                if digest != existing["request_hash"]:
                    raise ValueError(
                        "idempotency key reused with a different request"
                    )
                return json.loads(existing["response_json"])
            next_cursor = cx.execute(
                "UPDATE project_event_cursors SET next_seq=next_seq+1 WHERE"
                " project_id=? RETURNING next_seq-1",
                (project_id,),
            ).fetchone()
            if not next_cursor:
                raise KeyError("project not found")
            event = {
                "id": self.uid(),
                "project_id": project_id,
                "scope_id": scope_id,
                "session_id": session_id,
                "kind": "checkpoint",
                "content": content,
                "source_uri": None,
                "metadata_json": canonical({"checkpoint": payload}),
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "created_at": created_at,
                "event_seq": next_cursor[0],
            }
            cx.execute(
                """INSERT INTO events(id,project_id,scope_id,session_id,
              kind,content,source_uri,metadata_json,content_hash,created_at,
              event_seq) VALUES(:id,:project_id,:scope_id,:session_id,
              :kind,:content,:source_uri,:metadata_json,:content_hash,
              :created_at,:event_seq)""",
                event,
            )
            self.store._audit(
                cx, project_id, "event", event["id"], "recorded", event
            )
            handoff = None
            if mode == "final":
                for event_id in verified_event_ids:
                    source = cx.execute(
                        "SELECT project_id FROM events WHERE id=?", (event_id,)
                    ).fetchone()
                    if not source or source["project_id"] != project_id:
                        raise ValueError(f"invalid verified event: {event_id}")
                previous = None
                if previous_handoff_memory_id:
                    previous = cx.execute(
                        "SELECT * FROM memories WHERE id=?",
                        (previous_handoff_memory_id,),
                    ).fetchone()
                    if not previous or previous["project_id"] != project_id:
                        raise ValueError(
                            "previous handoff must belong to the same project"
                        )
                    if previous["status"] != "active":
                        raise ValueError("previous handoff must be active")
                handoff_id, handoff_ts = self.uid(), self.now()
                handoff = {
                    "id": handoff_id,
                    "project_id": project_id,
                    "scope_id": scope_id,
                    "type": "task",
                    "status": "active",
                    "title": handoff_title.strip(),
                    "content": handoff_content.strip(),
                    "confidence": 1.0,
                    "importance": 1.0,
                    "valid_from": None,
                    "valid_until": None,
                    "tags_json": canonical(["handoff", "checkpoint"]),
                    "created_at": handoff_ts,
                    "updated_at": handoff_ts,
                    "observed_at": handoff_ts,
                    "last_confirmed_at": handoff_ts,
                    "visibility": "project",
                }
                cx.execute(
                    """INSERT INTO memories(id,project_id,scope_id,type,
                  status,title,content,confidence,importance,valid_from,
                  valid_until,tags_json,created_at,updated_at,observed_at,
                  last_confirmed_at,visibility) VALUES(:id,:project_id,
                  :scope_id,:type,:status,:title,:content,:confidence,
                  :importance,:valid_from,:valid_until,:tags_json,
                  :created_at,:updated_at,:observed_at,:last_confirmed_at,
                  :visibility)""",
                    handoff,
                )
                for event_id in [event["id"], *verified_event_ids]:
                    cx.execute(
                        "INSERT INTO memory_sources VALUES(?,?,?,?)",
                        (handoff_id, event_id, "", handoff_ts),
                    )
                self.store._index_embedding(cx, handoff)
                self.store._audit(
                    cx, project_id, "memory", handoff_id, "created", handoff
                )
                if previous:
                    cx.execute(
                        "UPDATE memories SET status='superseded',updated_at=?"
                        " WHERE id=?",
                        (handoff_ts, previous_handoff_memory_id),
                    )
                    cx.execute(
                        "INSERT INTO edges VALUES(?,?,?,?,?,?,?)",
                        (
                            self.uid(),
                            project_id,
                            handoff_id,
                            previous_handoff_memory_id,
                            "supersedes",
                            "replaced by final checkpoint",
                            handoff_ts,
                        ),
                    )
                    self.store._audit(
                        cx,
                        project_id,
                        "memory",
                        previous_handoff_memory_id,
                        "status:superseded",
                        {"replacement_memory_id": handoff_id},
                    )
                cx.execute(
                    "UPDATE sessions SET ended_at=? WHERE id=?",
                    (handoff_ts, session_id),
                )
                self.store._audit(
                    cx,
                    project_id,
                    "session",
                    session_id,
                    "ended",
                    {"checkpoint_id": event["id"], "ended_at": handoff_ts},
                )
            result = {
                "checkpoint_id": event["id"],
                "event_seq": event["event_seq"],
                "created_at": created_at,
                "handoff_memory_id": handoff["id"] if handoff else None,
                "previous_handoff_memory_id": (
                    previous_handoff_memory_id if handoff else None
                ),
                "session_ended": mode == "final",
                **payload,
            }
            self.store._save_idem(
                cx, "create_checkpoint", idempotency_key, request, result
            )
        return result

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
        """Evaluate portable triggers without writing a checkpoint."""
        if context_usage is not None and not 0 <= context_usage <= 1:
            raise ValueError("context_usage must be between 0 and 1")
        policy = self.store.get_policy(project_id)
        session = None
        if session_id:
            session = self.store.checkpoints.session_start(session_id)
            if not session:
                raise KeyError("session not found")
            if session["project_id"] != project_id:
                raise ValueError("session belongs to a different project")
        latest = self.store.checkpoints.latest(project_id)
        cursor = self.store.checkpoints.event_cursor(project_id)
        if cursor is None:
            raise KeyError("project not found")
        current_repository = (
            self.store._repository_facts(repository_path)
            if repository_path
            else None
        )
        latest_payload = (
            json.loads(latest["metadata_json"]).get("checkpoint", {})
            if latest
            else {}
        )
        prior_repository = None
        if latest:
            prior_repository = latest_payload.get("objective", {}).get(
                "repository"
            )
        repository_changed = bool(
            current_repository
            and (
                prior_repository is None
                or any(
                    current_repository.get(key) != prior_repository.get(key)
                    for key in ("head", "dirty", "changed_files")
                )
            )
        )
        baseline_cursor = latest["event_seq"] if latest else 0
        durable_event_count = self.store.checkpoints.durable_events_after(
            project_id, baseline_cursor
        )
        current_time = self.current_datetime()
        checkpoint_age = (
            int(
                (
                    current_time - datetime.fromisoformat(latest["created_at"])
                ).total_seconds()
            )
            if latest
            else None
        )
        session_elapsed = (
            int(
                (
                    current_time
                    - datetime.fromisoformat(session["started_at"])
                ).total_seconds()
            )
            if session
            else None
        )
        material_change = repository_changed or durable_event_count > 0
        recovery_hash = self.store._checkpoint_recovery_hash(
            project_id,
            cursor,
            goal,
            completed or [],
            next_step,
            blockers or [],
            current_repository,
        )
        evaluation = evaluate_checkpoint_policy(
            context_usage=context_usage,
            material_change=material_change,
            repository_changed=repository_changed,
            durable_event_count=durable_event_count,
            session_elapsed=session_elapsed,
            checkpoint_age=checkpoint_age,
            recoverable_state_changed=(
                latest_payload.get("recovery_hash") != recovery_hash
            ),
            latest_context_usage=latest_payload.get("context_usage"),
            policy=policy,
        )
        suggested_key = f"checkpoint:{project_id}:{recovery_hash}"
        return {
            "project_id": project_id,
            **evaluation,
            "recovery_hash": recovery_hash,
            "suggested_idempotency_key": suggested_key,
            "latest_checkpoint_id": latest["id"] if latest else None,
            "event_cursor": cursor,
        }
