"""Black-box upgrade check from the latest published local release."""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
import tempfile
from pathlib import Path


PUBLISHED_VERSION = "0.6.2"


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: upgrade_e2e.py CURRENT_WHEEL")
    current_wheel = Path(sys.argv[1]).resolve()
    if not current_wheel.is_file():
        raise AssertionError(f"current wheel not found: {current_wheel}")

    executable = Path(sys.executable).with_name("context-memory")
    run(
        sys.executable,
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "--no-deps",
        f"context-memory-mcp=={PUBLISHED_VERSION}",
    )
    if importlib.metadata.version("context-memory-mcp") != PUBLISHED_VERSION:
        raise AssertionError("published release installation was not selected")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        database = root / "data" / "memory.db"
        backup = root / "backups" / "before-upgrade.db"

        initialized = run(
            str(executable),
            "--db",
            str(database),
            "init",
            "--workspace",
            str(workspace),
            "--launcher",
            "installed",
        )
        project_id = json.loads(initialized.stdout)["project"]["id"]
        backed_up = run(
            str(executable),
            "--db",
            str(database),
            "backup",
            "--output",
            str(backup),
        )
        if json.loads(backed_up.stdout)["integrity"] != "ok":
            raise AssertionError(backed_up.stdout)

        run(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            "--no-deps",
            str(current_wheel),
        )
        doctor = json.loads(
            run(str(executable), "--db", str(database), "doctor").stdout
        )
        if not doctor["ok"]:
            raise AssertionError(doctor)

        projects = json.loads(
            run(
                str(executable),
                "--db",
                str(database),
                "project-list",
            ).stdout
        )
        if project_id not in {project["id"] for project in projects}:
            raise AssertionError("upgrade did not preserve project identity")
        if not backup.is_file():
            raise AssertionError("upgrade removed the pre-upgrade backup")


if __name__ == "__main__":
    main()
