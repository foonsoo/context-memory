import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.run_decision_evaluation import load_fixture, run


class DecisionEvaluationTests(unittest.TestCase):
    def test_frozen_scenarios_cover_the_p0_failure_modes(self):
        fixture = load_fixture()
        self.assertEqual(fixture["schema_version"], 1)
        self.assertEqual({scenario["id"] for scenario in fixture["scenarios"]}, {
            "changed-decision", "conflicting-evidence", "rejected-alternative",
            "outcome-feedback", "missing-rationale", "stale-external-source",
        })

    def test_decision_brief_meets_synthetic_exit_gate(self):
        result = run(repeats=1)
        metrics = result["metrics"]
        self.assertEqual(metrics["current_decision_accuracy"], 1.0, result["scenarios"])
        self.assertEqual(metrics["stale_decision_leakage"], 0.0, result["scenarios"])
        self.assertEqual(metrics["unsupported_claim_rate"], 0.0, result["scenarios"])
        self.assertEqual(metrics["source_recovery_rate"], 1.0, result["scenarios"])
        self.assertEqual(metrics["useful_history_recall"], 1.0, result["scenarios"])
        self.assertTrue(all(all(item["section_checks"].values()) for item in result["scenarios"]), result["scenarios"])
        self.assertGreater(metrics["context_chars"]["median"], 0)
        self.assertGreaterEqual(metrics["latency_ms"]["p95"], metrics["latency_ms"]["p50"])

    def test_fixture_rejects_unknown_expected_keys(self):
        fixture = {"schema_version": 1, "contract_version": "decision-brief/v1", "scenarios": [{
            "id": "bad", "question": "Question", "memories": [
                {"key": "known", "title": "Title", "content": "Content", "type": "decision", "status": "active"}
            ], "expected": {"current_decisions": ["missing"]},
        }]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown keys"):
                load_fixture(path)


if __name__ == "__main__":
    unittest.main()
