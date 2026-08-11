import json
import tempfile
import unittest
from pathlib import Path

from context_memory.mcp import MCPServer
from context_memory.store import MemoryStore


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.store = MemoryStore(Path(self.temp.name) / "memory.db"); self.server = MCPServer(self.store)
    def tearDown(self): self.store.close(); self.temp.cleanup()

    def test_initialize_list_and_tool_call(self):
        init = self.server.handle({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}})
        self.assertEqual(init["result"]["capabilities"]["tools"], {"listChanged": False})
        tools = self.server.handle({"jsonrpc":"2.0","id":2,"method":"tools/list"})
        self.assertTrue({"record_event", "get_context", "get_source", "memory_transition", "graph_traverse", "search_alias_set",
                         "policy_set", "maintenance_run", "search_health", "backup_create"} <= {t["name"] for t in tools["result"]["tools"]})
        created = self.server.handle({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"project_create","arguments":{"slug":"mcp-demo"}}})
        value = created["result"]["structuredContent"]["result"]
        self.assertEqual(value["slug"], "mcp-demo"); self.assertEqual(json.loads(created["result"]["content"][0]["text"])["id"], value["id"])

    def test_notification_has_no_response(self):
        self.assertIsNone(self.server.handle({"jsonrpc":"2.0","method":"notifications/initialized"}))

    def test_external_http_requires_token(self):
        with self.assertRaisesRegex(ValueError, "refusing external bind"): self.server.serve_http("0.0.0.0", 0)


if __name__ == "__main__": unittest.main()
