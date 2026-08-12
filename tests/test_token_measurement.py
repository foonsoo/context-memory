import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.analyze_codex_tokens import analyze, summarize


class TokenMeasurementTests(unittest.TestCase):
    def test_extracts_counters_without_session_content(self):
        rows = [
            {"type":"response_item","payload":{"type":"function_call","name":"mcp__context_memory__context_bootstrap","arguments":"SECRET"}},
            {"type":"response_item","payload":{"type":"function_call_output","output":"PRIVATE"}},
            {"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":1200,"cached_input_tokens":900}}}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            report = summarize([analyze(path)])
        rendered = json.dumps(report)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("PRIVATE", rendered)
        self.assertEqual(report["summary"]["bootstrap"]["uncached_input_tokens_min"], 300)

    def test_groups_legacy_calls_before_next_model_turn(self):
        rows = []
        for name in ("project_resolve", "session_start", "get_context"):
            rows.append({"type":"response_item","payload":{"type":"function_call","name":f"mcp__context_memory__{name}"}})
        rows.append({"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":2000}}}})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            report = analyze(path)
        self.assertEqual(report["observations"][0]["workflow"], "legacy")
        self.assertEqual(report["observations"][0]["tools_since_previous_model_turn"],
                         ["project_resolve", "session_start", "get_context"])

    def test_extracts_calls_orchestrated_inside_exec(self):
        rows = [
            {"type":"response_item","payload":{"type":"custom_tool_call","name":"exec",
             "input":"await tools.mcp__context_memory__context_bootstrap({cwd: 'private'})"}},
            {"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":42}}}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            report = analyze(path)
        self.assertEqual(report["observations"][0]["workflow"], "bootstrap")


if __name__ == "__main__":
    unittest.main()
