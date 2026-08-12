import json
import tempfile
import unittest
from pathlib import Path

from context_memory.mcp import CORE_TOOL_NAMES, MCPServer, TOOLS
from context_memory.store import MemoryStore


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.store = MemoryStore(Path(self.temp.name) / "memory.db"); self.server = MCPServer(self.store)
    def tearDown(self): self.store.close(); self.temp.cleanup()

    def test_initialize_list_and_tool_call(self):
        init = self.server.handle({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}})
        self.assertEqual(init["result"]["capabilities"]["tools"], {"listChanged": False})
        tool_names, cursor = [], None
        while True:
            params = {"cursor": cursor} if cursor is not None else {}
            page = self.server.handle({"jsonrpc":"2.0","id":2,"method":"tools/list","params":params})["result"]
            tool_names.extend(tool["name"] for tool in page["tools"])
            cursor = page.get("nextCursor")
            if cursor is None: break
        self.assertEqual(tool_names, [tool["name"] for tool in TOOLS])
        created = self.server.handle({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"project_create","arguments":{"slug":"mcp-demo"}}})
        value = created["result"]["structuredContent"]["result"]
        self.assertEqual(value["slug"], "mcp-demo"); self.assertEqual(json.loads(created["result"]["content"][0]["text"])["id"], value["id"])
        recorded = self.server.handle({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"record_event","arguments":{"project_id":value["id"],"kind":"message","content":"hello next session"}}})
        event = recorded["result"]["structuredContent"]["result"]
        polled = self.server.handle({"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"read_events_since","arguments":{"project_id":value["id"],"cursor":0,"kinds":["message"]}}})
        stream = polled["result"]["structuredContent"]["result"]
        self.assertEqual(stream["events"][0]["id"], event["id"]); self.assertEqual(stream["next_cursor"], event["event_seq"])
        durable = self.server.call("event_poll", {"project_id":value["id"],"consumer_id":"mcp-test","kinds":["message"]})
        self.assertEqual(durable["events"][0]["id"], event["id"])
        receipt = self.server.call("event_ack", {"project_id":value["id"],"consumer_id":"mcp-test","cursor":durable["next_cursor"],"kinds":["message"]})
        self.assertEqual(receipt["acknowledged_cursor"], event["event_seq"])
        checkpointed = self.server.handle({"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"checkpoint_create","arguments":{
            "project_id":value["id"],"mode":"interim","reason":"manual","goal":"Resume MCP test","idempotency_key":"mcp-checkpoint",
            "test_results":[{"name":"MCP test","status":"passed"}]
        }}})
        checkpoint = checkpointed["result"]["structuredContent"]["result"]
        self.assertEqual(checkpoint["mode"], "interim")
        self.assertEqual(checkpoint["objective"]["test_results"][0]["status"], "passed")
        evaluated = self.server.handle({"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"checkpoint_evaluate","arguments":{
            "project_id":value["id"],"context_usage":.9,"goal":"Resume MCP test"
        }}})["result"]["structuredContent"]["result"]
        self.assertFalse(evaluated["should_checkpoint"])
        self.assertEqual(evaluated["suppression"], "unchanged_recovery_state")
        self.assertTrue(evaluated["suggested_idempotency_key"].startswith("checkpoint:"))

    def test_notification_has_no_response(self):
        self.assertIsNone(self.server.handle({"jsonrpc":"2.0","method":"notifications/initialized"}))

    def test_bootstrap_combines_startup_and_is_idempotent(self):
        response = self.server.handle({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"context_bootstrap","arguments":{
            "cwd":self.temp.name,"query":"next work","client":"test","external_id":"same","response_format":"compact"
        }}})
        first = response["result"]["structuredContent"]["result"]
        second = self.server.call("context_bootstrap", {"cwd":self.temp.name,"query":"next work","client":"test","external_id":"same","response_format":"compact"})
        self.assertEqual(first["session"]["id"], second["session"]["id"])
        self.assertEqual(first["project"]["id"], second["project"]["id"])
        self.assertEqual(first["context"]["response_format"], "compact")

    def test_tool_profiles_split_working_and_administrative_surfaces(self):
        core = MCPServer(self.store, "core")
        admin = MCPServer(self.store, "admin")
        core_names = {tool["name"] for tool in core.tools}
        admin_names = {tool["name"] for tool in admin.tools}
        self.assertEqual(core_names, CORE_TOOL_NAMES)
        self.assertFalse(core_names & admin_names)
        self.assertEqual(core_names | admin_names, {tool["name"] for tool in TOOLS})
        denied = admin.handle({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_context","arguments":{"project_id":"x","query":"q"}}})
        self.assertEqual(denied["error"]["code"], -32602)

    def test_external_http_requires_token(self):
        with self.assertRaisesRegex(ValueError, "refusing external bind"): self.server.serve_http("0.0.0.0", 0)

    def test_tools_list_rejects_invalid_cursor(self):
        response = self.server.handle({"jsonrpc":"2.0", "id":1, "method":"tools/list", "params":{"cursor":"not-a-cursor"}})
        self.assertEqual(response["error"]["code"], -32602)
        non_object = self.server.handle({"jsonrpc":"2.0", "id":2, "method":"tools/list", "params":[]})
        self.assertEqual(non_object["error"]["code"], -32602)

    def test_reserved_request_metadata_is_accepted_and_ignored(self):
        listed = self.server.handle({"jsonrpc":"2.0", "id":1, "method":"tools/list", "params":{"_meta":{"progressToken":"sdk"}}})
        self.assertIn("tools", listed["result"])
        called = self.server.handle({"jsonrpc":"2.0", "id":2, "method":"tools/call", "params":{
            "name":"project_resolve", "arguments":{"cwd":"/tmp"}, "_meta":{"progressToken":"sdk"},
        }})
        self.assertNotIn("error", called)

    def test_tool_arguments_are_validated_against_declared_schema(self):
        cases = [
            {"name":"project_resolve", "arguments":{}},
            {"name":"project_resolve", "arguments":{"cwd":"/tmp", "unexpected":True}},
            {"name":"read_events_since", "arguments":{"project_id":"p", "limit":0}},
            {"name":"memory_upsert", "arguments":{"project_id":"p", "title":"t", "content":"c", "confidence":True}},
            {"name":"memory_upsert", "arguments":{"project_id":"p", "title":"t", "content":"c", "memory_type":"unknown"}},
            {"name":"checkpoint_create", "arguments":{"project_id":"p", "mode":"unknown", "reason":"manual", "goal":"g", "idempotency_key":"k"}},
            {"name":"search_alias_set", "arguments":{"project_id":"p", "term":"db", "aliases":["database", 7]}},
            {"name":"project_resolve", "arguments":[]},
        ]
        for index, params in enumerate(cases, 1):
            with self.subTest(params=params):
                response = self.server.handle({"jsonrpc":"2.0", "id":index, "method":"tools/call", "params":params})
                self.assertEqual(response["error"]["code"], -32602)
        valid = self.server.handle({"jsonrpc":"2.0", "id":100, "method":"tools/call", "params":{"name":"project_resolve", "arguments":{"cwd":"/tmp"}}})
        self.assertNotIn("error", valid)


if __name__ == "__main__": unittest.main()
