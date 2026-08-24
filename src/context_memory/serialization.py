"""Shared deterministic serialization primitives."""

import json
from typing import Any


def canonical(value: Any) -> str:
    """Serialize with the stable Context Memory JSON contract."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
