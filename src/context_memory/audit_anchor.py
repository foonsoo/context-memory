from __future__ import annotations

import base64
import json
import re
from typing import Any

from .clock import utc_now


def _crypto() -> tuple[Any, Any, Any]:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as exc:
        raise RuntimeError(
            "audit signing requires the optional 'crypto' extra: "
            "pip install 'context-memory[crypto]'"
        ) from exc
    return Ed25519PrivateKey, Ed25519PublicKey, InvalidSignature


def _payload(project_id: str, head_digest: str, created_at: str) -> bytes:
    return json.dumps(
        {
            "created_at": created_at,
            "head_digest": head_digest,
            "project_id": project_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def create_anchor(
    project_id: str, head_digest: str, private_key_base64: str
) -> dict[str, Any]:
    if not project_id:
        raise ValueError("project_id cannot be empty")
    if not re.fullmatch(r"[0-9a-f]{64}", head_digest):
        raise ValueError("head_digest must be 64 lowercase hex characters")
    try:
        private_bytes = base64.b64decode(private_key_base64, validate=True)
    except Exception as exc:
        raise ValueError("private key must be valid base64") from exc
    if len(private_bytes) != 32:
        raise ValueError("Ed25519 private key must decode to 32 bytes")
    private_key_type, _, _ = _crypto()
    key = private_key_type.from_private_bytes(private_bytes)
    public_bytes = key.public_key().public_bytes_raw()
    created_at = utc_now()
    signature = key.sign(_payload(project_id, head_digest, created_at))
    return {
        "format": "context-memory-audit-anchor",
        "version": 1,
        "algorithm": "Ed25519",
        "project_id": project_id,
        "head_digest": head_digest,
        "created_at": created_at,
        "public_key": base64.b64encode(public_bytes).decode(),
        "signature": base64.b64encode(signature).decode(),
    }


def verify_anchor(
    anchor: dict[str, Any],
    expected_project_id: str | None = None,
    expected_public_key: str | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    if (
        anchor.get("format") != "context-memory-audit-anchor"
        or anchor.get("version") != 1
    ):
        errors.append("unsupported audit-anchor format or version")
    if anchor.get("algorithm") != "Ed25519":
        errors.append("unsupported signature algorithm")
    project_id, digest, created_at = (
        anchor.get("project_id"),
        anchor.get("head_digest"),
        anchor.get("created_at"),
    )
    if not isinstance(project_id, str) or not project_id:
        errors.append("missing project_id")
    if not isinstance(digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", digest
    ):
        errors.append("invalid head_digest")
    if not isinstance(created_at, str) or not created_at:
        errors.append("missing created_at")
    if expected_project_id is not None and project_id != expected_project_id:
        errors.append("expected project_id mismatch")
    if (
        expected_public_key is not None
        and anchor.get("public_key") != expected_public_key
    ):
        errors.append("expected public key mismatch")
    if not errors:
        _, public_key_type, invalid_signature_error = _crypto()
        try:
            public = base64.b64decode(anchor["public_key"], validate=True)
            signature = base64.b64decode(anchor["signature"], validate=True)
            if len(public) != 32:
                raise ValueError("invalid public key length")
            public_key_type.from_public_bytes(public).verify(
                signature, _payload(project_id, digest, created_at)
            )
        except (KeyError, ValueError, invalid_signature_error):
            errors.append("signature verification failed")
    return {
        "ok": not errors,
        "project_id": project_id,
        "head_digest": digest,
        "created_at": created_at,
        "public_key": anchor.get("public_key"),
        "errors": errors,
    }
