from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from typing import Any

from . import __version__
from .mcp_tools import CORE_TOOL_NAMES, TOOL_BY_NAME, TOOL_PAGE_SIZE, TOOLS
from .store import MemoryStore

PROTOCOL = "2025-06-18"
INSTRUCTIONS = (
    "At task start prefer one context_bootstrap call with the workspace, "
    "a focused query, compact response format, actual client name, and "
    "session/task ID. The separate project_resolve, session_start, and "
    "get_context tools remain available when individual control is needed. "
    "Preserve evidence with record_event before proposing memories. New "
    "inferred memories should remain proposed until verified; use active "
    "only for confirmed facts/decisions. Retrieve original evidence with "
    "get_source. Record consequential decisions during work and end the "
    "session when done."
)


def validate_json(
    value: Any, schema: dict[str, Any], path: str = "arguments"
) -> None:
    """Validate the JSON Schema subset used by MCP tool declarations."""
    expected = schema.get("type")
    matches = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: (
            isinstance(item, int) and not isinstance(item, bool)
        ),
        "number": lambda item: (
            isinstance(item, (int, float)) and not isinstance(item, bool)
        ),
    }
    if expected in matches and not matches[expected](value):
        raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} must be <= {schema['maximum']}")
    if expected == "array" and "items" in schema:
        for index, item in enumerate(value):
            validate_json(item, schema["items"], f"{path}[{index}]")
    if expected == "object":
        properties = schema.get("properties", {})
        missing = [
            name for name in schema.get("required", []) if name not in value
        ]
        if missing:
            raise ValueError(
                f"{path} missing required properties: {', '.join(missing)}"
            )
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ValueError(
                    f"{path} has unknown properties: {', '.join(extra)}"
                )
        for name, item in value.items():
            if name in properties:
                validate_json(item, properties[name], f"{path}.{name}")


def tool_page(
    cursor: Any = None, tools: list[dict[str, Any]] = TOOLS
) -> dict[str, Any]:
    if cursor is None:
        offset = 0
    elif (
        not isinstance(cursor, str)
        or not cursor.isascii()
        or not cursor.isdigit()
    ):
        raise ValueError(
            "params.cursor must be an opaque cursor returned by tools/list"
        )
    else:
        offset = int(cursor)
    if offset < 0 or offset >= len(tools):
        raise ValueError("params.cursor is invalid or expired")
    end = min(offset + TOOL_PAGE_SIZE, len(tools))
    result: dict[str, Any] = {"tools": tools[offset:end]}
    if end < len(tools):
        result["nextCursor"] = str(end)
    return result


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    """HTTPServer without startup reverse DNS lookup for its bind."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class MCPServer:
    def __init__(self, store: MemoryStore, tool_profile: str = "all"):
        if tool_profile not in {"core", "admin", "all"}:
            raise ValueError("tool_profile must be core, admin, or all")
        self.store = store
        self.tool_profile = tool_profile
        self.tools = (
            TOOLS
            if tool_profile == "all"
            else [
                t
                for t in TOOLS
                if (t["name"] in CORE_TOOL_NAMES) == (tool_profile == "core")
            ]
        )

    def bootstrap(
        self,
        cwd: str,
        query: str,
        client: str = "codex",
        external_id: str | None = None,
        metadata: dict | None = None,
        **context_args: Any,
    ) -> dict[str, Any]:
        resolved = self.store.resolve_project(cwd)
        session = self.store.start_session(
            resolved["project"]["id"],
            client,
            resolved["scope_id"],
            external_id,
            metadata,
        )
        context = self.store.get_context(
            resolved["project"]["id"],
            query,
            scope_id=resolved["scope_id"],
            **context_args,
        )
        return {
            "project": resolved["project"],
            "scope_id": resolved["scope_id"],
            "created": resolved["created"],
            "session": session,
            "context": context,
        }

    def call(self, name: str, a: dict[str, Any]) -> Any:
        mapping = {
            "context_bootstrap": self.bootstrap,
            "project_create": self.store.create_project,
            "project_list": self.store.list_projects,
            "project_resolve": self.store.resolve_project,
            "project_alias_list": self.store.list_project_aliases,
            "scope_create": self.store.create_scope,
            "session_start": self.store.start_session,
            "session_end": self.store.end_session,
            "record_event": self.store.record_event,
            "checkpoint_create": self.store.create_checkpoint,
            "checkpoint_evaluate": self.store.evaluate_checkpoint,
            "read_events_since": self.store.read_events_since,
            "event_poll": self.store.poll_events,
            "event_ack": self.store.acknowledge_events,
            "memory_upsert": self.store.upsert_memory,
            "memory_transition": self.store.transition,
            "search_alias_set": self.store.set_search_aliases,
            "search_alias_list": self.store.list_search_aliases,
            "relation_create": self.store.create_relation,
            "graph_traverse": self.store.traverse,
            "memory_search": self.store.search,
            "memory_feedback": self.store.record_memory_feedback,
            "get_context": self.store.get_context,
            "decision_context": self.store.decision_context,
            "investigation_create": self.store.create_investigation,
            "investigation_record_source": self.store.record_source_analysis,
            "investigation_get": self.store.get_investigation,
            "investigation_complete": self.store.complete_investigation,
            "source_reinspection_request": (
                self.store.request_source_reinspection
            ),
            "wiki_page_create": self.store.create_wiki_page,
            "wiki_note_set": self.store.set_wiki_notes,
            "wiki_revision_generate": self.store.generate_wiki_revision,
            "wiki_revision_transition": self.store.transition_wiki_revision,
            "wiki_page_get": self.store.get_wiki_page,
            "wiki_revision_render": self.store.render_wiki_revision,
            "wiki_browse": self.store.browse_wiki,
            "wiki_markdown_export": self.store.export_wiki_markdown,
            "wiki_revision_lint": self.store.lint_wiki_revision,
            "review_queue": self.store.review_queue,
            "memory_correct": self.store.propose_correction,
            "review_action": self.store.review_candidate,
            "policy_get": self.store.get_policy,
            "policy_set": self.store.set_policy,
            "search_health": self.store.search_health,
            "maintenance_status": self.store.maintenance_status,
            "maintenance_run": self.store.maintain,
            "backup_create": self.store.backup_to,
            "get_source": self.store.get_source,
            "get_audit": self.store.audit,
        }
        if name not in {tool["name"] for tool in self.tools}:
            raise KeyError(
                f"tool is not exposed by {self.tool_profile} profile: {name}"
            )
        if name not in mapping:
            raise KeyError(f"unknown tool: {name}")
        validate_json(a, TOOL_BY_NAME[name]["inputSchema"])
        return mapping[name](**a)

    def handle(self, req: dict[str, Any]) -> dict[str, Any] | None:
        if "id" not in req:
            return None
        rid, method = req.get("id"), req.get("method")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "context-memory",
                        "version": __version__,
                    },
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                params = req.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("params must be object")
                extra = sorted(set(params) - {"cursor", "_meta"})
                if extra:
                    raise ValueError(
                        f"params has unknown properties: {', '.join(extra)}"
                    )
                result = tool_page(params.get("cursor"), self.tools)
            elif method == "tools/call":
                p = req.get("params", {})
                if not isinstance(p, dict):
                    raise ValueError("params must be object")
                extra = sorted(set(p) - {"name", "arguments", "_meta"})
                if extra:
                    raise ValueError(
                        f"params has unknown properties: {', '.join(extra)}"
                    )
                if not isinstance(p.get("name"), str):
                    raise ValueError("params.name must be string")
                arguments = p.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise ValueError("params.arguments must be object")
                value = self.call(p["name"], arguments)
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                value, ensure_ascii=False, indent=2
                            ),
                        }
                    ],
                    "structuredContent": {"result": value},
                }
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except (ValueError, KeyError, TypeError) as exc:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32602, "message": str(exc)},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32603, "message": f"internal error: {exc}"},
            }

    def serve_stdio(self) -> None:
        for line in sys.stdin:
            try:
                response = self.handle(json.loads(line))
                if response is not None:
                    sys.stdout.write(
                        json.dumps(
                            response, ensure_ascii=False, separators=(",", ":")
                        )
                        + "\n"
                    )
                    sys.stdout.flush()
            except json.JSONDecodeError as exc:
                sys.stdout.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": str(exc)},
                        }
                    )
                    + "\n"
                )
                sys.stdout.flush()

    def serve_http(
        self, host: str, port: int, token: str | None = None
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"} and not token:
            raise ValueError(
                "refusing external bind without --token or "
                "CONTEXT_MEMORY_TOKEN"
            )
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/mcp":
                    self.send_error(404)
                    return
                if (
                    token
                    and self.headers.get("Authorization") != f"Bearer {token}"
                ):
                    self.send_error(401)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    request = json.loads(self.rfile.read(length))
                    response = server.handle(request)
                except Exception as exc:
                    self.send_error(400, str(exc))
                    return
                if response is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                body = json.dumps(response, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: Any) -> None:
                print("context-memory http: " + fmt % args, file=sys.stderr)

        LocalThreadingHTTPServer((host, port), Handler).serve_forever()
