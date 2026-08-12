import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.analyze_codex_tokens import analyze, analyze_manifest, summarize
from benchmarks.run_codex_token_experiment import create_snapshot, toml_string, WORKFLOWS


class TokenMeasurementTests(unittest.TestCase):
    def test_experiment_snapshot_is_frozen_and_synthetic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / "snapshot.db"
            digest = create_snapshot(snapshot, root / "workspace")
            self.assertEqual(digest, __import__("hashlib").sha256(snapshot.read_bytes()).hexdigest())
            self.assertEqual(len(digest), 64)
        self.assertEqual(toml_string("a b"), '"a b"')
        self.assertIn("exact order", WORKFLOWS["legacy"])

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
        self.assertEqual(report["session_summary"]["bootstrap"]["items"][0]["input_tokens"], 1200)
        self.assertEqual(report["controlled_summary"]["bootstrap"]["sessions"], 1)

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

    def test_sums_multi_turn_workflow_per_session(self):
        rows = [
            {"type":"response_item","payload":{"type":"function_call","name":"mcp__context_memory__project_resolve"}},
            {"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":100,"cached_input_tokens":80}}}},
            {"type":"response_item","payload":{"type":"function_call","name":"mcp__context_memory__session_start"}},
            {"type":"response_item","payload":{"type":"function_call","name":"mcp__context_memory__get_context"}},
            {"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":150,"cached_input_tokens":100}}}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            report = summarize([analyze(path)])
        item = report["session_summary"]["legacy"]["items"][0]
        self.assertEqual(item["model_turns"], 2)
        self.assertEqual(item["input_tokens"], 250)
        self.assertEqual(item["uncached_input_tokens"], 70)

    def test_controlled_summary_requires_exact_single_workflow_sequence(self):
        bootstrap = {"session":"bootstrap.jsonl", "observations":[{
            "tools_since_previous_model_turn":["context_bootstrap"], "workflow":"bootstrap",
            "input_tokens":120, "cached_input_tokens":100, "uncached_input_tokens":20,
        }]}
        legacy = {"session":"legacy.jsonl", "observations":[
            {"tools_since_previous_model_turn":["project_resolve"], "workflow":"legacy",
             "input_tokens":100, "cached_input_tokens":90, "uncached_input_tokens":10},
            {"tools_since_previous_model_turn":["session_start", "get_context"], "workflow":"legacy",
             "input_tokens":110, "cached_input_tokens":90, "uncached_input_tokens":20},
        ]}
        mixed = {"session":"mixed.jsonl", "observations":bootstrap["observations"] + legacy["observations"]}
        report = summarize([bootstrap, legacy, mixed])
        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(report["controlled_summary"]["bootstrap"]["sessions"], 1)
        self.assertEqual(report["controlled_summary"]["legacy"]["sessions"], 1)
        comparison = report["controlled_summary"]["comparison"]
        self.assertEqual(comparison["input_tokens_median_delta"], -90)
        self.assertEqual(comparison["cached_input_tokens_median_delta"], -80)
        self.assertEqual(comparison["uncached_input_tokens_median_delta"], -10)
        self.assertEqual(comparison["uncached_input_tokens_median_change_percent"], -33.3)

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

    def test_extracts_completed_mcp_call_from_dynamic_exec(self):
        rows = [
            {"type":"response_item","payload":{"type":"custom_tool_call","name":"exec",
             "input":"const fn = tools[selected]; await fn(args);"}},
            {"type":"event_msg","payload":{"type":"mcp_tool_call_end","invocation":{
             "server":"context_memory", "tool":"context_bootstrap", "arguments":{}}}},
            {"type":"event_msg","payload":{"type":"token_count","info":{
             "last_token_usage":{"input_tokens":100,"cached_input_tokens":80}}}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dynamic.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            report = analyze(path)
        self.assertEqual(report["observations"][0]["tools_since_previous_model_turn"],
                         ["context_bootstrap"])
        self.assertEqual(report["observations"][0]["uncached_input_tokens"], 20)

    def test_manifest_reports_paired_distributions_by_cache_stratum(self):
        def rows(names, total, cached):
            result = [{"type":"response_item", "payload":{"type":"function_call",
                       "name":f"mcp__context_memory__{name}"}} for name in names]
            result.append({"type":"event_msg", "payload":{"type":"token_count", "info":{
                "last_token_usage":{"input_tokens":total, "cached_input_tokens":cached}}}})
            return result
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = []
            for pair, stratum, bootstrap, legacy in (
                ("cold-1", "cold", (120, 20), (150, 30)),
                ("cold-2", "cold", (110, 10), (160, 40)),
                ("warm-1", "warm", (100, 80), (130, 100)),
            ):
                for workflow, values, names in (
                    ("bootstrap", bootstrap, ["context_bootstrap"]),
                    ("legacy", legacy, ["project_resolve", "session_start", "get_context"]),
                ):
                    session = f"{pair}-{workflow}.jsonl"
                    (root / session).write_text("".join(json.dumps(row) + "\n" for row in rows(names, *values)), encoding="utf-8")
                    runs.append({"pair":pair, "stratum":stratum, "workflow":workflow, "session":session})
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"snapshot_sha256":"a" * 64, "runs":runs}), encoding="utf-8")
            reports, experiment = analyze_manifest(manifest)
            report = summarize(reports)
        self.assertEqual(experiment["snapshot_sha256"], "a" * 64)
        self.assertEqual(report["paired_summary"]["cold"]["pairs"], 2)
        self.assertEqual(report["paired_summary"]["cold"]["input_tokens_delta_median"], -40)
        self.assertEqual(report["paired_summary"]["warm"]["uncached_input_tokens_delta_median"], -10)

    def test_manifest_rejects_incomplete_pair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / "bootstrap.jsonl"
            session.write_text(json.dumps({"type":"response_item", "payload":{"type":"function_call",
                "name":"mcp__context_memory__context_bootstrap"}}) + "\n" +
                json.dumps({"type":"event_msg", "payload":{"type":"token_count", "info":{
                    "last_token_usage":{"input_tokens":1}}}}) + "\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"snapshot_sha256":"b" * 64, "runs":[{
                "pair":"p1", "stratum":"cold", "workflow":"bootstrap", "session":session.name}]}), encoding="utf-8")
            reports, _ = analyze_manifest(manifest)
            with self.assertRaisesRegex(ValueError, "exactly one bootstrap"):
                summarize(reports)

    def test_completed_mcp_event_does_not_duplicate_literal_exec_call(self):
        rows = [
            {"type":"response_item","payload":{"type":"custom_tool_call","name":"exec",
             "input":"await tools.mcp__context_memory__context_bootstrap(args);"}},
            {"type":"event_msg","payload":{"type":"mcp_tool_call_end","invocation":{
             "server":"context_memory", "tool":"context_bootstrap", "arguments":{}}}},
            {"type":"event_msg","payload":{"type":"token_count","info":{
             "last_token_usage":{"input_tokens":100,"cached_input_tokens":80}}}},
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "literal.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            report = analyze(path)
        self.assertEqual(report["observations"][0]["tools_since_previous_model_turn"],
                         ["context_bootstrap"])


if __name__ == "__main__":
    unittest.main()
