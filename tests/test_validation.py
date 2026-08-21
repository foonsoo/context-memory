import pytest

from context_memory.validation import normalize_test_results


def test_normalize_test_results_trims_optional_fields():
    assert normalize_test_results(
        [
            {
                "name": " unit tests ",
                "status": "passed",
                "command": " pytest -q ",
                "details": " 139 passed ",
            },
            {"name": "lint", "status": "skipped"},
        ]
    ) == [
        {
            "name": "unit tests",
            "status": "passed",
            "command": "pytest -q",
            "details": "139 passed",
        },
        {"name": "lint", "status": "skipped"},
    ]


@pytest.mark.parametrize(
    ("results", "message"),
    [
        ([{"name": "tests", "status": "passed", "extra": True}], "objects"),
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
    ],
)
def test_normalize_test_results_rejects_invalid_values(results, message):
    with pytest.raises(ValueError, match=message):
        normalize_test_results(results)
