"""Build the P6 call inventory from repository syntax trees."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = ROOT / "src/context_memory/store.py"


def _surface(path: Path) -> str:
    relative = path.relative_to(ROOT)
    if relative == Path("src/context_memory/store.py"):
        return "store_internal"
    if relative == Path("src/context_memory/mcp.py"):
        return "mcp"
    if relative == Path("src/context_memory/cli.py"):
        return "cli"
    if relative == Path("src/context_memory/hooks.py"):
        return "hooks"
    if relative == Path("src/context_memory/tasks.py"):
        return "tasks"
    if relative.parts[0] == "tests":
        return "tests"
    return "other_python"


def build_inventory(root: Path = ROOT) -> dict[str, Any]:
    """Return method visibility and syntax-tree call-site evidence."""
    store_path = root / STORE_PATH.relative_to(ROOT)
    tree = ast.parse(store_path.read_text(encoding="utf-8"))
    store_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MemoryStore"
    )
    methods = sorted(
        node.name
        for node in store_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    references: dict[str, dict[str, set[str]]] = {
        method: {} for method in methods
    }
    for path in sorted(root.rglob("*.py")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            source_tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        surface = _surface(path)
        for node in ast.walk(source_tree):
            name = node.attr if isinstance(node, ast.Attribute) else None
            if name not in references:
                continue
            relative = str(path.relative_to(root))
            references[name].setdefault(surface, set()).add(relative)

    entries = []
    for method in methods:
        private = method.startswith("_") and method != "__init__"
        sites = {
            surface: sorted(paths)
            for surface, paths in sorted(references[method].items())
        }
        entries.append(
            {
                "name": method,
                "visibility": "private" if private else "public",
                "surfaces": sites,
                "referenced": bool(sites),
            }
        )
    return {
        "method_count": len(entries),
        "public_count": sum(
            entry["visibility"] == "public" for entry in entries
        ),
        "private_count": sum(
            entry["visibility"] == "private" for entry in entries
        ),
        "unused_private": [
            entry["name"]
            for entry in entries
            if entry["visibility"] == "private" and not entry["referenced"]
        ],
        "methods": entries,
    }


if __name__ == "__main__":
    print(json.dumps(build_inventory(), indent=2, sort_keys=True))
