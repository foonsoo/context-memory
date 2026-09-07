#!/usr/bin/env python3
"""Evaluate global versus project-configured aliases on regression data."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from context_memory.store import MemoryStore


LEGACY_SPECIALIZED = {
    "설치": "wheel",
    "클라이언트": "handoff",
    "패키지": "scope",
    "옮겼는데": "scope",
}


def _legacy_query(query: str) -> str:
    additions = [value for key, value in LEGACY_SPECIALIZED.items() if key in query]
    return " ".join([query, *additions])


def _ids(result: dict[str, Any]) -> list[str]:
    return [item.get("memory_id") for item in result.get("items", [])]


def run() -> dict[str, Any]:
    scenarios = [
        ("auth-api-no-answer", "auth", "인증 API 설치", None),
        ("payment-api-no-answer", "payments", "결제 API 클라이언트", None),
        ("server-restart", "servers", "서버 재시작 검증", "server"),
        ("generic-app-install", "apps", "일반 앱 설치", "app"),
        ("other-project-only", "empty", "설치 검증", None),
        ("mixed-project-alias", "cli", "설치 검증", "wheel"),
        ("stale-decision", "database", "이전 설치 결정", "current"),
    ]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = MemoryStore(root / "alias-regression.db")
        projects: dict[str, tuple[str, Path]] = {}
        memories: dict[str, str] = {}
        try:
            for slug in {item[1] for item in scenarios}:
                path = root / slug
                resolved = store.resolve_project(str(path))
                projects[slug] = (resolved["project"]["id"], path)

            def memory(slug: str, key: str, text: str, status: str = "active") -> None:
                project_id, _ = projects[slug]
                event = store.record_event(project_id, "decision", text)
                item = store.upsert_memory(
                    project_id, key, text, "decision", status,
                    source_event_ids=[event["id"]],
                )
                memories[key] = item["id"]

            memory("servers", "server", "Restart the server after configuration")
            memory("apps", "app", "Install the general desktop application")
            memory("cli", "wheel", "Run the installed wheel verification")
            memory("database", "old", "Install SQLite", "superseded")
            memory("database", "current", "Current decision uses PostgreSQL")
            modes: dict[str, Any] = {}
            for mode in ("legacy-global", "default", "project-configured"):
                store.conn.execute(
                    "DELETE FROM search_aliases WHERE project_id=?",
                    (projects["cli"][0],),
                )
                if mode == "project-configured":
                    store.set_search_aliases(
                        projects["cli"][0], "설치", ["wheel"]
                    )
                outcomes = []
                correct = 0
                for case_id, slug, query, expected_key in scenarios:
                    project_id, cwd = projects[slug]
                    effective = _legacy_query(query) if mode == "legacy-global" else query
                    result = store.context_recall(str(cwd), effective)
                    returned = _ids(result)
                    expected = memories.get(expected_key) if expected_key else None
                    passed = expected in returned if expected else not returned
                    correct += int(passed)
                    outcomes.append(
                        {
                            "case": case_id,
                            "query": query,
                            "expected_memory_id": expected,
                            "returned_memory_ids": returned,
                            "passed": passed,
                        }
                    )
                modes[mode] = {
                    "accuracy": correct / len(scenarios),
                    "passed": correct,
                    "total": len(scenarios),
                    "outcomes": outcomes,
                }
            return {
                "schema_version": 1,
                "dataset": "synthetic-regression-not-independent-evaluation",
                "embeddings": "default local-hash",
                "modes": modes,
            }
        finally:
            store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
