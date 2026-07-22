from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openspace.cloud.skill_quality_reporter import build_skill_quality_judgment_payload
from openspace.skill_engine.quality import (
    FailureDomain,
    QualityObservation,
    aggregate_quality,
    classify_skill_outcome,
    promotion_eligibility,
)
from openspace.skill_engine.types import ExecutionAnalysis, SkillJudgment


def _analysis(*, completed: bool, phase_failed: bool = False, note: str = ""):
    analysis = ExecutionAnalysis(
        task_id="task-1",
        timestamp=datetime.now(timezone.utc),
        task_completed=completed,
        execution_note=note,
        skill_judgments=[SkillJudgment("skill-1", skill_applied=True)],
        skill_phase_failed_skill_ids=["skill-1"] if phase_failed else [],
    )
    analysis.duration_ms = 123
    return analysis


def test_explicit_skill_phase_failure_is_attributable() -> None:
    analysis = _analysis(completed=False, phase_failed=True)
    result = classify_skill_outcome(analysis, analysis.skill_judgments[0])
    assert result.failure_domain == FailureDomain.SKILL
    assert result.attributable_to_skill is True
    assert result.counts_toward_skill_quality is True


def test_external_permission_failure_is_excluded_from_skill_denominator() -> None:
    analysis = _analysis(completed=False, note="Permission denied by operator policy")
    judgment = analysis.skill_judgments[0]
    result = classify_skill_outcome(analysis, judgment)
    assert result.failure_domain == FailureDomain.PERMISSION
    assert result.attributable_to_skill is False

    payload = build_skill_quality_judgment_payload(
        analysis,
        judgment,
        cloud_skill_id="cloud-skill-1",
        session_id="session-1",
    )
    assert payload["quality_schema_version"] == "skill_quality_v2"
    assert payload["failure_reason"] == "permission"
    assert payload["duration_ms"] == 123
    assert payload["extras"]["counts_toward_skill_quality"] is False


def test_time_decay_wilson_interval_and_diversity_gates() -> None:
    now = datetime.now(timezone.utc)
    observations = [
        QualityObservation(True, now, "task-1", "session-1", "replay"),
        QualityObservation(True, now - timedelta(days=1), "task-2", "session-2", "live"),
        QualityObservation(
            False,
            now,
            "task-external",
            "session-2",
            "live",
            FailureDomain.TOOL,
            1.0,
        ),
    ]
    snapshot = aggregate_quality(observations, now=now)
    assert snapshot.score == 1.0
    assert snapshot.included_observations == 2
    assert snapshot.excluded_observations == 1
    assert 0.0 < snapshot.confidence_low < snapshot.confidence_high <= 1.0
    decision = promotion_eligibility(
        snapshot,
        min_confidence_low=0.0,
        min_evidence_domains=2,
        min_distinct_sessions=2,
    )
    assert decision["eligible"] is True


def test_quality_aggregation_rejects_non_finite_configuration() -> None:
    with pytest.raises(ValueError, match="finite"):
        aggregate_quality([], half_life_days=float("nan"))
