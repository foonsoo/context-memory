from __future__ import annotations

PROMOTABLE_EVENT_KINDS = (
    "fact",
    "decision",
    "preference",
    "constraint",
    "procedure",
    "task",
    "summary",
)

MEMORY_TYPES = {
    "fact",
    "decision",
    "preference",
    "constraint",
    "procedure",
    "summary",
    "task",
    "other",
}
INVESTIGATION_ROLES = {
    "evidence",
    "inference",
    "action",
    "decision",
    "rationale",
    "outcome",
}
OUTCOME_EFFECTS = {"confirms", "weakens", "disputes", "supersedes"}
SOURCE_REINSPECTION_REASONS = {
    "old",
    "unavailable",
    "newer_version_known",
}


def promotable_kinds_text() -> str:
    return ", ".join(f"`{kind}`" for kind in PROMOTABLE_EVENT_KINDS)


def workflow_guide() -> str:
    kinds = promotable_kinds_text()
    lines = (
        "# Shared Context Memory workflow",
        "",
        "- At the start of every task, call `context_bootstrap` once "
        "with the current workspace directory, the user's request as a "
        "focused query, a 4,000–8,000 character budget, "
        "`response_format=compact`, the actual client name, and the "
        "current client session/task ID as `external_id` when available. "
        "Use the returned project and scope IDs for all writes; never ask "
        "the user for a project UUID. Treat the directory as an identity "
        "hint because retrieval may discover relevant memories from "
        "another registered project. Inspect the bounded recent event tail "
        "as well as promoted memories before concluding that prior work or "
        "an artifact is missing; repository paths in newer immutable events "
        "take precedence over an empty workspace hint.",
        "- Inspect consequential citations with `get_source` before "
        "relying on them. Treat disputed memories as warnings.",
        "- Record durable user decisions, constraints, verified facts, "
        "material test results, procedures, tasks, preferences, and "
        "concise summaries as immutable `record_event` evidence. The "
        "event kinds eligible for automatic session-end memory proposals "
        f"are exactly: {kinds}. Other kinds remain valid immutable events "
        "but are not automatically promoted.",
        "- Derive memories from `source_event_ids`. Keep model-inferred "
        "summaries `proposed`; activate only information confirmed by the "
        "user, repository, tests, or authoritative evidence. Review "
        "proposed memories instead of silently treating them as verified.",
        "- Never store credentials, secrets, raw environment dumps, "
        "unrelated personal data, or routine tool chatter.",
        "- When information changes, create and activate the evidenced "
        "replacement, then mark the prior memory `superseded`. Use "
        "`disputed` for unresolved conflicts. Never rewrite immutable "
        "event history to repair an event kind.",
        "- Before finishing, preserve concise completion evidence, "
        "promote only reusable verified information, and call "
        "`session_end`. Review the returned candidates and warnings.",
        "",
        "This contract is client-neutral. Hooks may automate parts of it, "
        "but correctness must not depend on a hook being installed or "
        "fired.",
        "",
    )
    return "\n".join(lines)
