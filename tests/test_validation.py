import unittest

from context_memory.validation import normalize_test_results


class ValidationTests(unittest.TestCase):
    def test_normalize_test_results_trims_optional_fields(self):
        self.assertEqual(
            normalize_test_results(
                [
                    {
                        "name": " unit tests ",
                        "status": "passed",
                        "command": " pytest -q ",
                        "details": " 139 passed ",
                    },
                    {"name": "lint", "status": "skipped"},
                ]
            ),
            [
                {
                    "name": "unit tests",
                    "status": "passed",
                    "command": "pytest -q",
                    "details": "139 passed",
                },
                {"name": "lint", "status": "skipped"},
            ],
        )

    def test_normalize_test_results_rejects_invalid_values(self):
        cases = [
            (
                [{"name": "tests", "status": "passed", "extra": True}],
                "objects",
            ),
            ([{"name": " ", "status": "passed"}], "name cannot be empty"),
            ([{"name": "tests", "status": "unknown"}], "status must be"),
            (
                [{"name": "tests", "status": "failed", "command": " "}],
                "command cannot be empty",
            ),
            (
                [{"name": "tests", "status": "failed", "details": 1}],
                "details cannot be empty",
            ),
        ]
        for results, message in cases:
            with self.subTest(results=results):
                with self.assertRaisesRegex(ValueError, message):
                    normalize_test_results(results)
