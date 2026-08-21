from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .cli import default_db
from .store import MemoryStore


def checkpoint_from_hook(
    store: MemoryStore,
    project_id: str,
    session_id: str | None,
    payload: dict,
    scope_id: str | None = None,
) -> dict | None:
    """Convert a client lifecycle signal into a checkpoint call.

    Hook payloads are treated as unverified recovery state. A client may
    explicitly request a final checkpoint only by supplying the verified
    evidence and handoff fields required by
    ``MemoryStore.create_checkpoint``.
    """
    state = payload.get("context_memory") or {}
    if not isinstance(state, dict):
        raise ValueError("context_memory hook state must be an object")
    goal = str(
        state.get("goal")
        or payload.get("last_assistant_message")
        or "Resume client work"
    )
    completed = state.get("completed") or []
    blockers = state.get("blockers") or []
    next_step = state.get("next_step")
    mode = state.get("mode")
    if mode == "final":
        return store.create_checkpoint(
            project_id,
            "final",
            state.get("reason", "completed"),
            goal,
            state.get("idempotency_key") or f"hook-final:{session_id}",
            session_id=session_id,
            scope_id=scope_id,
            completed=completed,
            next_step=next_step,
            blockers=blockers,
            source_event_cursor=state.get("source_event_cursor"),
            context_usage=state.get("context_usage"),
            repository_path=state.get("repository_path"),
            test_results=state.get("test_results"),
            verified_event_ids=state.get("verified_event_ids"),
            handoff_title=state.get("handoff_title"),
            handoff_content=state.get("handoff_content"),
            previous_handoff_memory_id=state.get("previous_handoff_memory_id"),
            commit=state.get("commit"),
        )
    evaluation = store.evaluate_checkpoint(
        project_id,
        context_usage=state.get("context_usage"),
        session_id=session_id,
        repository_path=state.get("repository_path"),
        goal=goal,
        completed=completed,
        next_step=next_step,
        blockers=blockers,
    )
    if not evaluation["should_checkpoint"] and not state.get("force"):
        return None
    return store.create_checkpoint(
        project_id,
        "interim",
        evaluation.get("recommended_reason") or state.get("reason", "manual"),
        goal,
        state.get("idempotency_key")
        or evaluation["suggested_idempotency_key"],
        session_id=session_id,
        scope_id=scope_id,
        completed=completed,
        next_step=next_step,
        blockers=blockers,
        source_event_cursor=state.get("source_event_cursor"),
        context_usage=state.get("context_usage"),
        repository_path=state.get("repository_path"),
        test_results=state.get("test_results"),
    )


def main() -> None:
    payload = json.load(sys.stdin)
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    store = MemoryStore(default_db())
    try:
        slug = os.environ.get("CONTEXT_MEMORY_PROJECT")
        if slug:
            project = next(
                (p for p in store.list_projects() if p["slug"] == slug), None
            )
            if not project:
                project = store.create_project(slug, slug)
            scope_id = None
        else:
            resolved = store.resolve_project(payload.get("cwd") or os.getcwd())
            project, scope_id = resolved["project"], resolved["scope_id"]
        session_id = payload.get("session_id")
        if action == "start":
            store.start_session(
                project["id"],
                "codex",
                scope_id=scope_id,
                external_id=session_id,
                metadata={
                    "cwd": payload.get("cwd"),
                    "model": payload.get("model"),
                },
            )
            cwd_terms = " ".join(Path(payload.get("cwd") or ".").parts[-2:])
            query = (
                f"{cwd_terms} architecture decision constraint procedure task"
            )
            context = store.get_context(
                project["id"],
                query,
                int(os.environ.get("CONTEXT_MEMORY_CONTEXT_BUDGET", "5000")),
            )
            text = (
                context["context"]
                or "No verified relevant memories found. Record source "
                "events before proposing new memories."
            )
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": "Context Memory:\n" + text,
                        }
                    },
                    ensure_ascii=False,
                )
            )
        elif action == "prompt":
            query = (payload.get("prompt") or "").strip()
            context = store.get_context(
                project["id"],
                query,
                int(os.environ.get("CONTEXT_MEMORY_CONTEXT_BUDGET", "5000")),
                scope_id=scope_id,
            )
            if context["context"]:
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "UserPromptSubmit",
                                "additionalContext": (
                                    "Relevant verified Context Memory for "
                                    "this request:\n"
                                )
                                + context["context"],
                            }
                        },
                        ensure_ascii=False,
                    )
                )
        elif action == "end":
            row = store.conn.execute(
                "SELECT id FROM sessions WHERE project_id=? "
                "AND client='codex' AND external_id=?",
                (project["id"], session_id),
            ).fetchone()
            summary = payload.get("last_assistant_message")
            if row:
                if summary:
                    store.record_event(
                        project["id"],
                        "session_output",
                        summary,
                        session_id=row["id"],
                        metadata={"unverified_ai_output": True},
                        idempotency_key=f"session-end:{session_id}",
                    )
                checkpoint = checkpoint_from_hook(
                    store, project["id"], row["id"], payload, scope_id
                )
                if not checkpoint or checkpoint["mode"] != "final":
                    store.end_session(
                        row["id"],
                        summary=(
                            "Raw final assistant output recorded; not "
                            "promoted to memory."
                        ),
                    )
    finally:
        store.close()


if __name__ == "__main__":
    main()
