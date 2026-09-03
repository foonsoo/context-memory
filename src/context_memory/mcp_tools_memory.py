"""MCP memory tool declarations."""

from .mcp_schema import obj

TOOLS = [
    {
        "name": "context_recall",
        "description": (
            "Retrieve a small, session-independent context pack for a "
            "natural-language continuation request. Results are bounded by "
            "an estimated token budget; fetch source details separately."
        ),
        "inputSchema": obj(
            {
                "cwd": {"type": "string"},
                "query": {"type": "string"},
                "token_budget": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 2048,
                },
                "max_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                },
            },
            ["cwd", "query"],
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_upsert",
        "description": (
            "Propose, activate, or update a derived project/global memory "
            "with source provenance."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "title": {"type": "string"},
                "content": {"type": "string"},
                "memory_type": {
                    "type": "string",
                    "enum": [
                        "fact",
                        "decision",
                        "preference",
                        "constraint",
                        "procedure",
                        "summary",
                        "task",
                        "other",
                    ],
                },
                "status": {
                    "type": "string",
                    "enum": [
                        "proposed",
                        "active",
                        "superseded",
                        "disputed",
                        "expired",
                        "rejected",
                    ],
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "scope_id": {"type": "string"},
                "source_event_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "valid_from": {"type": "string"},
                "valid_until": {"type": "string"},
                "observed_at": {"type": "string"},
                "last_confirmed_at": {"type": "string"},
                "visibility": {
                    "type": "string",
                    "enum": ["project", "global"],
                },
                "tags": {"type": "array", "items": {"type": "string"}},
                "memory_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            ["project_id", "title", "content"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "memory_transition",
        "description": (
            "Verify/activate, supersede, dispute, expire, or reject a memory; "
            "optionally link the challenging/replacement memory."
        ),
        "inputSchema": obj(
            {
                "memory_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [
                        "active",
                        "superseded",
                        "disputed",
                        "expired",
                        "rejected",
                    ],
                },
                "related_memory_id": {"type": "string"},
                "note": {"type": "string"},
            },
            ["memory_id", "status"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "search_alias_set",
        "description": (
            "Set deterministic project vocabulary aliases used to expand "
            "lexical queries without embeddings."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "term": {"type": "string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
            },
            ["project_id", "term", "aliases"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "search_alias_list",
        "description": "List deterministic query aliases for a project.",
        "inputSchema": obj({"project_id": {"type": "string"}}, ["project_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "relation_create",
        "description": (
            "Create a verified relation between two memories without "
            "changing the evidence ledger."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "from_memory_id": {"type": "string"},
                "to_memory_id": {"type": "string"},
                "relation": {
                    "type": "string",
                    "enum": ["supports", "depends_on", "related_to"],
                },
                "note": {"type": "string"},
            },
            ["project_id", "from_memory_id", "to_memory_id", "relation"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "graph_traverse",
        "description": (
            "Traverse verified memory relations up to five hops; "
            "active/disputed nodes are returned by default."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "memory_id": {"type": "string"},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 5},
                "direction": {
                    "type": "string",
                    "enum": ["outgoing", "incoming", "both"],
                },
                "relations": {"type": "array", "items": {"type": "string"}},
                "statuses": {"type": "array", "items": {"type": "string"}},
            },
            ["project_id", "memory_id"],
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_search",
        "description": (
            "Rank memories locally by default, or across the shared project "
            "registry when discover_projects is true."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "statuses": {"type": "array", "items": {"type": "string"}},
                "scope_id": {"type": "string"},
                "discover_projects": {"type": "boolean"},
            },
            ["project_id", "query"],
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_feedback",
        "description": (
            "Record whether a retrieved memory was used, helpful, or "
            "incorrect so personal ranking can improve."
        ),
        "inputSchema": obj(
            {
                "memory_id": {"type": "string"},
                "signal": {
                    "type": "string",
                    "enum": ["retrieved", "used", "helpful", "incorrect"],
                },
            },
            ["memory_id", "signal"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "review_queue",
        "description": (
            "List proposed memory candidates and latest Decision Wiki "
            "revisions with deterministic lint findings in one explicit "
            "review queue. Actionable proposed Wiki revisions are ordered "
            "before accumulated memory candidates; queue_priority and "
            "created_at make the deterministic order inspectable. Memory "
            "candidates use review_action; proposed Wiki revisions expose "
            "approve/reject routes through wiki_revision_transition."
        ),
        "inputSchema": obj({"project_id": {"type": "string"}}, ["project_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "memory_correct",
        "description": (
            "Create an evidence-backed proposed correction for review."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "memory_id": {"type": "string"},
                "content": {"type": "string"},
                "title": {"type": "string"},
            },
            ["project_id", "memory_id", "content"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "review_action",
        "description": (
            "Approve/reject a candidate or use it to supersede/dispute an "
            "existing memory."
        ),
        "inputSchema": obj(
            {
                "memory_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["approve", "reject", "supersede", "dispute"],
                },
                "related_memory_id": {"type": "string"},
                "note": {"type": "string"},
            },
            ["memory_id", "action"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "get_context",
        "description": (
            "Retrieve bounded, provenance-preserving context; compact format "
            "removes duplicate serialization without rewriting memories."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "query": {"type": "string"},
                "char_budget": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100000,
                },
                "statuses": {"type": "array", "items": {"type": "string"}},
                "scope_id": {"type": "string"},
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
            ["project_id", "query"],
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "decision_context",
        "description": (
            "Compose a versioned, cited Decision Brief from existing "
            "retrieval without generating a recommendation or changing "
            "memory state."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "question": {"type": "string"},
                "char_budget": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100000,
                },
                "scope_id": {"type": "string"},
                "discover_projects": {"type": "boolean"},
            },
            ["project_id", "question"],
        ),
        "annotations": {"readOnlyHint": True},
    },
]
