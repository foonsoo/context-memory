from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import context_memory.store as store_module
from context_memory import cli
from context_memory.mcp import TOOLS
from context_memory.store import MemoryStore


ROOT = Path(__file__).parents[1]
FIXED_TIME = "2026-08-20T00:00:00+00:00"


class _ParserCaptured(Exception):
    def __init__(self, parser: argparse.ArgumentParser):
        self.parser = parser


def _capture_parser(
    parser: argparse.ArgumentParser,
    *args: object,
    **kwargs: object,
) -> argparse.Namespace:
    del args, kwargs
    raise _ParserCaptured(parser)


def _stable_default(value: object) -> object:
    if value == argparse.SUPPRESS:
        return "<suppressed>"
    if value == os.getcwd():
        return "<cwd>"
    if isinstance(value, str):
        home = str(Path.home())
        if value == home or value.startswith(home + os.sep):
            return value.replace(home, "<home>", 1)
    if value is None or isinstance(value, (bool, int, float, str, list)):
        return value
    return f"<{type(value).__name__}>"


def _parser_snapshot(parser: argparse.ArgumentParser) -> dict[str, object]:
    arguments: list[dict[str, object]] = []
    commands: dict[str, object] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            commands = {
                name: _parser_snapshot(subparser)
                for name, subparser in action.choices.items()
            }
            continue
        item: dict[str, object] = {
            "action": type(action).__name__,
            "dest": action.dest,
            "options": action.option_strings,
            "required": action.required,
            "nargs": action.nargs,
            "default": _stable_default(action.default),
            "help": action.help,
        }
        if action.choices is not None:
            item["choices"] = list(action.choices)
        if action.type is not None:
            item["type"] = getattr(action.type, "__name__", str(action.type))
        arguments.append(item)
    mutex_groups = [
        {
            "required": group.required,
            "destinations": [action.dest for action in group._group_actions],
        }
        for group in parser._mutually_exclusive_groups
    ]
    return {
        "prog": parser.prog,
        "description": parser.description,
        "arguments": arguments,
        "mutually_exclusive_groups": mutex_groups,
        "commands": commands,
    }


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalize_response(value: object, key: str = "") -> object:
    if key.endswith("_ms"):
        return "<timing>"
    if isinstance(value, float):
        return round(value, 5)
    if isinstance(value, list):
        return [_normalize_response(item) for item in value]
    if isinstance(value, dict):
        return {
            name: _normalize_response(item, name)
            for name, item in value.items()
        }
    return value


def _response_digest(value: object) -> str:
    return _digest(_normalize_response(value))


def _argument_names(surface: dict[str, object]) -> list[str]:
    return [
        str(argument["dest"])
        for argument in surface["arguments"]
        if argument["dest"] != "help"
    ]


def _compact_cli_surface(surface: dict[str, object]) -> dict[str, object]:
    commands = surface["commands"]
    return {
        "prog": surface["prog"],
        "arguments": _argument_names(surface),
        "sha256": _digest(surface),
        "commands": [
            {
                "name": name,
                "arguments": _argument_names(command),
                "sha256": _digest(command),
            }
            for name, command in commands.items()
        ],
    }


def cli_surface() -> dict[str, object]:
    try:
        with patch.object(
            argparse.ArgumentParser,
            "parse_args",
            _capture_parser,
        ):
            cli.main()
    except _ParserCaptured as captured:
        return _compact_cli_surface(_parser_snapshot(captured.parser))
    raise AssertionError("CLI parser was not captured")


def _ids() -> Iterator[str]:
    index = 1
    while True:
        yield f"00000000-0000-4000-8000-{index:012d}"
        index += 1


def _build_store_fixture(root: Path) -> dict[str, object]:
    generated_ids = _ids()
    with (
        patch.dict(os.environ, {"CONTEXT_MEMORY_EMBEDDINGS": "off"}),
        patch.object(store_module, "now", return_value=FIXED_TIME),
        patch.object(
            store_module,
            "uid",
            side_effect=lambda: next(generated_ids),
        ),
    ):
        store = MemoryStore(root / "memory.db")
        try:
            project = store.create_project(
                "compatibility-baseline",
                "Compatibility baseline",
                "Frozen P6 public-contract fixture",
            )
            project_id = project["id"]
            scope = store.create_scope(
                project_id,
                "fixture",
                "/workspace/fixture",
            )
            store.set_project_alias(project_id, "path", "/workspace/fixture")
            session = store.start_session(
                project_id,
                "compatibility-test",
                scope["id"],
                "p6-baseline",
                {"purpose": "freeze public contracts"},
            )
            decision_event = store.record_event(
                project_id,
                "decision",
                "Use SQLite as the authoritative local store.",
                session_id=session["id"],
                scope_id=scope["id"],
                source_uri="repo://architecture",
            )
            constraint_event = store.record_event(
                project_id,
                "constraint",
                "The local product must keep a single-file backup path.",
                session_id=session["id"],
                scope_id=scope["id"],
                source_uri="repo://roadmap",
            )
            decision = store.upsert_memory(
                project_id,
                "SQLite authority decision",
                "Use SQLite as the authoritative local store.",
                "decision",
                "active",
                .95,
                .9,
                scope["id"],
                [decision_event["id"]],
                tags=["storage", "sqlite"],
            )
            constraint = store.upsert_memory(
                project_id,
                "Single-file backup constraint",
                "The local product must keep a single-file backup path.",
                "constraint",
                "active",
                .9,
                .8,
                scope["id"],
                [constraint_event["id"]],
                tags=["backup", "sqlite"],
            )
            store.create_relation(
                project_id,
                constraint["id"],
                decision["id"],
                "supports",
                "The backup constraint supports the storage choice.",
            )
            store.set_search_aliases(project_id, "sqlite", ["database"])
            store.record_memory_feedback(decision["id"], "retrieved")
            store.propose_correction(
                project_id,
                decision["id"],
                "Use SQLite as the authoritative local evidence store.",
            )

            investigation = store.create_investigation(
                project_id,
                "Which local store preserves provenance?",
                "Confirm the storage decision.",
                "The authoritative persistence choice.",
                ["single file", "local first"],
                "compatibility-test",
                scope["id"],
            )
            analysis = store.record_source_analysis(
                investigation["id"],
                {
                    "source_type": "repository",
                    "stable_source_id": "architecture",
                    "canonical_uri": "repo://architecture",
                    "source_version": "p6-baseline",
                    "access_reason": "Verify the current contract.",
                    "analysis_method": "direct inspection",
                },
                [
                    {
                        "key": "authority",
                        "role": "evidence",
                        "title": "SQLite is authoritative",
                        "content": "SQLite remains the writable authority.",
                        "memory_status": "active",
                    },
                    {
                        "key": "choice",
                        "role": "decision",
                        "title": "Keep SQLite authority",
                        "content": "Keep SQLite as the authoritative store.",
                        "evidence_claim_keys": ["authority"],
                        "memory_status": "active",
                    },
                ],
                session["id"],
            )
            store.request_source_reinspection(
                analysis["source_analysis_id"],
                "newer_version_known",
                "Recheck after the next architecture revision.",
                "p6-next",
            )

            context = store.get_context(
                project_id,
                "SQLite authority decision",
                4000,
                scope_id=scope["id"],
                discover_projects=False,
                response_format="compact",
            )
            brief = store.decision_context(
                project_id,
                "Which SQLite authority decision is current?",
                5000,
                scope["id"],
                False,
            )
            page = store.create_wiki_page(
                project_id,
                "SQLite authority",
                "SQLite Authority Decision",
                scope["id"],
            )
            store.set_wiki_notes(
                page["id"],
                "Reviewed by the compatibility fixture.",
            )
            revision = store.generate_wiki_revision(
                page["id"],
                "Which SQLite authority decision is current?",
                5000,
                {"fixture": "p6-baseline"},
            )
            published = store.transition_wiki_revision(
                revision["id"],
                "published",
                "Compatibility baseline",
            )
            wiki_export = store.export_wiki_markdown(
                project_id,
                scope["id"],
                10,
                0,
            )

            delivered = store.poll_events(
                project_id,
                "compatibility-reader",
                limit=10,
            )
            store.acknowledge_events(
                project_id,
                "compatibility-reader",
                delivered["next_cursor"],
            )
            for index in range(105):
                store.record_event(
                    project_id,
                    "trace",
                    f"Audit fixture {index:03d}",
                )
            store.set_policy(project_id, audit_keep_entries=100)
            store.maintain(project_id, True)
            records = store.export_project(project_id)

            record_order = list(
                dict.fromkeys(record["record_type"] for record in records)
            )
            record_keys = {
                record_type: sorted(
                    set().union(
                        *(
                            set(record["data"])
                            for record in records
                            if record["record_type"] == record_type
                        )
                    )
                )
                for record_type in record_order
            }
            record_counts = {
                record_type: sum(
                    record["record_type"] == record_type for record in records
                )
                for record_type in record_order
            }

            restored = MemoryStore(root / "restored.db")
            try:
                imported = restored.import_project(records)
                restored_records = restored.export_project(project_id)
            finally:
                restored.close()
            round_trip_types = [
                record["record_type"] for record in restored_records
            ]
            return {
                "compact_context": {
                    "sha256": _response_digest(context),
                    "keys": sorted(context),
                    "budget": context["budget"],
                    "used": context["used"],
                    "item_count": len(context["items"]),
                    "retrieval_gate": _normalize_response(
                        context["retrieval_gate"]
                    ),
                    "response_format": context["response_format"],
                },
                "decision_brief": {
                    "sha256": _response_digest(brief),
                    "keys": sorted(brief),
                    "contract_version": brief["contract_version"],
                    "question": brief["question"],
                    "section_counts": {
                        name: len(value)
                        for name, value in brief.items()
                        if isinstance(value, list)
                    },
                },
                "wiki_markdown_export": {
                    "sha256": _response_digest(wiki_export),
                    "keys": sorted(wiki_export),
                    "contract_version": wiki_export["contract_version"],
                    "page_count": wiki_export["page_count"],
                    "source_page_count": wiki_export["source_page_count"],
                    "skipped_page_count": wiki_export["skipped_page_count"],
                    "index": wiki_export["index"],
                    "documents": wiki_export["documents"],
                    "published_revision_id": published["id"],
                },
                "export_import": {
                    "record_order": record_order,
                    "record_keys": record_keys,
                    "record_counts": record_counts,
                    "import_result": imported,
                    "round_trip_record_types_equal": round_trip_types
                    == [record["record_type"] for record in records],
                },
            }
        finally:
            store.close()


def compatibility_snapshot() -> dict[str, object]:
    benchmark = json.loads(
        (ROOT / "benchmarks/results/p25-exit-2026-08-17.json").read_text(
            encoding="utf-8"
        )
    )
    with tempfile.TemporaryDirectory() as temporary:
        store_fixture = _build_store_fixture(Path(temporary))
    return {
        "schema_version": 1,
        "mcp_tools": [
            {
                "name": tool["name"],
                "input_schema_sha256": _digest(tool["inputSchema"]),
                "annotations": tool["annotations"],
            }
            for tool in TOOLS
        ],
        "cli": cli_surface(),
        **store_fixture,
        "thresholds": {
            "retrieval": {
                "discovery_min_confidence": (
                    store_module.DISCOVERY_MIN_CONFIDENCE
                ),
                "discovery_auto_select_confidence": (
                    store_module.DISCOVERY_AUTO_SELECT_CONFIDENCE
                ),
                "discovery_min_margin": store_module.DISCOVERY_MIN_MARGIN,
                "negative_vector_only_min_similarity": (
                    store_module.NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY
                ),
                "negative_vector_only_min_separation": (
                    store_module.NEGATIVE_VECTOR_ONLY_MIN_SEPARATION
                ),
                "local_hash_fallback_candidate_limit": (
                    store_module.LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT
                ),
                "local_hash_fallback_time_limit_ms": (
                    store_module.LOCAL_HASH_FALLBACK_TIME_LIMIT_MS
                ),
                "discovery_project_candidate_limit": (
                    store_module.DISCOVERY_PROJECT_CANDIDATE_LIMIT
                ),
                "source_reinspection_age_days": (
                    store_module.SOURCE_REINSPECTION_AGE_DAYS
                ),
            },
            "p25_exit": {
                "baseline_commit": benchmark["baseline_commit"],
                "current_commit": benchmark["current_commit"],
                "decision_brief": benchmark["decision_brief"],
                "discovery": benchmark["discovery"],
                "embedding_fixture": benchmark["embedding_fixture"],
                "exit_gate": benchmark["exit_gate"],
            },
        },
    }


if __name__ == "__main__":
    print(json.dumps(compatibility_snapshot(), ensure_ascii=False, indent=2))
