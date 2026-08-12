from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any

MAGIC = b"CTXMEMENC\x01"


def _crypto() -> tuple[Any, Any]:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:
        raise RuntimeError(
            "encrypted backups require the optional 'crypto' extra: pip install 'context-memory[crypto]'"
        ) from exc
    return AESGCM, Scrypt


def encrypt_file(source: Path, destination: Path, passphrase: str) -> dict[str, Any]:
    if not passphrase:
        raise ValueError("backup encryption passphrase cannot be empty")
    AESGCM, Scrypt = _crypto()
    salt, nonce = os.urandom(16), os.urandom(12)
    header = {
        "cipher": "AES-256-GCM", "kdf": "scrypt", "n": 2**14, "r": 8, "p": 1,
        "salt": salt.hex(), "nonce": nonce.hex(), "plaintext_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    key = Scrypt(salt=salt, length=32, n=header["n"], r=header["r"], p=header["p"]).derive(passphrase.encode())
    ciphertext = AESGCM(key).encrypt(nonce, source.read_bytes(), MAGIC + encoded)
    destination.write_bytes(MAGIC + struct.pack(">I", len(encoded)) + encoded + ciphertext)
    return {"encrypted": True, "envelope_version": 1, "cipher": header["cipher"], "kdf": header["kdf"]}


def decrypt_file(source: Path, destination: Path, passphrase: str) -> dict[str, Any]:
    if not passphrase:
        raise ValueError("backup decryption passphrase cannot be empty")
    AESGCM, Scrypt = _crypto()
    payload = source.read_bytes()
    if not payload.startswith(MAGIC) or len(payload) < len(MAGIC) + 4:
        raise ValueError("not a Context Memory encrypted backup envelope")
    offset = len(MAGIC); size = struct.unpack(">I", payload[offset:offset + 4])[0]; offset += 4
    encoded = payload[offset:offset + size]
    try:
        header = json.loads(encoded)
        salt, nonce = bytes.fromhex(header["salt"]), bytes.fromhex(header["nonce"])
        key = Scrypt(salt=salt, length=32, n=header["n"], r=header["r"], p=header["p"]).derive(passphrase.encode())
        plaintext = AESGCM(key).decrypt(nonce, payload[offset + size:], MAGIC + encoded)
    except Exception as exc:
        raise ValueError("encrypted backup authentication failed") from exc
    if hashlib.sha256(plaintext).hexdigest() != header["plaintext_sha256"]:
        raise ValueError("encrypted backup plaintext digest mismatch")
    destination.write_bytes(plaintext)
    return {"ok": True, "encrypted": True, "envelope_version": 1, "plaintext_sha256": header["plaintext_sha256"]}
