from datetime import datetime, timezone

from context_memory.wiki_lint import (
    embedded_citation_findings,
    embedded_memory_ids,
    finish_lint_result,
    memory_citation_findings,
    source_reinspection_finding,
)


def test_embedded_citation_rules_are_database_free():
    citation = {
        "memory_id": "memory-1",
        "source_event_ids": ["event-1"],
    }
    revision = {
        "sections": {
            "current_position": [
                {"claim": "We should adopt it.", "citations": citation}
            ],
            "open_questions": [{"claim": "What remains?"}],
        },
        "citations": [
            {
                "section": "current_position",
                "ordinal": 0,
                "memory_id": "memory-1",
                "event_id": "event-1",
            }
        ],
    }

    assert embedded_memory_ids(revision) == {"memory-1"}
    findings = embedded_citation_findings(
        revision, {"memory-1": ("evidence", False)}
    )

    assert {item["code"] for item in findings} == {
        "missing_citation",
        "recommendation_mislabeled_as_evidence",
        "unsupported_recommendation",
    }


def test_memory_rules_allow_terminal_history_and_flag_disputes():
    terminal = memory_citation_findings(
        "memory-1",
        {"status": "superseded"},
        {"decision_timeline"},
        1,
        1,
    )
    disputed = memory_citation_findings(
        "memory-2", {"status": "disputed"}, {"current_position"}, 2, 1
    )

    assert terminal == []
    assert [item["code"] for item in disputed] == [
        "missing_source",
        "unresolved_dispute",
    ]


def test_source_age_and_result_contract_are_deterministic():
    source = {
        "memory_id": "memory-1",
        "source_analysis_id": "source-1",
        "source_type": "wiki",
        "stable_source_id": "page-1",
        "canonical_uri": "https://example.test/page-1",
        "source_version": "7",
        "source_updated_at": None,
        "retrieved_at": "2026-01-01T00:00:00+00:00",
    }
    finding = source_reinspection_finding(
        source, datetime(2026, 2, 1, tzinfo=timezone.utc)
    )
    revision = {"id": "revision-1", "page_id": "page-1"}

    result = finish_lint_result(revision, [finding])

    assert finding["age_days"] == 31
    assert result["status"] == "warn"
    assert result["finding_count"] == 1
    assert result["deterministic"] is True
