"""MCP lifecycle tool declarations."""

from .contracts import promotable_kinds_text
from .mcp_schema import obj

TOOLS = [
    {
        "name": "context_bootstrap",
        "description": (
            "Resolve a workspace, idempotently start its session, and "
            "retrieve focused context in one call. Unless an event cursor is "
            "provided, also include a bounded tail of recent promotable "
            "immutable events so unpromoted handoffs and repository-path "
            "clues are not missed."
        ),
        "inputSchema": obj(
            {
                "cwd": {"type": "string"},
                "query": {"type": "string"},
                "client": {"type": "string"},
                "external_id": {"type": "string"},
                "metadata": {"type": "object"},
                "char_budget": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100000,
                },
                "statuses": {"type": "array", "items": {"type": "string"}},
                "event_cursor": {"type": "integer", "minimum": 0},
                "event_kinds": {"type": "array", "items": {"type": "string"}},
                "event_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                },
                "event_char_budget": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 4000,
                },
                "discover_projects": {"type": "boolean"},
                "response_format": {
                    "type": "string",
                    "enum": ["legacy", "compact"],
                },
            },
            ["cwd", "query"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "project_create",
        "description": "Create a local memory project.",
        "inputSchema": obj(
            {
                "slug": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            ["slug"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "project_list",
        "description": "List local memory projects.",
        "inputSchema": obj({}),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "project_resolve",
        "description": (
            "Resolve a workspace hint using canonical paths and "
            "unambiguous registered project names."
        ),
        "inputSchema": obj({"cwd": {"type": "string"}}, ["cwd"]),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "project_alias_list",
        "description": (
            "List registered path and project-name identities for a project."
        ),
        "inputSchema": obj({"project_id": {"type": "string"}}, ["project_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "scope_create",
        "description": "Create a project scope, optionally tied to a path.",
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "name": {"type": "string"},
                "path": {"type": "string"},
            },
            ["project_id", "name"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "session_start",
        "description": "Start or resume an idempotent client session.",
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "client": {"type": "string"},
                "scope_id": {"type": "string"},
                "external_id": {"type": "string"},
                "metadata": {"type": "object"},
            },
            ["project_id"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "session_end",
        "description": (
            "End a session, audit its summary, and extract evidence-backed "
            "proposed memories for review."
        ),
        "inputSchema": obj(
            {
                "session_id": {"type": "string"},
                "summary": {"type": "string"},
                "extract_candidates": {"type": "boolean"},
            },
            ["session_id"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "record_event",
        "description": (
            "Append immutable raw evidence. Session events with these kinds "
            "are eligible for automatic proposed-memory extraction: "
            f"{promotable_kinds_text()}. Other kinds, including message, "
            "remain valid immutable events but are not automatically "
            "promoted; the response includes a non-fatal promotion advisory. "
            "Success is returned only after durable DB commit."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "kind": {"type": "string"},
                "content": {"type": "string"},
                "session_id": {"type": "string"},
                "scope_id": {"type": "string"},
                "source_uri": {"type": "string"},
                "metadata": {"type": "object"},
                "idempotency_key": {"type": "string"},
            },
            ["project_id", "kind", "content"],
        ),
        "annotations": {"readOnlyHint": False},
    },
]
