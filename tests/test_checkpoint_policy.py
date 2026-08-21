import pytest

from context_memory.checkpoint_policy import evaluate_checkpoint_policy


POLICY = {
    "checkpoint_soft_usage": 0.6,
    "checkpoint_hard_usage": 0.8,
    "checkpoint_elapsed_seconds": 1800,
    "checkpoint_event_count": 10,
    "checkpoint_max_age_seconds": 3600,
    "checkpoint_cooldown_seconds": 300,
    "checkpoint_hysteresis": 0.05,
}


def evaluate(**overrides):
    arguments = {
        "context_usage": None,
        "material_change": False,
        "repository_changed": False,
        "durable_event_count": 0,
        "session_elapsed": None,
        "checkpoint_age": None,
        "recoverable_state_changed": True,
        "latest_context_usage": None,
        "policy": POLICY,
    }
    arguments.update(overrides)
    return evaluate_checkpoint_policy(**arguments)


def test_checkpoint_policy_context_thresholds():
    assert not evaluate(context_usage=0.6)["should_checkpoint"]
    soft = evaluate(context_usage=0.6, material_change=True)
    assert soft["trigger"] == "soft_context_usage_after_material_change"
    assert soft["recommended_reason"] == "context_budget"
    assert evaluate(context_usage=0.8)["trigger"] == "hard_context_usage"


@pytest.mark.parametrize(
    ("overrides", "trigger", "reason"),
    [
        ({"session_elapsed": 1800}, "elapsed", "elapsed"),
        ({"durable_event_count": 10}, "event_count", "material_change"),
        ({"repository_changed": True}, "repository_change", "material_change"),
        (
            {"checkpoint_age": 3600, "material_change": True},
            "checkpoint_age",
            "elapsed",
        ),
    ],
)
def test_checkpoint_policy_fallback_priority(overrides, trigger, reason):
    result = evaluate(**overrides)
    assert result["trigger"] == trigger
    assert result["recommended_reason"] == reason


@pytest.mark.parametrize(
    ("overrides", "suppression"),
    [
        (
            {"context_usage": 0.8, "recoverable_state_changed": False},
            "unchanged_recovery_state",
        ),
        ({"context_usage": 0.8, "checkpoint_age": 299}, "cooldown"),
        (
            {
                "context_usage": 0.64,
                "material_change": True,
                "latest_context_usage": 0.6,
            },
            "hysteresis",
        ),
    ],
)
def test_checkpoint_policy_suppression(overrides, suppression):
    result = evaluate(**overrides)
    assert not result["should_checkpoint"]
    assert result["trigger"] is None
    assert result["suppression"] == suppression


def test_checkpoint_policy_rejects_invalid_usage():
    with pytest.raises(ValueError, match="between 0 and 1"):
        evaluate(context_usage=1.01)
