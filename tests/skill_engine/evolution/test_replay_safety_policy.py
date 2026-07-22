from __future__ import annotations

import pytest

from openspace.skill_engine.evolution.behavior_eval import (
    ReplaySafetyPolicy,
    _apply_replay_safety_policy,
    _normalize_replay_result,
)


def _raw(*, tasks: int = 2, baseline_cost: float = 1.0, candidate_cost: float = 1.0):
    return {
        "passed": True,
        "runner": "test",
        "baseline_revision_set": ["skill-v1"],
        "candidate_revision_set": ["skill-v2"],
        "baseline_score": 0.8,
        "candidate_score": 0.8,
        "baseline_cost": baseline_cost,
        "candidate_cost": candidate_cost,
        "replay_task_results": [
            {"task_id": f"task-{index}", "attempted": True, "passed": True}
            for index in range(tasks)
        ],
    }


def _normalized(raw):
    return _normalize_replay_result(
        raw,
        runner_name="test",
        baseline_revision_set=["skill-v1"],
        candidate_revision_set=["skill-v2"],
    )


def test_policy_requires_enough_distinct_executable_tasks() -> None:
    raw = _raw(tasks=1)
    result = _apply_replay_safety_policy(
        _normalized(raw),
        raw,
        ReplaySafetyPolicy(min_executable_tasks=2),
    )
    assert result.passed is False
    assert "insufficient_executable_replay_tasks:1:2" in result.failures


def test_policy_blocks_cost_regression_without_quality_gain() -> None:
    raw = _raw(baseline_cost=1.0, candidate_cost=1.5)
    result = _apply_replay_safety_policy(
        _normalized(raw),
        raw,
        ReplaySafetyPolicy(max_cost_regression_ratio=0.1),
    )
    assert result.passed is False
    assert "candidate_cost_regressed" in result.failures


def test_policy_accepts_cost_increase_for_configured_quality_gain() -> None:
    raw = _raw(baseline_cost=1.0, candidate_cost=1.5)
    raw["candidate_score"] = 0.9
    result = _apply_replay_safety_policy(
        _normalized(raw),
        raw,
        ReplaySafetyPolicy(
            max_cost_regression_ratio=0.1,
            min_score_gain_for_cost_regression=0.05,
        ),
    )
    assert result.passed is True
    assert "candidate_cost_increase_accepted_for_quality_gain" in result.warnings


def test_policy_never_auto_approves_cost_regression_from_zero() -> None:
    raw = _raw(baseline_cost=0.0, candidate_cost=0.1)
    raw["candidate_score"] = 1.0
    result = _apply_replay_safety_policy(
        _normalized(raw),
        raw,
        ReplaySafetyPolicy(min_score_gain_for_cost_regression=0.01),
    )
    assert result.passed is False
    assert "candidate_cost_regressed_from_zero" in result.failures


def test_policy_enforces_canary_contract_when_required() -> None:
    raw = _raw()
    missing = _apply_replay_safety_policy(
        _normalized(raw),
        raw,
        ReplaySafetyPolicy(require_canary=True, min_canary_samples=3),
    )
    assert "missing_required_canary" in missing.failures

    raw["canary"] = {"passed": True, "sample_count": 3, "status": "healthy"}
    accepted = _apply_replay_safety_policy(
        _normalized(raw),
        raw,
        ReplaySafetyPolicy(require_canary=True, min_canary_samples=3),
    )
    assert accepted.passed is True


def test_policy_rejects_non_finite_thresholds_and_cost_metrics() -> None:
    with pytest.raises(ValueError, match="finite"):
        ReplaySafetyPolicy(max_cost_regression_ratio=float("nan"))

    raw = _raw(candidate_cost=float("inf"))
    result = _apply_replay_safety_policy(
        _normalized(raw),
        raw,
        ReplaySafetyPolicy(require_cost_metrics=True),
    )
    assert "missing_replay_cost_metrics" in result.failures
