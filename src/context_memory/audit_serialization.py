"""Pure serialization and validation helpers for audit-chain bundles."""

from __future__ import annotations

import re
from typing import Any

AUDIT_CHAIN_FORMAT = "context-memory-audit-chain"
AUDIT_CHAIN_VERSION = 1


def serialize_audit_chain(
    project_id: str,
    checkpoints: list[dict[str, Any]],
    audit_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the stable offline audit-chain response contract."""
    return {
        "format": AUDIT_CHAIN_FORMAT,
        "version": AUDIT_CHAIN_VERSION,
        "project_id": project_id,
        "checkpoints": checkpoints,
        "audit_entries": audit_entries,
        "head_digest": checkpoints[-1]["digest"] if checkpoints else None,
    }


def verify_audit_chain_bundle(
    bundle: dict[str, Any], expected_head_digest: str | None = None
) -> dict[str, Any]:
    """Verify an exported chain without opening its source database."""
    errors: list[str] = []
    if (
        bundle.get("format") != AUDIT_CHAIN_FORMAT
        or bundle.get("version") != AUDIT_CHAIN_VERSION
    ):
        errors.append("unsupported audit-chain format or version")
    project_id = bundle.get("project_id")
    checkpoints = bundle.get("checkpoints")
    entries = bundle.get("audit_entries")
    if not isinstance(project_id, str) or not project_id:
        errors.append("missing project_id")
    if not isinstance(checkpoints, list) or not isinstance(entries, list):
        return {
            "ok": False,
            "errors": errors
            + ["checkpoints and audit_entries must be arrays"],
        }
    previous_digest = None
    previous_through = None
    for index, checkpoint in enumerate(checkpoints):
        label = f"checkpoint[{index}]"
        if not isinstance(checkpoint, dict):
            errors.append(f"{label} must be an object")
            continue
        digest = checkpoint.get("digest")
        if checkpoint.get("project_id") != project_id:
            errors.append(f"{label} project_id mismatch")
        if checkpoint.get("previous_digest") != previous_digest:
            errors.append(f"{label} previous_digest mismatch")
        if not isinstance(digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", digest
        ):
            errors.append(f"{label} invalid digest")
        start, end, count = (
            checkpoint.get("from_seq"),
            checkpoint.get("through_seq"),
            checkpoint.get("entry_count"),
        )
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (start, end, count)
        ):
            errors.append(f"{label} range and entry_count must be integers")
        elif start > end or count < 1 or count > end - start + 1:
            errors.append(f"{label} invalid range or entry_count")
        elif previous_through is not None and start <= previous_through:
            errors.append(
                f"{label} overlaps or reorders the previous checkpoint"
            )
        previous_digest, previous_through = digest, end
    head = previous_digest
    if bundle.get("head_digest") != head:
        errors.append("head_digest does not match the checkpoint chain")
    if expected_head_digest is not None and expected_head_digest != head:
        errors.append("expected head digest mismatch")
    previous_seq = previous_through
    for index, entry in enumerate(entries):
        label = f"audit_entries[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object")
            continue
        if entry.get("project_id") != project_id:
            errors.append(f"{label} project_id mismatch")
        seq = entry.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            errors.append(f"{label} invalid seq")
        elif previous_seq is not None and seq <= previous_seq:
            errors.append(
                f"{label} is not strictly ordered after prior audit data"
            )
        previous_seq = (
            seq
            if isinstance(seq, int) and not isinstance(seq, bool)
            else previous_seq
        )
    return {
        "ok": not errors,
        "project_id": project_id,
        "head_digest": head,
        "checkpoint_count": len(checkpoints),
        "audit_entry_count": len(entries),
        "anchored": expected_head_digest is not None,
        "errors": errors,
    }
