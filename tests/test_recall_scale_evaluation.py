import unittest

from benchmarks.run_recall_scale_evaluation import run


class RecallScaleEvaluationTests(unittest.TestCase):
    def test_large_repository_gate_recovers_target_within_bounds(self):
        result = run(repeats=2, distractors=1000)

        self.assertEqual(result["contract_version"], "context-recall-scale/v1")
        self.assertTrue(result["fixture"]["target_is_after_bulk_lexically"])
        self.assertEqual(result["metrics"]["target_recovery"], 1.0)
        self.assertLessEqual(
            result["metrics"]["max_files_read"],
            result["limits"]["max_files"],
        )
        self.assertGreaterEqual(
            result["metrics"]["latency_ms"]["p95"],
            result["metrics"]["latency_ms"]["p50"],
        )

    def test_scale_gate_rejects_tiny_unrepresentative_fixture(self):
        with self.assertRaisesRegex(ValueError, "at least 1000"):
            run(repeats=1, distractors=999)


if __name__ == "__main__":
    unittest.main()
