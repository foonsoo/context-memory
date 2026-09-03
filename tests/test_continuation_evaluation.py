import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.run_continuation_evaluation import BASELINES, load_fixture, run


class ContinuationEvaluationTests(unittest.TestCase):
    def test_fixture_freezes_twenty_four_natural_continuation_prompts(self):
        fixture = load_fixture()
        self.assertEqual(len(fixture["cases"]), 8)
        self.assertEqual(sum(len(case["prompts"]) for case in fixture["cases"]), 24)
        blog = next(case for case in fixture["cases"] if case["id"] == "blog-episode-four")
        rendered = json.dumps(blog, ensure_ascii=False)
        for expected in (
            "01-why-ai-forgets.md", "02-what-to-remember.md",
            "03-how-to-find-context.md", "6편", "일반 독자",
            "기억을 쌓았더니 의사결정 문서가 되었다", "원고가 없다",
        ):
            self.assertIn(expected, rendered)

    def test_bakeoff_reports_all_required_metrics_for_four_baselines(self):
        result = run(repeats=1)
        self.assertEqual(set(result["modes"]), set(BASELINES))
        self.assertEqual(result["fixture"]["prompts"], 24)
        required = {
            "continuation_recall", "artifact_recovery", "decision_recovery",
            "next_step_recovery", "wrong_project_rate", "false_absence_rate",
            "stale_error_leakage", "source_recovery", "returned_tokens",
            "total_llm_input_tokens", "latency_ms",
        }
        for mode in result["modes"].values():
            self.assertEqual(set(mode["metrics"]), required)
            self.assertEqual(len(mode["prompts"]), 24)
            self.assertGreaterEqual(mode["metrics"]["latency_ms"]["p95"], mode["metrics"]["latency_ms"]["p50"])
        vnext = result["modes"]["context-recall-vnext"]["metrics"]
        self.assertEqual(vnext["stale_error_leakage"], 0.0)
        # The 350-token vNext item budget excludes the fixed JSON envelope.
        self.assertLessEqual(vnext["returned_tokens"]["max"], 512)

    def test_fixture_rejects_too_few_prompts(self):
        fixture = load_fixture()
        fixture["cases"] = fixture["cases"][:1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "20..30"):
                load_fixture(path)


if __name__ == "__main__":
    unittest.main()
