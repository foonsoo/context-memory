import unittest

from benchmarks.run_discovery_calibration import run


class DiscoveryCalibrationTests(unittest.TestCase):
    def test_multi_project_fixture_is_accurate_and_ambiguity_safe(self):
        result = run(items_per_project=8, repeats=3)
        self.assertEqual(result["accuracy"], 1.0, result["scenarios"])
        self.assertTrue(result["ambiguity_safe"])


if __name__ == "__main__":
    unittest.main()
