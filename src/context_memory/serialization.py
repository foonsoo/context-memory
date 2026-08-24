"""Shared deterministic serialization primitives."""

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    """Serialize with the stable Context Memory JSON contract."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def canonical_digest(value: Any) -> str:
    """Hash a value using the stable Context Memory JSON contract."""
    return hashlib.sha256(canonical(value).encode()).hexdigest()
