"""Black-box package-removal check for an isolated installed wheel."""

from __future__ import annotations

import importlib.metadata
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    executable = Path(sys.executable).with_name("context-memory")
    if not executable.is_file():
        raise AssertionError(
            f"installed console script not found: {executable}"
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        database = root / "data" / "memory.db"
        initialized = subprocess.run(
            [
                str(executable),
                "--db",
                str(database),
                "init",
                "--workspace",
                str(workspace),
                "--launcher",
                "installed",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        project_id = json.loads(initialized.stdout)["project"]["id"]

        removed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "uninstall",
                "--yes",
                "context-memory-mcp",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if "Successfully uninstalled context-memory-mcp" not in removed.stdout:
            raise AssertionError(removed.stdout)
        if executable.exists():
            raise AssertionError("package removal left the console script")
        try:
            importlib.metadata.version("context-memory-mcp")
        except importlib.metadata.PackageNotFoundError:
            pass
        else:
            raise AssertionError("package removal left distribution metadata")

        if not database.is_file():
            raise AssertionError("package removal deleted the user database")
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        try:
            stored_project = connection.execute(
                "SELECT id FROM projects WHERE id=?",
                (project_id,),
            ).fetchone()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
        if stored_project != (project_id,) or integrity != ("ok",):
            raise AssertionError("preserved user database failed verification")


if __name__ == "__main__":
    main()
