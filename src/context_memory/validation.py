from __future__ import annotations

from typing import Any


def normalize_test_results(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and normalize checkpoint test results."""
    allowed = {"name", "status", "command", "details"}
    normalized = []
    for result in results:
        if not isinstance(result, dict) or set(result) - allowed:
            raise ValueError(
                "test_results must contain objects with name, status,"
                " command, and details only"
            )
        name, status = result.get("name"), result.get("status")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("test result name cannot be empty")
        if status not in {"passed", "failed", "skipped"}:
            raise ValueError(
                "test result status must be passed, failed, or skipped"
            )
        item = {"name": name.strip(), "status": status}
        for field in ("command", "details"):
            value = result.get(field)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"test result {field} cannot be empty")
                item[field] = value.strip()
        normalized.append(item)
    return normalized
