from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from typing import Any

from . import __version__
from .store import MemoryStore

PROTOCOL = "2025-06-18"
INSTRUCTIONS = ("At task start prefer one context_bootstrap call with the workspace, a focused query, compact response format, actual client name, and session/task ID. "
                "The separate project_resolve, session_start, and get_context tools remain available when individual control is needed. Preserve evidence with record_event before proposing memories. "
                "New inferred memories should remain proposed until verified; use active only for confirmed facts/decisions. "
                "Retrieve original evidence with get_source. Record consequential decisions during work and end the session when done.")


def obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


TOOLS = [
    {"name":"context_bootstrap","description":"Resolve a workspace, idempotently start its session, and retrieve focused context in one call.","inputSchema":obj({"cwd":{"type":"string"},"query":{"type":"string"},"client":{"type":"string"},"external_id":{"type":"string"},"metadata":{"type":"object"},"char_budget":{"type":"integer","minimum":0,"maximum":100000},"statuses":{"type":"array","items":{"type":"string"}},"event_cursor":{"type":"integer","minimum":0},"event_kinds":{"type":"array","items":{"type":"string"}},"event_limit":{"type":"integer","minimum":1,"maximum":1000},"event_char_budget":{"type":"integer","minimum":0,"maximum":4000},"discover_projects":{"type":"boolean"},"response_format":{"type":"string","enum":["legacy","compact"]}},["cwd","query"]),"annotations":{"readOnlyHint":False}},
    {"name": "project_create", "description": "Create a local memory project.", "inputSchema": obj({"slug":{"type":"string"},"name":{"type":"string"},"description":{"type":"string"},"idempotency_key":{"type":"string"}}, ["slug"]), "annotations":{"readOnlyHint":False}},
    {"name": "project_list", "description": "List local memory projects.", "inputSchema": obj({}), "annotations":{"readOnlyHint":True}},
    {"name": "project_resolve", "description": "Resolve a workspace hint using canonical paths and unambiguous registered project names.", "inputSchema": obj({"cwd":{"type":"string"}}, ["cwd"]), "annotations":{"readOnlyHint":False}},
    {"name":"project_alias_list","description":"List registered path and project-name identities for a project.","inputSchema":obj({"project_id":{"type":"string"}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name": "scope_create", "description": "Create a project scope, optionally tied to a path.", "inputSchema": obj({"project_id":{"type":"string"},"name":{"type":"string"},"path":{"type":"string"}}, ["project_id","name"]), "annotations":{"readOnlyHint":False}},
    {"name": "session_start", "description": "Start or resume an idempotent client session.", "inputSchema": obj({"project_id":{"type":"string"},"client":{"type":"string"},"scope_id":{"type":"string"},"external_id":{"type":"string"},"metadata":{"type":"object"}}, ["project_id"]), "annotations":{"readOnlyHint":False}},
    {"name": "session_end", "description": "End a session, audit its summary, and extract evidence-backed proposed memories for review.", "inputSchema": obj({"session_id":{"type":"string"},"summary":{"type":"string"},"extract_candidates":{"type":"boolean"}}, ["session_id"]), "annotations":{"readOnlyHint":False}},
    {"name": "record_event", "description": "Append immutable raw evidence. Use kind=message for unverified inter-session coordination; messages are not promoted into ranked memory automatically. Success is returned only after durable DB commit.", "inputSchema": obj({"project_id":{"type":"string"},"kind":{"type":"string"},"content":{"type":"string"},"session_id":{"type":"string"},"scope_id":{"type":"string"},"source_uri":{"type":"string"},"metadata":{"type":"object"},"idempotency_key":{"type":"string"}}, ["project_id","kind","content"]), "annotations":{"readOnlyHint":False}},
    {"name":"checkpoint_create","description":"Idempotently record an interim recovery checkpoint or atomically publish a verified final handoff and close its session. Final mode requires an active session, verified event IDs, and handoff content; it may replace one active handoff and record an existing commit. Git is never mutated.","inputSchema":obj({"project_id":{"type":"string"},"mode":{"type":"string","enum":["interim","final"]},"reason":{"type":"string","enum":["context_budget","elapsed","material_change","completed","manual"]},"goal":{"type":"string"},"idempotency_key":{"type":"string"},"session_id":{"type":"string"},"scope_id":{"type":"string"},"completed":{"type":"array","items":{"type":"string"}},"next_step":{"type":"string"},"blockers":{"type":"array","items":{"type":"string"}},"source_event_cursor":{"type":"integer","minimum":0},"context_usage":{"type":"number","minimum":0,"maximum":1},"repository_path":{"type":"string"},"test_results":{"type":"array","items":obj({"name":{"type":"string"},"status":{"type":"string","enum":["passed","failed","skipped"]},"command":{"type":"string"},"details":{"type":"string"}},["name","status"])},"verified_event_ids":{"type":"array","items":{"type":"string"}},"handoff_title":{"type":"string"},"handoff_content":{"type":"string"},"previous_handoff_memory_id":{"type":"string"},"commit":{"type":"string"}},["project_id","mode","reason","goal","idempotency_key"]),"annotations":{"readOnlyHint":False}},
    {"name":"checkpoint_evaluate","description":"Evaluate checkpoint thresholds, cooldown, hysteresis, and recoverable-state deduplication without writing a checkpoint.","inputSchema":obj({"project_id":{"type":"string"},"context_usage":{"type":"number","minimum":0,"maximum":1},"session_id":{"type":"string"},"repository_path":{"type":"string"},"goal":{"type":"string"},"completed":{"type":"array","items":{"type":"string"}},"next_step":{"type":"string"},"blockers":{"type":"array","items":{"type":"string"}}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name":"read_events_since","description":"Read immutable project events after a cursor in sequence order. Omit kinds for all events; use kinds=[message] for inter-session polling.","inputSchema":obj({"project_id":{"type":"string"},"cursor":{"type":"integer","minimum":0},"kinds":{"type":"array","items":{"type":"string"}},"scope_id":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":1000}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name": "memory_upsert", "description": "Propose, activate, or update a derived project/global memory with source provenance.", "inputSchema": obj({"project_id":{"type":"string"},"title":{"type":"string"},"content":{"type":"string"},"memory_type":{"type":"string","enum":["fact","decision","preference","constraint","procedure","summary","task","other"]},"status":{"type":"string","enum":["proposed","active","superseded","disputed","expired","rejected"]},"confidence":{"type":"number","minimum":0,"maximum":1},"importance":{"type":"number","minimum":0,"maximum":1},"scope_id":{"type":"string"},"source_event_ids":{"type":"array","items":{"type":"string"}},"valid_from":{"type":"string"},"valid_until":{"type":"string"},"observed_at":{"type":"string"},"last_confirmed_at":{"type":"string"},"visibility":{"type":"string","enum":["project","global"]},"tags":{"type":"array","items":{"type":"string"}},"memory_id":{"type":"string"},"idempotency_key":{"type":"string"}}, ["project_id","title","content"]), "annotations":{"readOnlyHint":False}},
    {"name": "memory_transition", "description": "Verify/activate, supersede, dispute, expire, or reject a memory; optionally link the challenging/replacement memory.", "inputSchema": obj({"memory_id":{"type":"string"},"status":{"type":"string","enum":["active","superseded","disputed","expired","rejected"]},"related_memory_id":{"type":"string"},"note":{"type":"string"}}, ["memory_id","status"]), "annotations":{"readOnlyHint":False}},
    {"name":"search_alias_set","description":"Set deterministic project vocabulary aliases used to expand lexical queries without embeddings.","inputSchema":obj({"project_id":{"type":"string"},"term":{"type":"string"},"aliases":{"type":"array","items":{"type":"string"}}},["project_id","term","aliases"]),"annotations":{"readOnlyHint":False}},
    {"name":"search_alias_list","description":"List deterministic query aliases for a project.","inputSchema":obj({"project_id":{"type":"string"}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name":"relation_create","description":"Create a verified relation between two memories without changing the evidence ledger.","inputSchema":obj({"project_id":{"type":"string"},"from_memory_id":{"type":"string"},"to_memory_id":{"type":"string"},"relation":{"type":"string","enum":["supports","depends_on","related_to"]},"note":{"type":"string"}},["project_id","from_memory_id","to_memory_id","relation"]),"annotations":{"readOnlyHint":False}},
    {"name":"graph_traverse","description":"Traverse verified memory relations up to five hops; active/disputed nodes are returned by default.","inputSchema":obj({"project_id":{"type":"string"},"memory_id":{"type":"string"},"max_depth":{"type":"integer","minimum":1,"maximum":5},"direction":{"type":"string","enum":["outgoing","incoming","both"]},"relations":{"type":"array","items":{"type":"string"}},"statuses":{"type":"array","items":{"type":"string"}}},["project_id","memory_id"]),"annotations":{"readOnlyHint":True}},
    {"name": "memory_search", "description": "Rank memories locally by default, or across the shared project registry when discover_projects is true.", "inputSchema": obj({"project_id":{"type":"string"},"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":100},"statuses":{"type":"array","items":{"type":"string"}},"scope_id":{"type":"string"},"discover_projects":{"type":"boolean"}}, ["project_id","query"]), "annotations":{"readOnlyHint":True}},
    {"name":"memory_feedback","description":"Record whether a retrieved memory was used, helpful, or incorrect so personal ranking can improve.","inputSchema":obj({"memory_id":{"type":"string"},"signal":{"type":"string","enum":["retrieved","used","helpful","incorrect"]}},["memory_id","signal"]),"annotations":{"readOnlyHint":False}},
    {"name":"review_queue","description":"List evidence-backed proposed memories and possible conflicts awaiting review.","inputSchema":obj({"project_id":{"type":"string"}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name":"memory_correct","description":"Create an evidence-backed proposed correction for review.","inputSchema":obj({"project_id":{"type":"string"},"memory_id":{"type":"string"},"content":{"type":"string"},"title":{"type":"string"}},["project_id","memory_id","content"]),"annotations":{"readOnlyHint":False}},
    {"name":"review_action","description":"Approve/reject a candidate or use it to supersede/dispute an existing memory.","inputSchema":obj({"memory_id":{"type":"string"},"action":{"type":"string","enum":["approve","reject","supersede","dispute"]},"related_memory_id":{"type":"string"},"note":{"type":"string"}},["memory_id","action"]),"annotations":{"readOnlyHint":False}},
    {"name": "get_context", "description": "Retrieve bounded, provenance-preserving context; compact format removes duplicate serialization without rewriting memories.", "inputSchema": obj({"project_id":{"type":"string"},"query":{"type":"string"},"char_budget":{"type":"integer","minimum":0,"maximum":100000},"statuses":{"type":"array","items":{"type":"string"}},"scope_id":{"type":"string"},"event_cursor":{"type":"integer","minimum":0},"event_kinds":{"type":"array","items":{"type":"string"}},"event_limit":{"type":"integer","minimum":1,"maximum":1000},"event_char_budget":{"type":"integer","minimum":0,"maximum":4000},"discover_projects":{"type":"boolean"},"response_format":{"type":"string","enum":["legacy","compact"]}}, ["project_id","query"]), "annotations":{"readOnlyHint":True}},
    {"name":"policy_get","description":"Read bounded context, retention, and checkpoint trigger policy for a project.","inputSchema":obj({"project_id":{"type":"string"}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name":"policy_set","description":"Set project operational bounds, checkpoint triggers, and scheduled-maintenance interval.","inputSchema":obj({"project_id":{"type":"string"},"max_context_chars":{"type":"integer","minimum":1000,"maximum":20000},"max_context_items":{"type":"integer","minimum":1,"maximum":50},"audit_keep_entries":{"type":"integer","minimum":100,"maximum":100000},"terminal_memory_days":{"type":"integer","minimum":1,"maximum":3650},"checkpoint_soft_usage":{"type":"number","minimum":0,"maximum":1},"checkpoint_hard_usage":{"type":"number","minimum":0,"maximum":1},"checkpoint_elapsed_seconds":{"type":"integer","minimum":60,"maximum":86400},"checkpoint_event_count":{"type":"integer","minimum":1,"maximum":10000},"checkpoint_max_age_seconds":{"type":"integer","minimum":60,"maximum":604800},"checkpoint_cooldown_seconds":{"type":"integer","minimum":0,"maximum":86400},"checkpoint_hysteresis":{"type":"number","minimum":0,"maximum":0.5},"maintenance_interval_seconds":{"type":"integer","minimum":0,"maximum":2592000}},["project_id"]),"annotations":{"readOnlyHint":False}},
    {"name":"search_health","description":"Check that every authoritative memory has exactly one FTS projection and report missing, duplicate, or orphan rows.","inputSchema":obj({"project_id":{"type":"string"}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name":"maintenance_status","description":"Report policy, live/terminal counts, audit checkpoints, and search projection health.","inputSchema":obj({"project_id":{"type":"string"}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name":"maintenance_run","description":"Plan or apply policy-based terminal-memory purge and checkpointed audit compaction. Raw source events are never deleted.","inputSchema":obj({"project_id":{"type":"string"},"apply":{"type":"boolean"}},["project_id"]),"annotations":{"readOnlyHint":False,"destructiveHint":True}},
    {"name":"backup_create","description":"Create an integrity-checked single-file SQLite snapshot with the Online Backup API, including committed WAL data.","inputSchema":obj({"output_path":{"type":"string"}},["output_path"]),"annotations":{"readOnlyHint":False}},
    {"name": "get_source", "description": "Retrieve the immutable raw event behind a memory citation.", "inputSchema": obj({"event_id":{"type":"string"}}, ["event_id"]), "annotations":{"readOnlyHint":True}},
    {"name": "get_audit", "description": "Read append-only history for an event, memory, session, project, or scope.", "inputSchema": obj({"entity_type":{"type":"string"},"entity_id":{"type":"string"}}, ["entity_type","entity_id"]), "annotations":{"readOnlyHint":True}},
]

TOOL_PAGE_SIZE = 10
TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}
CORE_TOOL_NAMES = {"context_bootstrap","project_resolve","session_start","session_end","record_event","read_events_since",
                   "memory_upsert","memory_transition","memory_search","memory_feedback","get_context","get_source",
                   "checkpoint_create","checkpoint_evaluate","review_queue","review_action"}


def validate_json(value: Any, schema: dict[str, Any], path: str = "arguments") -> None:
    """Validate the small JSON Schema subset used by MCP tool declarations."""
    expected = schema.get("type")
    matches = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
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
        missing = [name for name in schema.get("required", []) if name not in value]
        if missing:
            raise ValueError(f"{path} missing required properties: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ValueError(f"{path} has unknown properties: {', '.join(extra)}")
        for name, item in value.items():
            if name in properties:
                validate_json(item, properties[name], f"{path}.{name}")


def tool_page(cursor: Any = None, tools: list[dict[str, Any]] = TOOLS) -> dict[str, Any]:
    if cursor is None:
        offset = 0
    elif not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit():
        raise ValueError("params.cursor must be an opaque cursor returned by tools/list")
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
    """HTTPServer without a startup-time reverse DNS lookup for its bind address."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class MCPServer:
    def __init__(self, store: MemoryStore, tool_profile: str = "all"):
        if tool_profile not in {"core", "admin", "all"}: raise ValueError("tool_profile must be core, admin, or all")
        self.store = store; self.tool_profile = tool_profile
        self.tools = TOOLS if tool_profile == "all" else [t for t in TOOLS if (t["name"] in CORE_TOOL_NAMES) == (tool_profile == "core")]

    def bootstrap(self, cwd: str, query: str, client: str = "codex", external_id: str | None = None,
                  metadata: dict | None = None, **context_args: Any) -> dict[str, Any]:
        resolved = self.store.resolve_project(cwd)
        session = self.store.start_session(resolved["project"]["id"], client, resolved["scope_id"], external_id, metadata)
        context = self.store.get_context(resolved["project"]["id"], query, scope_id=resolved["scope_id"], **context_args)
        return {"project":resolved["project"],"scope_id":resolved["scope_id"],"created":resolved["created"],
                "session":session,"context":context}

    def call(self, name: str, a: dict[str, Any]) -> Any:
        mapping = {
            "context_bootstrap":self.bootstrap,
            "project_create": self.store.create_project, "project_list": self.store.list_projects, "project_resolve": self.store.resolve_project,
            "project_alias_list":self.store.list_project_aliases,
            "scope_create": self.store.create_scope, "session_start": self.store.start_session,
            "session_end": self.store.end_session, "record_event": self.store.record_event,
            "checkpoint_create":self.store.create_checkpoint, "checkpoint_evaluate":self.store.evaluate_checkpoint, "read_events_since":self.store.read_events_since,
            "memory_upsert": self.store.upsert_memory, "memory_transition": self.store.transition,
            "search_alias_set":self.store.set_search_aliases,"search_alias_list":self.store.list_search_aliases,
            "relation_create":self.store.create_relation,"graph_traverse":self.store.traverse,
            "memory_search": self.store.search, "memory_feedback":self.store.record_memory_feedback, "get_context": self.store.get_context,
            "review_queue":self.store.review_queue,"memory_correct":self.store.propose_correction,"review_action":self.store.review_candidate,
            "policy_get":self.store.get_policy,"policy_set":self.store.set_policy,"search_health":self.store.search_health,
            "maintenance_status":self.store.maintenance_status,"maintenance_run":self.store.maintain,"backup_create":self.store.backup_to,
            "get_source": self.store.get_source, "get_audit": self.store.audit,
        }
        if name not in {tool["name"] for tool in self.tools}: raise KeyError(f"tool is not exposed by {self.tool_profile} profile: {name}")
        if name not in mapping: raise KeyError(f"unknown tool: {name}")
        validate_json(a, TOOL_BY_NAME[name]["inputSchema"])
        return mapping[name](**a)

    def handle(self, req: dict[str, Any]) -> dict[str, Any] | None:
        if "id" not in req: return None
        rid, method = req.get("id"), req.get("method")
        try:
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL, "capabilities":{"tools":{"listChanged":False}},
                          "serverInfo":{"name":"context-memory","version":__version__}, "instructions":INSTRUCTIONS}
            elif method == "ping": result = {}
            elif method == "tools/list":
                params = req.get("params", {})
                if not isinstance(params, dict): raise ValueError("params must be object")
                extra = sorted(set(params) - {"cursor", "_meta"})
                if extra: raise ValueError(f"params has unknown properties: {', '.join(extra)}")
                result = tool_page(params.get("cursor"), self.tools)
            elif method == "tools/call":
                p = req.get("params", {})
                if not isinstance(p, dict): raise ValueError("params must be object")
                extra = sorted(set(p) - {"name", "arguments", "_meta"})
                if extra: raise ValueError(f"params has unknown properties: {', '.join(extra)}")
                if not isinstance(p.get("name"), str): raise ValueError("params.name must be string")
                arguments = p.get("arguments", {})
                if not isinstance(arguments, dict): raise ValueError("params.arguments must be object")
                value = self.call(p["name"], arguments)
                result = {"content":[{"type":"text","text":json.dumps(value, ensure_ascii=False, indent=2)}], "structuredContent":{"result":value}}
            else: return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":"Method not found"}}
            return {"jsonrpc":"2.0","id":rid,"result":result}
        except (ValueError, KeyError, TypeError) as exc:
            return {"jsonrpc":"2.0","id":rid,"error":{"code":-32602,"message":str(exc)}}
        except Exception as exc:
            return {"jsonrpc":"2.0","id":rid,"error":{"code":-32603,"message":f"internal error: {exc}"}}

    def serve_stdio(self) -> None:
        for line in sys.stdin:
            try:
                response = self.handle(json.loads(line))
                if response is not None:
                    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"); sys.stdout.flush()
            except json.JSONDecodeError as exc:
                sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":str(exc)}}) + "\n"); sys.stdout.flush()

    def serve_http(self, host: str, port: int, token: str | None = None) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"} and not token:
            raise ValueError("refusing external bind without --token or CONTEXT_MEMORY_TOKEN")
        server = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/mcp": self.send_error(404); return
                if token and self.headers.get("Authorization") != f"Bearer {token}": self.send_error(401); return
                try:
                    length = int(self.headers.get("Content-Length", "0")); request = json.loads(self.rfile.read(length)); response = server.handle(request)
                except Exception as exc:
                    self.send_error(400, str(exc)); return
                if response is None: self.send_response(202); self.end_headers(); return
                body = json.dumps(response, ensure_ascii=False).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, fmt: str, *args: Any) -> None: print("context-memory http: " + fmt % args, file=sys.stderr)
        LocalThreadingHTTPServer((host, port), Handler).serve_forever()
