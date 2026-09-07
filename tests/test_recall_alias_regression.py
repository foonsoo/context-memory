import unittest

from benchmarks.run_recall_alias_regression import run


class RecallAliasRegressionTests(unittest.TestCase):
    def test_project_alias_regression_is_isolated_and_reports_all_modes(self):
        result = run()
        self.assertEqual(
            result["dataset"], "synthetic-regression-not-independent-evaluation"
        )
        self.assertEqual(
            set(result["modes"]),
            {"legacy-global", "default", "project-configured"},
        )
        configured = result["modes"]["project-configured"]["outcomes"]
        cases = {item["case"]: item for item in configured}
        self.assertTrue(cases["mixed-project-alias"]["passed"])
        self.assertTrue(cases["other-project-only"]["passed"])
        self.assertTrue(cases["stale-decision"]["passed"])


if __name__ == "__main__":
    unittest.main()
