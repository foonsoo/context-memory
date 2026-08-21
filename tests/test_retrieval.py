from context_memory.retrieval import retrieval_gate, select_project_candidate


def test_select_project_candidate_requires_confidence_and_separation():
    assert select_project_candidate([]) == (None, "no_candidates", 0.0)
    assert select_project_candidate([{"id": "p1", "confidence": 0.44}]) == (
        None,
        "low_confidence",
        0.44,
    )
    assert select_project_candidate([{"id": "p1", "confidence": 0.45}]) == (
        "p1",
        "single_confident_candidate",
        0.45,
    )
    assert select_project_candidate(
        [
            {"id": "p1", "confidence": 0.72},
            {"id": "p2", "confidence": 0.60},
        ]
    ) == ("p1", "dominant_candidate", 0.72)
    assert select_project_candidate(
        [
            {"id": "p1", "confidence": 0.71},
            {"id": "p2", "confidence": 0.60},
        ]
    ) == (None, "ambiguous_candidates", 0.71)


def test_retrieval_gate_preserves_lexical_and_rejects_weak_vectors():
    lexical = {
        "retrieval": {
            "score": 0.02,
            "lexical_rank": 1,
            "query_coverage": 1.0,
            "semantic_similarity": None,
        }
    }
    assert retrieval_gate([lexical])["reason"] == "lexical_match"

    weak = {
        "retrieval": {
            "score": 0.01,
            "lexical_rank": None,
            "semantic_similarity": 0.29,
        }
    }
    assert retrieval_gate([weak])["reason"] == "weak_vector_only_similarity"

    strong = {
        "retrieval": {
            "score": 0.01,
            "lexical_rank": None,
            "semantic_similarity": 0.31,
        }
    }
    assert retrieval_gate([strong])["reason"] == "strong_vector_only_match"
