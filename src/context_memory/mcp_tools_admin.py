"""MCP admin tool declarations."""

from .mcp_schema import obj

TOOLS = [
    {
        "name": "policy_get",
        "description": (
            "Read bounded context, retention, and checkpoint trigger policy "
            "for a project."
        ),
        "inputSchema": obj({"project_id": {"type": "string"}}, ["project_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "policy_set",
        "description": (
            "Set project operational bounds, checkpoint triggers, message "
            "expiry, and scheduled-maintenance interval."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "max_context_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 20000,
                },
                "max_context_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                },
                "audit_keep_entries": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 100000,
                },
                "terminal_memory_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3650,
                },
                "checkpoint_soft_usage": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "checkpoint_hard_usage": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "checkpoint_elapsed_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 86400,
                },
                "checkpoint_event_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10000,
                },
                "checkpoint_max_age_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 604800,
                },
                "checkpoint_cooldown_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 86400,
                },
                "checkpoint_hysteresis": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 0.5,
                },
                "maintenance_interval_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2592000,
                },
                "message_ttl_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2592000,
                },
            },
            ["project_id"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "search_health",
        "description": (
            "Check that every authoritative memory has exactly one FTS "
            "projection and report missing, duplicate, or orphan rows."
        ),
        "inputSchema": obj({"project_id": {"type": "string"}}, ["project_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "maintenance_status",
        "description": (
            "Report policy, live/terminal counts, audit checkpoints, and "
            "search projection health."
        ),
        "inputSchema": obj({"project_id": {"type": "string"}}, ["project_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "maintenance_run",
        "description": (
            "Plan or apply policy-based terminal-memory purge and "
            "checkpointed audit compaction. Raw source events are never "
            "deleted."
        ),
        "inputSchema": obj(
            {"project_id": {"type": "string"}, "apply": {"type": "boolean"}},
            ["project_id"],
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "backup_create",
        "description": (
            "Create an integrity-checked single-file SQLite snapshot with the "
            "Online Backup API, including committed WAL data."
        ),
        "inputSchema": obj(
            {"output_path": {"type": "string"}}, ["output_path"]
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "get_source",
        "description": (
            "Retrieve the immutable raw event behind a memory citation."
        ),
        "inputSchema": obj({"event_id": {"type": "string"}}, ["event_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_audit",
        "description": (
            "Read append-only history for an event, memory, session, project, "
            "or scope."
        ),
        "inputSchema": obj(
            {
                "entity_type": {"type": "string"},
                "entity_id": {"type": "string"},
            },
            ["entity_type", "entity_id"],
        ),
        "annotations": {"readOnlyHint": True},
    },
]
