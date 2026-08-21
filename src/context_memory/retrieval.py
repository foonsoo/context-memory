from __future__ import annotations

from typing import Any

DISCOVERY_MIN_CONFIDENCE = 0.45
DISCOVERY_AUTO_SELECT_CONFIDENCE = 0.60
DISCOVERY_MIN_MARGIN = 0.12
NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY = 0.30
NEGATIVE_VECTOR_ONLY_MIN_SEPARATION = 0.03
LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT = 1000
LOCAL_HASH_FALLBACK_TIME_LIMIT_MS = 25
DISCOVERY_PROJECT_CANDIDATE_LIMIT = 12


def select_project_candidate(
    candidates: list[dict[str, Any]],
) -> tuple[str | None, str, float]:
    """Select a sufficiently strong, separated project candidate."""
    if not candidates:
        return None, "no_candidates", 0.0
    top = candidates[0]
    if top["confidence"] < DISCOVERY_MIN_CONFIDENCE:
        return None, "low_confidence", top["confidence"]
    if len(candidates) == 1:
        return top["id"], "single_confident_candidate", top["confidence"]
    margin = top["confidence"] - candidates[1]["confidence"]
    if (
        top["confidence"] >= DISCOVERY_AUTO_SELECT_CONFIDENCE
        and margin >= DISCOVERY_MIN_MARGIN
    ):
        return top["id"], "dominant_candidate", top["confidence"]
    return None, "ambiguous_candidates", top["confidence"]


def retrieval_gate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Reject weak vectors while retaining lexical recall."""
    thresholds = {
        "vector_only_similarity": NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY,
        "vector_only_separation": NEGATIVE_VECTOR_ONLY_MIN_SEPARATION,
    }
    if not candidates:
        return {
            "status": "no_confident_match",
            "reason": "no_candidates",
            "components": {
                "lexical_rank": None,
                "query_coverage": 0.0,
                "semantic_similarity": None,
                "lexical_vector_agreement": False,
                "top_score": None,
                "runner_up_score": None,
                "score_margin": None,
                "semantic_separation": None,
            },
            "thresholds": thresholds,
        }
    top = candidates[0]["retrieval"]
    runner = candidates[1]["retrieval"] if len(candidates) > 1 else None
    top_score = float(top["score"])
    runner_score = float(runner["score"]) if runner else None
    similarity = top.get("semantic_similarity")
    runner_similarity = runner.get("semantic_similarity") if runner else None
    semantic_separation = (
        float(similarity) - float(runner_similarity)
        if similarity is not None and runner_similarity is not None
        else float(similarity or 0.0)
    )
    components = {
        "lexical_rank": top.get("lexical_rank"),
        "query_coverage": top.get("query_coverage", 0.0),
        "semantic_similarity": similarity,
        "lexical_vector_agreement": bool(
            top.get("lexical_rank") is not None and similarity is not None
        ),
        "top_score": top_score,
        "runner_up_score": runner_score,
        "score_margin": (
            top_score - runner_score if runner_score is not None else top_score
        ),
        "semantic_separation": semantic_separation,
    }
    if top.get("lexical_rank") is not None:
        return {
            "status": "accepted",
            "reason": "lexical_match",
            "components": components,
            "thresholds": thresholds,
        }
    if similarity is None or similarity < NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY:
        return {
            "status": "no_confident_match",
            "reason": "weak_vector_only_similarity",
            "components": components,
            "thresholds": thresholds,
        }
    if (
        runner is not None
        and semantic_separation < NEGATIVE_VECTOR_ONLY_MIN_SEPARATION
    ):
        return {
            "status": "no_confident_match",
            "reason": "weak_vector_only_separation",
            "components": components,
            "thresholds": thresholds,
        }
    return {
        "status": "accepted",
        "reason": "strong_vector_only_match",
        "components": components,
        "thresholds": thresholds,
    }
