from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .cli import default_db
from .store import MemoryStore


def main() -> None:
    payload = json.load(sys.stdin); action = sys.argv[1] if len(sys.argv) > 1 else "start"
    store = MemoryStore(default_db())
    try:
        slug = os.environ.get("CONTEXT_MEMORY_PROJECT")
        if slug:
            project = next((p for p in store.list_projects() if p["slug"] == slug), None)
            if not project: project = store.create_project(slug, slug)
            scope_id = None
        else:
            resolved = store.resolve_project(payload.get("cwd") or os.getcwd())
            project, scope_id = resolved["project"], resolved["scope_id"]
        session_id = payload.get("session_id")
        if action == "start":
            store.start_session(project["id"], "codex", scope_id=scope_id, external_id=session_id, metadata={"cwd":payload.get("cwd"),"model":payload.get("model")})
            cwd_terms = " ".join(Path(payload.get("cwd") or ".").parts[-2:])
            query = f"{cwd_terms} architecture decision constraint procedure task"
            context = store.get_context(project["id"], query, int(os.environ.get("CONTEXT_MEMORY_CONTEXT_BUDGET", "5000")))
            text = context["context"] or "No verified relevant memories found. Record source events before proposing new memories."
            print(json.dumps({"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"Context Memory:\n" + text}}, ensure_ascii=False))
        elif action == "prompt":
            query = (payload.get("prompt") or "").strip()
            context = store.get_context(project["id"], query, int(os.environ.get("CONTEXT_MEMORY_CONTEXT_BUDGET", "5000")), scope_id=scope_id)
            if context["context"]:
                print(json.dumps({"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"Relevant verified Context Memory for this request:\n" + context["context"]}}, ensure_ascii=False))
        elif action == "end":
            row = store.conn.execute("SELECT id FROM sessions WHERE project_id=? AND client='codex' AND external_id=?", (project["id"], session_id)).fetchone()
            summary = payload.get("last_assistant_message")
            if row:
                if summary: store.record_event(project["id"], "session_output", summary, session_id=row["id"], metadata={"unverified_ai_output":True}, idempotency_key=f"session-end:{session_id}")
                store.end_session(row["id"], summary="Raw final assistant output recorded; not promoted to memory.")
    finally: store.close()


if __name__ == "__main__": main()
