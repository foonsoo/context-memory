"""Small helpers for MCP JSON Schema declarations."""

from typing import Any


def obj(
    props: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }
