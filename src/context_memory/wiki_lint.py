from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

SOURCE_REINSPECTION_AGE_DAYS = 30


def embedded_memory_ids(revision: dict[str, Any]) -> set[str]:
    """Collect memory IDs referenced by embedded revision claims."""
    return {
        citation["memory_id"]
        for entries in revision["sections"].values()
        for entry in entries
        if isinstance(entry, dict)
        for key in (
            "citations",
            "decision_citation",
            "outcome_citation",
        )
        if (citation := entry.get(key)) and citation.get("memory_id")
    }


def embedded_citation_findings(
    revision: dict[str, Any],
    claim_provenance: dict[str, tuple[str, bool]],
) -> list[dict[str, Any]]:
    """Check loaded claims and their immutable citation index."""
    findings: list[dict[str, Any]] = []
    citation_keys = {
        (
            item["section"],
            item["ordinal"],
            item["memory_id"],
            item["event_id"],
        )
        for item in revision["citations"]
    }
    for section, entries in revision["sections"].items():
        for ordinal, entry in enumerate(entries):
            embedded = []
            if isinstance(entry, dict):
                embedded.extend(
                    entry.get(key)
                    for key in (
                        "citations",
                        "decision_citation",
                        "outcome_citation",
                    )
                    if entry.get(key)
                )
            if not embedded:
                findings.append(
                    _finding(
                        "missing_citation",
                        "error",
                        "Wiki claim has no memory citation.",
                        section=section,
                        ordinal=ordinal,
                    )
                )
                continue
            for citation in embedded:
                memory_id = citation.get("memory_id")
                event_ids = citation.get("source_event_ids") or []
                if not memory_id or not event_ids:
                    findings.append(
                        _finding(
                            "missing_citation",
                            "error",
                            "Wiki citation is incomplete.",
                            section=section,
                            ordinal=ordinal,
                            memory_id=memory_id,
                        )
                    )
                    continue
                for event_id in event_ids:
                    if (
                        section,
                        ordinal,
                        memory_id,
                        event_id,
                    ) not in citation_keys:
                        findings.append(
                            _finding(
                                "missing_citation",
                                "error",
                                "Embedded citation is absent from the"
                                " immutable citation index.",
                                section=section,
                                ordinal=ordinal,
                                memory_id=memory_id,
                                event_id=event_id,
                            )
                        )
                signal = recommendation_signal(
                    str(
                        entry.get("claim")
                        or entry.get("observed_outcome")
                        or ""
                    )
                )
                if not signal:
                    continue
                role, supported = claim_provenance[memory_id]
                if role == "evidence":
                    findings.append(
                        _finding(
                            "recommendation_mislabeled_as_evidence",
                            "error",
                            "Recommendation-like language must be labeled"
                            " as inference, not evidence.",
                            section=section,
                            ordinal=ordinal,
                            memory_id=memory_id,
                            detected_signal=signal,
                            current_label=role,
                            required_label="inference",
                        )
                    )
                if not supported and role not in {"decision", "action"}:
                    findings.append(
                        _finding(
                            "unsupported_recommendation",
                            "error",
                            "Recommendation-like claim has no explicit"
                            " supporting claim or memory relation.",
                            section=section,
                            ordinal=ordinal,
                            memory_id=memory_id,
                            detected_signal=signal,
                            claim_role=role,
                            required_label="inference",
                        )
                    )
    return findings


def memory_citation_findings(
    memory_id: str,
    memory: dict[str, Any] | None,
    citation_sections: set[str],
    indexed_citations: int,
    exact_sources: int,
) -> list[dict[str, Any]]:
    """Check one cited memory from observed persistence state."""
    if not memory:
        return [
            _finding(
                "missing_source",
                "error",
                "Cited memory no longer exists.",
                memory_id=memory_id,
            )
        ]
    findings = []
    if exact_sources != indexed_citations:
        findings.append(
            _finding(
                "missing_source",
                "error",
                "A citation is not backed by an exact memory source event.",
                memory_id=memory_id,
                indexed_citations=indexed_citations,
                exact_sources=exact_sources,
            )
        )
    historical_sections = {"decision_timeline", "considered_alternatives"}
    history_only = bool(citation_sections) and (
        citation_sections <= historical_sections
    )
    if memory["status"] in {"superseded", "expired", "rejected"}:
        if not history_only:
            findings.append(
                _finding(
                    "terminal_memory",
                    "error",
                    "Revision cites a terminal memory.",
                    memory_id=memory_id,
                    memory_status=memory["status"],
                )
            )
    elif memory["status"] == "disputed":
        findings.append(
            _finding(
                "unresolved_dispute",
                "warning",
                "Revision cites a disputed memory.",
                memory_id=memory_id,
                memory_status=memory["status"],
            )
        )
    return findings


def source_reinspection_finding(
    source: dict[str, Any], inspected_at: datetime
) -> dict[str, Any] | None:
    """Return an age prompt for one already-loaded source version."""
    try:
        retrieved_at = datetime.fromisoformat(
            source["retrieved_at"].replace("Z", "+00:00")
        )
        if retrieved_at.tzinfo is None:
            retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
        age_days = max(
            0,
            (inspected_at - retrieved_at.astimezone(timezone.utc)).days,
        )
    except (AttributeError, TypeError, ValueError):
        return None
    if age_days < SOURCE_REINSPECTION_AGE_DAYS:
        return None
    return _finding(
        "source_reinspection_due",
        "warning",
        "The cited source version was inspected long enough ago to warrant"
        " reinspection; this does not establish that the external source"
        " changed or that the citation is stale.",
        memory_id=source["memory_id"],
        source_analysis_id=source["source_analysis_id"],
        source_type=source["source_type"],
        stable_source_id=source["stable_source_id"],
        canonical_uri=source["canonical_uri"],
        source_version=source["source_version"],
        source_updated_at=source["source_updated_at"],
        retrieved_at=source["retrieved_at"],
        age_days=age_days,
        threshold_days=SOURCE_REINSPECTION_AGE_DAYS,
        prompt="reinspect_source_version",
        external_change_verified=False,
    )


def finish_lint_result(
    revision: dict[str, Any], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Sort deterministic findings and construct the stable contract."""
    findings.sort(
        key=lambda item: (
            item["code"],
            item.get("section", ""),
            item.get("ordinal", -1),
            item.get("memory_id", ""),
            item.get("event_id", ""),
        )
    )
    return {
        "contract_version": "topic-wiki-lint/v1",
        "revision_id": revision["id"],
        "page_id": revision["page_id"],
        "status": (
            "fail"
            if any(item["severity"] == "error" for item in findings)
            else "warn"
            if findings
            else "pass"
        ),
        "finding_count": len(findings),
        "findings": findings,
        "check_mode": "deterministic_rules",
        "deterministic": True,
        "model_assisted": False,
        "state_changed": False,
        "autonomous_state_changes": False,
    }


def recommendation_signal(claim: str) -> str | None:
    patterns = (
        ("recommend", r"\b(?:recommend|recommended|advisable)\b"),
        ("should", r"\b(?:should|ought\s+to|best\s+to)\b"),
        (
            "korean_recommend",
            r"(?:권장|추천|하는\s*것이\s*좋|해야\s*(?:한다|합니다|함))",
        ),
    )
    folded = claim.casefold()
    return next(
        (name for name, pattern in patterns if re.search(pattern, folded)),
        None,
    )


def _finding(
    code: str, severity: str, message: str, **details: Any
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        **details,
    }
