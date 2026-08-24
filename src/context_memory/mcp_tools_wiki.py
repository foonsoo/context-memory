"""MCP wiki tool declarations."""

from .mcp_schema import obj

TOOLS = [
    {
        "name": "wiki_page_create",
        "description": (
            "Create a topic-oriented Decision Wiki page. Manual notes remain "
            "separate from generated revisions."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "topic": {"type": "string"},
                "title": {"type": "string"},
                "scope_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            ["project_id", "topic", "title"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "wiki_note_set",
        "description": (
            "Replace the explicitly manual notes attached to a Wiki page "
            "without modifying generated revision content."
        ),
        "inputSchema": obj(
            {
                "page_id": {"type": "string"},
                "manual_notes": {"type": "string"},
            },
            ["page_id", "manual_notes"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "wiki_revision_generate",
        "description": (
            "Create an immutable proposed Wiki revision from the cited "
            "Decision Brief projection."
        ),
        "inputSchema": obj(
            {
                "page_id": {"type": "string"},
                "question": {"type": "string"},
                "char_budget": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100000,
                },
                "generation_metadata": {"type": "object"},
                "idempotency_key": {"type": "string"},
            },
            ["page_id", "question"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "wiki_revision_transition",
        "description": (
            "Publish or reject a proposed Wiki revision, or explicitly mark "
            "a published revision stale."
        ),
        "inputSchema": obj(
            {
                "revision_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["published", "stale", "rejected"],
                },
                "reason": {"type": "string"},
            },
            ["revision_id", "status"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "wiki_page_get",
        "description": (
            "Read a Wiki page, separate manual notes, immutable revision "
            "history, and exact citations."
        ),
        "inputSchema": obj({"page_id": {"type": "string"}}, ["page_id"]),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "wiki_browse",
        "description": (
            "Browse a deterministic, paginated topic/page index and optional "
            "reverse citation backlinks for one Wiki page. This reads "
            "authoritative Wiki metadata and does not create a second search "
            "index."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "page_id": {"type": "string"},
                "scope_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0},
            },
            ["project_id"],
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "wiki_revision_render",
        "description": (
            "Render one stored Wiki revision as portable Markdown with "
            "citations and separate manual notes."
        ),
        "inputSchema": obj(
            {"revision_id": {"type": "string"}}, ["revision_id"]
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "wiki_markdown_export",
        "description": (
            "Export a deterministic, bounded set of current Wiki pages as "
            "linked Markdown documents with stable page and revision "
            "metadata. SQLite remains authoritative."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "scope_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0},
            },
            ["project_id"],
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "wiki_revision_lint",
        "description": (
            "Deterministically report citation, source, lifecycle, staleness, "
            "dispute, and omitted-current-memory findings without changing "
            "state."
        ),
        "inputSchema": obj(
            {"revision_id": {"type": "string"}}, ["revision_id"]
        ),
        "annotations": {"readOnlyHint": True},
    },
]
