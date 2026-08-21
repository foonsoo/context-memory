from __future__ import annotations

from typing import Any

THRESHOLD_KEYS = (
    "checkpoint_soft_usage",
    "checkpoint_hard_usage",
    "checkpoint_elapsed_seconds",
    "checkpoint_event_count",
    "checkpoint_max_age_seconds",
    "checkpoint_cooldown_seconds",
    "checkpoint_hysteresis",
)


def evaluate_checkpoint_policy(
    *,
    context_usage: float | None,
    material_change: bool,
    repository_changed: bool,
    durable_event_count: int,
    session_elapsed: int | None,
    checkpoint_age: int | None,
    recoverable_state_changed: bool,
    latest_context_usage: float | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate checkpoint triggers from already-observed state."""
    if context_usage is not None and not 0 <= context_usage <= 1:
        raise ValueError("context_usage must be between 0 and 1")

    signals = {
        "context_usage": context_usage,
        "material_change": material_change,
        "repository_changed": repository_changed,
        "durable_event_count": durable_event_count,
        "session_elapsed_seconds": session_elapsed,
        "checkpoint_age_seconds": checkpoint_age,
        "recoverable_state_changed": recoverable_state_changed,
    }
    trigger = None
    mode = None
    if context_usage is not None:
        if context_usage >= policy["checkpoint_hard_usage"]:
            trigger, mode = "hard_context_usage", "interim"
        elif (
            context_usage >= policy["checkpoint_soft_usage"]
            and material_change
        ):
            trigger, mode = (
                "soft_context_usage_after_material_change",
                "interim",
            )
    else:
        fallback = [
            (
                session_elapsed is not None
                and session_elapsed >= policy["checkpoint_elapsed_seconds"],
                "elapsed",
            ),
            (
                durable_event_count >= policy["checkpoint_event_count"],
                "event_count",
            ),
            (repository_changed, "repository_change"),
            (
                checkpoint_age is not None
                and checkpoint_age >= policy["checkpoint_max_age_seconds"]
                and material_change,
                "checkpoint_age",
            ),
        ]
        trigger = next((name for matched, name in fallback if matched), None)
        if trigger:
            mode = "interim"

    suppression = None
    if trigger and not recoverable_state_changed:
        suppression = "unchanged_recovery_state"
    elif (
        trigger
        and checkpoint_age is not None
        and checkpoint_age < policy["checkpoint_cooldown_seconds"]
    ):
        suppression = "cooldown"
    elif (
        trigger == "soft_context_usage_after_material_change"
        and latest_context_usage is not None
    ):
        rearm_usage = min(
            1.0,
            latest_context_usage + policy["checkpoint_hysteresis"],
        )
        if context_usage < rearm_usage:
            suppression = "hysteresis"
    if suppression:
        trigger, mode = None, None

    return {
        "should_checkpoint": trigger is not None,
        "recommended_mode": mode,
        "recommended_reason": (
            "context_budget"
            if context_usage is not None and trigger
            else (
                "elapsed"
                if trigger in {"elapsed", "checkpoint_age"}
                else "material_change"
                if trigger
                else None
            )
        ),
        "trigger": trigger,
        "suppression": suppression,
        "signals": signals,
        "thresholds": {key: policy[key] for key in THRESHOLD_KEYS},
    }
