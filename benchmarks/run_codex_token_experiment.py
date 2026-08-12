#!/usr/bin/env python3
"""Run reproducible paired Codex startup measurements and write a manifest.

The runner never points Context Memory at the user's live database. Every Codex
session receives a private copy of one frozen synthetic SQLite snapshot. Rollout
files stay in Codex's normal session directory; the manifest stores only paths,
pair/cache labels, and the snapshot digest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from context_memory.store import MemoryStore
try:
    from benchmarks.analyze_codex_tokens import analyze
except ModuleNotFoundError:  # Direct `python benchmarks/run_...py` execution.
    from analyze_codex_tokens import analyze


WORKFLOWS = {
    "bootstrap": "Call context_memory context_bootstrap exactly once with cwd={workspace!r}, query='next implementation task', client='codex-token-benchmark', external_id={external_id!r}, response_format='compact'. Do not call any other tool. Then reply done.",
    "legacy": "Call these context_memory tools in this exact order and no others: project_resolve with cwd={workspace!r}; session_start using the returned project and scope, client='codex-token-benchmark', external_id={external_id!r}; get_context using that project and scope, query='next implementation task', response_format='compact'. Then reply done.",
}


def create_snapshot(path: Path, workspace: Path) -> str:
    store = MemoryStore(path)
    try:
        project = store.resolve_project(str(workspace))["project"]
        for index in range(5):
            store.upsert_memory(
                project["id"], f"Synthetic checkpoint {index + 1}",
                f"Verified synthetic implementation fact {index + 1}; next task remains paired startup measurement.",
                "task" if index == 4 else "fact", "active",
            )
    finally:
        store.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def session_files(root: Path) -> set[Path]:
    return set(root.glob("**/rollout-*.jsonl")) if root.exists() else set()


def toml_string(value: str) -> str:
    return json.dumps(value)


def run_attempt(args: argparse.Namespace, snapshot: Path, pair: str, workflow: str,
                attempt: int) -> Path:
    run_root = args.output / "runs" / f"{pair}-{workflow}-attempt-{attempt}"
    run_root.mkdir(parents=True, exist_ok=False)
    database = run_root / "memory.db"
    shutil.copy2(snapshot, database)
    external_id = f"token-{pair}-{workflow}"
    prompt = WORKFLOWS[workflow].format(workspace=str(args.workspace), external_id=external_id)
    before = session_files(args.sessions_root)
    command = [
        args.codex, "--ask-for-approval", "never", "exec", "--json", "--sandbox", "read-only",
        "-C", str(args.workspace),
        "-c", f"mcp_servers.context_memory.command={toml_string(str(args.server))}",
        "-c", f"mcp_servers.context_memory.args={json.dumps(['--db', str(database), 'serve', '--transport', 'stdio'])}",
        prompt,
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    (run_root / "codex-events.jsonl").write_text(completed.stdout, encoding="utf-8")
    (run_root / "codex-stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(f"Codex failed for {pair}/{workflow}; see {run_root / 'codex-stderr.txt'}")
    deadline = time.monotonic() + 5
    created: set[Path] = set()
    while time.monotonic() < deadline:
        created = session_files(args.sessions_root) - before
        if len(created) == 1:
            break
        time.sleep(.1)
    if len(created) != 1:
        raise RuntimeError(f"expected one new rollout for {pair}/{workflow}, found {len(created)}")
    return created.pop()


def run_once(args: argparse.Namespace, snapshot: Path, pair: str, stratum: str,
             workflow: str) -> Path:
    expected = (["context_bootstrap"] if workflow == "bootstrap" else
                ["project_resolve", "session_start", "get_context"])
    for attempt in range(1, args.max_attempts + 1):
        rollout = run_attempt(args, snapshot, pair, workflow, attempt)
        observed = [name for item in analyze(rollout)["observations"]
                    for name in item["tools_since_previous_model_turn"]]
        if observed == expected:
            return rollout
    raise RuntimeError(f"no controlled rollout for {pair}/{workflow} after {args.max_attempts} attempts")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--sessions-root", type=Path, default=Path.home() / ".codex" / "sessions")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--server", type=Path, default=Path.home() / ".local/share/context-memory/runtime/bin/context-memory")
    parser.add_argument("--pairs-per-stratum", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()
    args.workspace = args.workspace.resolve(); args.output = args.output.resolve()
    if args.output.exists():
        parser.error("--output must not already exist")
    args.output.mkdir(parents=True)
    snapshot = args.output / "frozen-snapshot.db"
    digest = create_snapshot(snapshot, args.workspace)
    runs = []
    try:
        for stratum in ("cold", "warm"):
            for index in range(1, args.pairs_per_stratum + 1):
                pair = f"{stratum}-{index}"
                for workflow in ("bootstrap", "legacy"):
                    rollout = run_once(args, snapshot, pair, stratum, workflow)
                    runs.append({"pair": pair, "stratum": stratum, "workflow": workflow,
                                 "session": str(rollout)})
    finally:
        manifest = {"schema_version": 1, "snapshot_sha256": digest, "runs": runs}
        (args.output / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
