"""Ordered MCP tool catalog and profiles."""

from .mcp_tools_admin import TOOLS as ADMIN_TOOLS
from .mcp_tools_checkpoint_events import TOOLS as CHECKPOINT_EVENT_TOOLS
from .mcp_tools_investigation import TOOLS as INVESTIGATION_TOOLS
from .mcp_tools_lifecycle import TOOLS as LIFECYCLE_TOOLS
from .mcp_tools_memory import TOOLS as MEMORY_TOOLS
from .mcp_tools_wiki import TOOLS as WIKI_TOOLS

TOOLS = [
    *LIFECYCLE_TOOLS,
    *CHECKPOINT_EVENT_TOOLS,
    *MEMORY_TOOLS,
    *INVESTIGATION_TOOLS,
    *WIKI_TOOLS,
    *ADMIN_TOOLS,
]

TOOL_PAGE_SIZE = 10
TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}
CORE_TOOL_NAMES = {
    "context_recall",
    "context_bootstrap",
    "project_resolve",
    "session_start",
    "session_end",
    "record_event",
    "read_events_since",
    "event_poll",
    "event_ack",
    "memory_upsert",
    "memory_transition",
    "memory_search",
    "memory_feedback",
    "get_context",
    "decision_context",
    "get_source",
    "checkpoint_create",
    "checkpoint_evaluate",
    "review_queue",
    "review_action",
    "investigation_create",
    "investigation_record_source",
    "investigation_get",
    "investigation_complete",
    "wiki_page_create",
    "source_reinspection_request",
    "wiki_note_set",
    "wiki_revision_generate",
    "wiki_revision_transition",
    "wiki_page_get",
    "wiki_browse",
    "wiki_revision_render",
    "wiki_markdown_export",
    "wiki_revision_lint",
}
