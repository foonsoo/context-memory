"""MCP investigation tool declarations."""

from .mcp_schema import obj

TOOLS = [
    {
        "name": "investigation_create",
        "description": (
            "Create selective research intent for a decision under "
            "research-provenance/v1."
        ),
        "inputSchema": obj(
            {
                "project_id": {"type": "string"},
                "question": {"type": "string"},
                "reason": {"type": "string"},
                "decision_to_inform": {"type": "string"},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "initiator": {"type": "string"},
                "scope_id": {"type": "string"},
                "investigation_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            ["project_id", "question", "reason", "decision_to_inform"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "investigation_record_source",
        "description": (
            "Atomically record one identified source version and "
            "consequential evidence, inference, action, decision, rationale, "
            "or outcome claims. Claims may cite earlier claims in this "
            "analysis or explicit claims from prior analyses. Inferences "
            "always remain proposed."
        ),
        "inputSchema": obj(
            {
                "investigation_id": {"type": "string"},
                "source": obj(
                    {
                        "source_type": {"type": "string"},
                        "stable_source_id": {"type": "string"},
                        "canonical_uri": {"type": "string"},
                        "source_version": {"type": "string"},
                        "source_updated_at": {"type": "string"},
                        "retrieved_at": {"type": "string"},
                        "section_anchor": {"type": "string"},
                        "access_reason": {"type": "string"},
                        "analysis_method": {"type": "string"},
                        "content_fingerprint": {"type": "string"},
                    },
                    [
                        "source_type",
                        "stable_source_id",
                        "access_reason",
                        "analysis_method",
                    ],
                ),
                "claims": {
                    "type": "array",
                    "items": obj(
                        {
                            "key": {"type": "string"},
                            "role": {
                                "type": "string",
                                "enum": [
                                    "evidence",
                                    "inference",
                                    "action",
                                    "decision",
                                    "rationale",
                                    "outcome",
                                ],
                            },
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                            "evidence_claim_keys": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "evidence_claim_refs": {
                                "type": "array",
                                "items": obj(
                                    {
                                        "source_analysis_id": {
                                            "type": "string"
                                        },
                                        "claim_key": {"type": "string"},
                                    },
                                    ["source_analysis_id", "claim_key"],
                                ),
                            },
                            "expected_outcome": {"type": "string"},
                            "outcome_effect": {
                                "type": "string",
                                "enum": [
                                    "confirms",
                                    "weakens",
                                    "disputes",
                                    "supersedes",
                                ],
                            },
                            "memory_type": {
                                "type": "string",
                                "enum": [
                                    "fact",
                                    "decision",
                                    "preference",
                                    "constraint",
                                    "procedure",
                                    "summary",
                                    "task",
                                    "other",
                                ],
                            },
                            "memory_status": {
                                "type": "string",
                                "enum": ["proposed", "active"],
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "importance": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                        },
                        ["key", "role", "content"],
                    ),
                },
                "session_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            ["investigation_id", "source", "claims"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "investigation_get",
        "description": (
            "Read an investigation intent, source identities and versions, "
            "typed claims, memories, events, and causal evidence links."
        ),
        "inputSchema": obj(
            {"investigation_id": {"type": "string"}}, ["investigation_id"]
        ),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "source_reinspection_request",
        "description": (
            "Request client-owned reinspection of one recorded external "
            "source because it is old, unavailable, or known to have a newer "
            "version. The core records the request and source identity but "
            "never fetches the source."
        ),
        "inputSchema": obj(
            {
                "source_analysis_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "enum": ["old", "unavailable", "newer_version_known"],
                },
                "details": {"type": "string"},
                "known_source_version": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            ["source_analysis_id", "reason"],
        ),
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "investigation_complete",
        "description": (
            "Mark an investigation completed without rewriting its source "
            "analyses or claims."
        ),
        "inputSchema": obj(
            {"investigation_id": {"type": "string"}}, ["investigation_id"]
        ),
        "annotations": {"readOnlyHint": False},
    },
]
