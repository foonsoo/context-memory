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
INSTRUCTIONS = ("Use get_context at task start with a focused query. Preserve evidence with record_event before proposing memories. "
                "New inferred memories should remain proposed until verified; use active only for confirmed facts/decisions. "
                "Retrieve original evidence with get_source. Record consequential decisions during work and end the session when done.")


def obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


TOOLS = [
    {"name": "project_create", "description": "Create a local memory project.", "inputSchema": obj({"slug":{"type":"string"},"name":{"type":"string"},"description":{"type":"string"},"idempotency_key":{"type":"string"}}, ["slug"]), "annotations":{"readOnlyHint":False}},
    {"name": "project_list", "description": "List local memory projects.", "inputSchema": obj({}), "annotations":{"readOnlyHint":True}},
    {"name": "project_resolve", "description": "Resolve or create the memory project for a canonical agent workspace folder.", "inputSchema": obj({"cwd":{"type":"string"}}, ["cwd"]), "annotations":{"readOnlyHint":False}},
    {"name": "scope_create", "description": "Create a project scope, optionally tied to a path.", "inputSchema": obj({"project_id":{"type":"string"},"name":{"type":"string"},"path":{"type":"string"}}, ["project_id","name"]), "annotations":{"readOnlyHint":False}},
    {"name": "session_start", "description": "Start or resume an idempotent client session.", "inputSchema": obj({"project_id":{"type":"string"},"client":{"type":"string"},"scope_id":{"type":"string"},"external_id":{"type":"string"},"metadata":{"type":"object"}}, ["project_id"]), "annotations":{"readOnlyHint":False}},
    {"name": "session_end", "description": "Mark a session ended and audit its optional summary.", "inputSchema": obj({"session_id":{"type":"string"},"summary":{"type":"string"}}, ["session_id"]), "annotations":{"readOnlyHint":False}},
    {"name": "record_event", "description": "Append immutable raw evidence. Use kind=message for unverified inter-session coordination; messages are not promoted into ranked memory automatically. Success is returned only after durable DB commit.", "inputSchema": obj({"project_id":{"type":"string"},"kind":{"type":"string"},"content":{"type":"string"},"session_id":{"type":"string"},"scope_id":{"type":"string"},"source_uri":{"type":"string"},"metadata":{"type":"object"},"idempotency_key":{"type":"string"}}, ["project_id","kind","content"]), "annotations":{"readOnlyHint":False}},
    {"name":"read_events_since","description":"Read immutable project events after a cursor in sequence order. Omit kinds for all events; use kinds=[message] for inter-session polling.","inputSchema":obj({"project_id":{"type":"string"},"cursor":{"type":"integer","minimum":0},"kinds":{"type":"array","items":{"type":"string"}},"scope_id":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":1000}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name": "memory_upsert", "description": "Propose, activate, or update a derived memory with source event provenance and observation/confirmation time.", "inputSchema": obj({"project_id":{"type":"string"},"title":{"type":"string"},"content":{"type":"string"},"memory_type":{"type":"string","enum":["fact","decision","preference","constraint","procedure","summary","task","other"]},"status":{"type":"string","enum":["proposed","active","superseded","disputed","expired","rejected"]},"confidence":{"type":"number","minimum":0,"maximum":1},"importance":{"type":"number","minimum":0,"maximum":1},"scope_id":{"type":"string"},"source_event_ids":{"type":"array","items":{"type":"string"}},"valid_from":{"type":"string"},"valid_until":{"type":"string"},"observed_at":{"type":"string"},"last_confirmed_at":{"type":"string"},"tags":{"type":"array","items":{"type":"string"}},"memory_id":{"type":"string"},"idempotency_key":{"type":"string"}}, ["project_id","title","content"]), "annotations":{"readOnlyHint":False}},
    {"name": "memory_transition", "description": "Verify/activate, supersede, dispute, expire, or reject a memory; optionally link the challenging/replacement memory.", "inputSchema": obj({"memory_id":{"type":"string"},"status":{"type":"string","enum":["active","superseded","disputed","expired","rejected"]},"related_memory_id":{"type":"string"},"note":{"type":"string"}}, ["memory_id","status"]), "annotations":{"readOnlyHint":False}},
    {"name":"search_alias_set","description":"Set deterministic project vocabulary aliases used to expand lexical queries without embeddings.","inputSchema":obj({"project_id":{"type":"string"},"term":{"type":"string"},"aliases":{"type":"array","items":{"type":"string"}}},["project_id","term","aliases"]),"annotations":{"readOnlyHint":False}},
    {"name":"search_alias_list","description":"List deterministic query aliases for a project.","inputSchema":obj({"project_id":{"type":"string"}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name":"relation_create","description":"Create a verified relation between two memories without changing the evidence ledger.","inputSchema":obj({"project_id":{"type":"string"},"from_memory_id":{"type":"string"},"to_memory_id":{"type":"string"},"relation":{"type":"string","enum":["supports","depends_on","related_to"]},"note":{"type":"string"}},["project_id","from_memory_id","to_memory_id","relation"]),"annotations":{"readOnlyHint":False}},
    {"name":"graph_traverse","description":"Traverse verified memory relations up to five hops; active/disputed nodes are returned by default.","inputSchema":obj({"project_id":{"type":"string"},"memory_id":{"type":"string"},"max_depth":{"type":"integer","minimum":1,"maximum":5},"direction":{"type":"string","enum":["outgoing","incoming","both"]},"relations":{"type":"array","items":{"type":"string"}},"statuses":{"type":"array","items":{"type":"string"}}},["project_id","memory_id"]),"annotations":{"readOnlyHint":True}},
    {"name": "memory_search", "description": "Rank memories with FTS5 and optional on-device similarity using reciprocal-rank fusion, freshness, quality, and feedback signals.", "inputSchema": obj({"project_id":{"type":"string"},"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":100},"statuses":{"type":"array","items":{"type":"string"}},"scope_id":{"type":"string"}}, ["project_id","query"]), "annotations":{"readOnlyHint":True}},
    {"name":"memory_feedback","description":"Record whether a retrieved memory was used, helpful, or incorrect so personal ranking can improve.","inputSchema":obj({"memory_id":{"type":"string"},"signal":{"type":"string","enum":["retrieved","used","helpful","incorrect"]}},["memory_id","signal"]),"annotations":{"readOnlyHint":False}},
    {"name": "get_context", "description": "Select task-relevant active/disputed memories and optionally return cursor-based recent events in a separate bounded section. Messages never enter memory ranking automatically.", "inputSchema": obj({"project_id":{"type":"string"},"query":{"type":"string"},"char_budget":{"type":"integer","minimum":0,"maximum":100000},"statuses":{"type":"array","items":{"type":"string"}},"scope_id":{"type":"string"},"event_cursor":{"type":"integer","minimum":0},"event_kinds":{"type":"array","items":{"type":"string"}},"event_limit":{"type":"integer","minimum":1,"maximum":1000},"event_char_budget":{"type":"integer","minimum":0,"maximum":4000}}, ["project_id","query"]), "annotations":{"readOnlyHint":True}},
    {"name":"policy_get","description":"Read bounded context, audit retention, and terminal-memory retention policy for a project.","inputSchema":obj({"project_id":{"type":"string"}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name":"policy_set","description":"Set project operational bounds. Context is hard-limited to at most 20,000 characters.","inputSchema":obj({"project_id":{"type":"string"},"max_context_chars":{"type":"integer","minimum":1000,"maximum":20000},"max_context_items":{"type":"integer","minimum":1,"maximum":50},"audit_keep_entries":{"type":"integer","minimum":100,"maximum":100000},"terminal_memory_days":{"type":"integer","minimum":1,"maximum":3650}},["project_id"]),"annotations":{"readOnlyHint":False}},
    {"name":"search_health","description":"Check that every authoritative memory has exactly one FTS projection and report missing, duplicate, or orphan rows.","inputSchema":obj({"project_id":{"type":"string"}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name":"maintenance_status","description":"Report policy, live/terminal counts, audit checkpoints, and search projection health.","inputSchema":obj({"project_id":{"type":"string"}},["project_id"]),"annotations":{"readOnlyHint":True}},
    {"name":"maintenance_run","description":"Plan or apply policy-based terminal-memory purge and checkpointed audit compaction. Raw source events are never deleted.","inputSchema":obj({"project_id":{"type":"string"},"apply":{"type":"boolean"}},["project_id"]),"annotations":{"readOnlyHint":False,"destructiveHint":True}},
    {"name":"backup_create","description":"Create an integrity-checked single-file SQLite snapshot with the Online Backup API, including committed WAL data.","inputSchema":obj({"output_path":{"type":"string"}},["output_path"]),"annotations":{"readOnlyHint":False}},
    {"name": "get_source", "description": "Retrieve the immutable raw event behind a memory citation.", "inputSchema": obj({"event_id":{"type":"string"}}, ["event_id"]), "annotations":{"readOnlyHint":True}},
    {"name": "get_audit", "description": "Read append-only history for an event, memory, session, project, or scope.", "inputSchema": obj({"entity_type":{"type":"string"},"entity_id":{"type":"string"}}, ["entity_type","entity_id"]), "annotations":{"readOnlyHint":True}},
]


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    """HTTPServer without a startup-time reverse DNS lookup for its bind address."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class MCPServer:
    def __init__(self, store: MemoryStore): self.store = store

    def call(self, name: str, a: dict[str, Any]) -> Any:
        mapping = {
            "project_create": self.store.create_project, "project_list": self.store.list_projects, "project_resolve": self.store.resolve_project,
            "scope_create": self.store.create_scope, "session_start": self.store.start_session,
            "session_end": self.store.end_session, "record_event": self.store.record_event, "read_events_since":self.store.read_events_since,
            "memory_upsert": self.store.upsert_memory, "memory_transition": self.store.transition,
            "search_alias_set":self.store.set_search_aliases,"search_alias_list":self.store.list_search_aliases,
            "relation_create":self.store.create_relation,"graph_traverse":self.store.traverse,
            "memory_search": self.store.search, "memory_feedback":self.store.record_memory_feedback, "get_context": self.store.get_context,
            "policy_get":self.store.get_policy,"policy_set":self.store.set_policy,"search_health":self.store.search_health,
            "maintenance_status":self.store.maintenance_status,"maintenance_run":self.store.maintain,"backup_create":self.store.backup_to,
            "get_source": self.store.get_source, "get_audit": self.store.audit,
        }
        if name not in mapping: raise KeyError(f"unknown tool: {name}")
        return mapping[name](**a)

    def handle(self, req: dict[str, Any]) -> dict[str, Any] | None:
        if "id" not in req: return None
        rid, method = req.get("id"), req.get("method")
        try:
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL, "capabilities":{"tools":{"listChanged":False}},
                          "serverInfo":{"name":"context-memory","version":__version__}, "instructions":INSTRUCTIONS}
            elif method == "ping": result = {}
            elif method == "tools/list": result = {"tools": TOOLS}
            elif method == "tools/call":
                p = req.get("params") or {}; value = self.call(p.get("name", ""), p.get("arguments") or {})
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
