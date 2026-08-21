from __future__ import annotations

from typing import Any, Callable

from .store import MemoryStore


def checkpoint_task(
    store: MemoryStore,
    arguments: dict[str, Any],
    publish: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute checkpoint_create for an optional MCP Tasks host adapter.

    The host owns protocol negotiation, task IDs, polling, and
    cancellation. This dependency-free adapter owns only the durable
    operation and status mapping. Synchronous MCP/CLI behavior remains
    unchanged for clients without Tasks.
    """
    if publish:
        publish("working", {"message": "Creating durable checkpoint"})
    try:
        result = store.create_checkpoint(**arguments)
    except Exception as exc:
        if publish:
            publish("failed", {"message": str(exc)})
        raise
    if publish:
        publish(
            "completed",
            {"checkpoint_id": result["checkpoint_id"], "result": result},
        )
    return result
