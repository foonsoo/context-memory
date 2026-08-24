"""MCP checkpoint events tool declarations."""

from .mcp_schema import obj

TOOLS = [
    {
        "name": "checkpoint_create",
        "description": (
            "Idempotently record an interim recovery checkpoint or "
            "atomically publish a verified final handoff and close its "
            "session. Final mode requires an active session, verified event "
            "IDs, and handoff content; it may replace one active handoff and "
            "record an existing commit. Git is never mutated."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["interim", "final"]},
                "reason": {
                    "type": "string",
                    "enum": [
                        "context_budget",
                        "elapsed",
                        "material_change",
                        "completed",
                        "manual",
                    ],
                },
                "goal": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "session_id": {"type": "string"},
                "scope_id": {"type": "string"},
                "completed": {"type": "array", "items": {"type": "string"}},
                "next_step": {"type": "string"},
                "blockers": {"type": "array", "items": {"type": "string"}},
                "source_event_cursor": {"type": "integer", "minimum": 0},
                "context_usage": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "repository_path": {"type": "string"},
                "test_results": {
                    "type": "array",
                    "items": obj(
                        {
                            "name": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["passed", "failed", "skipped"],
                            },
                            "command": {"type": "string"},
                            "details": {"type": "string"},
                        },
                        ["name", "status"],
                    ),
                },
                "verified_event_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "handoff_title": {"type": "string"},
                "handoff_content": {"type": "string"},
                "previous_handoff_memory_id": {"type": "string"},
                "commit": {"type": "string"},
            },
            ["project_id", "mode", "reason", "goal", "idempotency_key"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "checkpoint_evaluate",
        "description": (
            "Evaluate checkpoint thresholds, cooldown, hysteresis, and "
            "recoverable-state deduplication without writing a checkpoint."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "context_usage": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "session_id": {"type": "string"},
                "repository_path": {"type": "string"},
                "goal": {"type": "string"},
                "completed": {"type": "array", "items": {"type": "string"}},
                "next_step": {"type": "string"},
                "blockers": {"type": "array", "items": {"type": "string"}},
            },
            ["project_id"],
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "read_events_since",
        "description": (
            "Read immutable project events after a cursor in sequence order. "
            "Omit kinds for all events; use kinds=[message] for inter-session "
            "polling."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "cursor": {"type": "integer", "minimum": 0},
                "kinds": {"type": "array", "items": {"type": "string"}},
                "scope_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            ["project_id"],
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "event_poll",
        "description": (
            "Poll an exact event stream from a durable per-consumer "
            "acknowledged cursor. Delivery does not advance acknowledgement."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "consumer_id": {"type": "string"},
                "kinds": {"type": "array", "items": {"type": "string"}},
                "scope_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            ["project_id", "consumer_id"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "event_ack",
        "description": (
            "Monotonically acknowledge a cursor previously delivered by "
            "event_poll for the same consumer and stream definition."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "consumer_id": {"type": "string"},
                "cursor": {"type": "integer", "minimum": 0},
                "kinds": {"type": "array", "items": {"type": "string"}},
                "scope_id": {"type": "string"},
            },
            ["project_id", "consumer_id", "cursor"],
        ),
        "annotations": {"readOnlyHint": False},
    },
]
