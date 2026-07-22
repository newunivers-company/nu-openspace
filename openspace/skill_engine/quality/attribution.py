"""Quality Attribution v2.

The classifier is deliberately deterministic and conservative: a task-level
failure is charged to a skill only when the runtime explicitly identified that
skill's phase as failed. External failures remain visible, but do not reduce the
skill-quality denominator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable


class FailureDomain(str, Enum):
    NONE = "none"
    SKILL = "skill"
    TOOL = "tool"
    MODEL = "model"
    ENVIRONMENT = "environment"
    PERMISSION = "permission"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AttributionResult:
    status: str
    failure_domain: FailureDomain
    attributable_to_skill: bool
    counts_toward_skill_quality: bool
    confidence: float
    signals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QualityObservation:
    success: bool
    occurred_at: datetime
    task_id: str
    session_id: str = ""
    evidence_domain: str = "execution"
    failure_domain: FailureDomain = FailureDomain.NONE
    attribution_confidence: float = 1.0

    @property
    def counts_toward_skill_quality(self) -> bool:
        return self.success or self.failure_domain == FailureDomain.SKILL


@dataclass(frozen=True, slots=True)
class QualitySnapshot:
    score: float
    confidence_low: float
    confidence_high: float
    effective_samples: float
    included_observations: int
    excluded_observations: int
    distinct_tasks: int
    distinct_sessions: int
    distinct_evidence_domains: int
    failure_domains: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "confidence_low": self.confidence_low,
            "confidence_high": self.confidence_high,
            "effective_samples": self.effective_samples,
            "included_observations": self.included_observations,
            "excluded_observations": self.excluded_observations,
            "distinct_tasks": self.distinct_tasks,
            "distinct_sessions": self.distinct_sessions,
            "distinct_evidence_domains": self.distinct_evidence_domains,
            "failure_domains": dict(self.failure_domains),
        }


_PERMISSION_TERMS = (
    "permission denied",
    "not permitted",
    "approval required",
    "unauthorized",
    "forbidden",
)
_CANCELLED_TERMS = ("cancelled", "canceled", "aborted", "interrupted")
_MODEL_TERMS = (
    "rate limit",
    "context length",
    "model timeout",
    "llm timeout",
    "provider error",
    "model unavailable",
)
_ENVIRONMENT_TERMS = (
    "dependency missing",
    "not installed",
    "connection refused",
    "network unavailable",
    "disk full",
    "out of memory",
    "no such file",
)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def classify_skill_outcome(analysis: Any, judgment: Any) -> AttributionResult:
    """Classify one analyzer judgment without relying on another LLM call."""

    skill_id = str(getattr(judgment, "skill_id", "") or "")
    applied = bool(getattr(judgment, "skill_applied", False))
    completed = bool(getattr(analysis, "task_completed", False))
    phase_failed = skill_id in set(
        getattr(analysis, "skill_phase_failed_skill_ids", []) or []
    )
    if applied and completed and not phase_failed:
        return AttributionResult(
            status="success",
            failure_domain=FailureDomain.NONE,
            attributable_to_skill=True,
            counts_toward_skill_quality=True,
            confidence=1.0,
            signals=("skill_applied", "task_completed"),
        )
    if phase_failed:
        return AttributionResult(
            status="failed",
            failure_domain=FailureDomain.SKILL,
            attributable_to_skill=True,
            counts_toward_skill_quality=True,
            confidence=1.0,
            signals=("explicit_skill_phase_failure",),
        )

    note = " ".join(
        str(value or "")
        for value in (
            getattr(analysis, "execution_note", ""),
            getattr(judgment, "note", ""),
            getattr(analysis, "status", ""),
            getattr(analysis, "failure_reason", ""),
        )
    )
    tool_issues = [str(item) for item in getattr(analysis, "tool_issues", []) or []]
    if _contains_any(note, _CANCELLED_TERMS):
        domain, signal = FailureDomain.CANCELLED, "cancellation_marker"
    elif _contains_any(note, _PERMISSION_TERMS):
        domain, signal = FailureDomain.PERMISSION, "permission_marker"
    elif tool_issues:
        domain, signal = FailureDomain.TOOL, "reported_tool_issue"
    elif _contains_any(note, _MODEL_TERMS):
        domain, signal = FailureDomain.MODEL, "model_marker"
    elif _contains_any(note, _ENVIRONMENT_TERMS):
        domain, signal = FailureDomain.ENVIRONMENT, "environment_marker"
    else:
        domain, signal = FailureDomain.UNKNOWN, "insufficient_attribution_evidence"
    confidence = 0.9 if domain != FailureDomain.UNKNOWN else 0.25
    return AttributionResult(
        status="failed",
        failure_domain=domain,
        attributable_to_skill=False,
        counts_toward_skill_quality=False,
        confidence=confidence,
        signals=(signal,),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def aggregate_quality(
    observations: Iterable[QualityObservation],
    *,
    now: datetime | None = None,
    half_life_days: float = 30.0,
    z_score: float = 1.96,
) -> QualitySnapshot:
    """Aggregate included observations with time decay and a Wilson interval."""

    reference = _as_utc(now or datetime.now(timezone.utc))
    half_life = float(half_life_days)
    z = float(z_score)
    if not math.isfinite(half_life) or half_life <= 0:
        raise ValueError("half_life_days must be finite and positive")
    if not math.isfinite(z) or z <= 0:
        raise ValueError("z_score must be finite and positive")
    successes = 0.0
    total = 0.0
    included = 0
    excluded = 0
    task_ids: set[str] = set()
    session_ids: set[str] = set()
    evidence_domains: set[str] = set()
    failures: dict[str, int] = {}
    for observation in observations:
        if not observation.success:
            key = observation.failure_domain.value
            failures[key] = failures.get(key, 0) + 1
        if not observation.counts_toward_skill_quality:
            excluded += 1
            continue
        age_days = max(
            0.0,
            (reference - _as_utc(observation.occurred_at)).total_seconds() / 86400.0,
        )
        time_weight = math.pow(0.5, age_days / half_life)
        confidence_weight = min(1.0, max(0.0, observation.attribution_confidence))
        weight = time_weight * confidence_weight
        total += weight
        successes += weight if observation.success else 0.0
        included += 1
        if observation.task_id:
            task_ids.add(observation.task_id)
        if observation.session_id:
            session_ids.add(observation.session_id)
        if observation.evidence_domain:
            evidence_domains.add(observation.evidence_domain)

    if total <= 0:
        low = high = score = 0.0
    else:
        score = successes / total
        z2 = z * z
        denominator = 1.0 + z2 / total
        center = (score + z2 / (2.0 * total)) / denominator
        margin = (
            z
            * math.sqrt((score * (1.0 - score) / total) + z2 / (4.0 * total * total))
            / denominator
        )
        low = max(0.0, center - margin)
        high = min(1.0, center + margin)
    return QualitySnapshot(
        score=round(score, 6),
        confidence_low=round(low, 6),
        confidence_high=round(high, 6),
        effective_samples=round(total, 6),
        included_observations=included,
        excluded_observations=excluded,
        distinct_tasks=len(task_ids),
        distinct_sessions=len(session_ids),
        distinct_evidence_domains=len(evidence_domains),
        failure_domains=failures,
    )


def promotion_eligibility(
    snapshot: QualitySnapshot,
    *,
    min_success_score: float = 0.8,
    min_confidence_low: float = 0.4,
    min_distinct_tasks: int = 2,
    min_distinct_sessions: int = 1,
    min_evidence_domains: int = 1,
) -> dict[str, Any]:
    """Return explicit trust-promotion gates and their deterministic decision."""

    gates = {
        "success_score": snapshot.score >= min_success_score,
        "confidence_low": snapshot.confidence_low >= min_confidence_low,
        "distinct_tasks": snapshot.distinct_tasks >= min_distinct_tasks,
        "distinct_sessions": snapshot.distinct_sessions >= min_distinct_sessions,
        "evidence_domains": snapshot.distinct_evidence_domains >= min_evidence_domains,
    }
    return {"eligible": all(gates.values()), "gates": gates, "snapshot": snapshot.to_dict()}
