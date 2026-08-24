"""Database backup and index-rebuild operations."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable


class OperationsRepository:
    """Own database-level operations behind the stable facade."""

    def __init__(
        self,
        store: Any,
        now: Callable[[], str],
        uid: Callable[[], str],
    ):
        self.store = store
        self.now = now
        self.uid = uid

    def backup_to(
        self, output_path: str | Path, encryption_passphrase: str | None = None
    ) -> dict[str, Any]:
        """Create a SQLite snapshot, including committed WAL data."""
        destination = Path(output_path).expanduser().resolve()
        if destination == self.store.path:
            raise ValueError(
                "backup output must differ from the live database"
            )
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_name(
            f".{destination.name}.{self.uid()}.tmp"
        )
        target = sqlite3.connect(temporary)
        try:
            self.store.conn.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(
                    f"backup integrity check failed: {integrity}"
                )
        except Exception:
            target.close()
            temporary.unlink(missing_ok=True)
            raise
        else:
            target.close()
        os.chmod(temporary, 0o600)
        encryption = {"encrypted": False}
        if encryption_passphrase is not None:
            from ..backup_crypto import encrypt_file

            plaintext = temporary
            encrypted = temporary.with_suffix(temporary.suffix + ".enc")
            try:
                encryption = encrypt_file(
                    plaintext, encrypted, encryption_passphrase
                )
                os.chmod(encrypted, 0o600)
                temporary = encrypted
            except Exception:
                encrypted.unlink(missing_ok=True)
                raise
            finally:
                plaintext.unlink(missing_ok=True)
        os.replace(temporary, destination)
        digest = hashlib.sha256()
        with destination.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "ok": True,
            "source": str(self.store.path),
            "output": str(destination),
            "bytes": destination.stat().st_size,
            "sha256": digest.hexdigest(),
            "created_at": self.now(),
            "integrity": "ok",
            **encryption,
        }

    def rebuild_fts(self, project_id: str | None = None) -> dict[str, Any]:
        if project_id and not self.store._row(
            "SELECT id FROM projects WHERE id=?", (project_id,)
        ):
            raise KeyError("project not found")
        condition = " WHERE project_id=?" if project_id else ""
        args = (project_id,) if project_id else ()
        with self.store.tx() as cx:
            if project_id:
                ids = [
                    row[0]
                    for row in cx.execute(
                        "SELECT id FROM memories WHERE project_id=?", args
                    )
                ]
                if ids:
                    cx.execute(
                        "DELETE FROM memories_fts WHERE memory_id IN ("
                        + ",".join("?" for _ in ids)
                        + ")",
                        ids,
                    )
            else:
                cx.execute("DELETE FROM memories_fts")
            rows = list(
                cx.execute(
                    "SELECT id,title,content,tags_json FROM memories"
                    + condition,
                    args,
                )
            )
            for row in rows:
                cx.execute(
                    "INSERT INTO memories_fts(memory_id,title,content,tags)"
                    " VALUES(?,?,?,?)",
                    (
                        row["id"],
                        row["title"],
                        row["content"],
                        " ".join(json.loads(row["tags_json"])),
                    ),
                )
            if self.store.embedding_provider:
                memories = list(
                    cx.execute("SELECT * FROM memories" + condition, args)
                )
                for memory in memories:
                    self.store._index_embedding(cx, dict(memory))
        return {
            "ok": True,
            "project_id": project_id,
            "indexed_memories": len(rows),
            "embedding_provider": self.store._provider_name(),
            "embedded_memories": len(rows)
            if self.store.embedding_provider
            else 0,
        }
