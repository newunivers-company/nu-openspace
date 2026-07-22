"""Deterministic quality attribution and confidence-aware aggregation."""

from .attribution import (
    AttributionResult,
    FailureDomain,
    QualityObservation,
    QualitySnapshot,
    aggregate_quality,
    classify_skill_outcome,
    promotion_eligibility,
)

__all__ = [
    "AttributionResult",
    "FailureDomain",
    "QualityObservation",
    "QualitySnapshot",
    "aggregate_quality",
    "classify_skill_outcome",
    "promotion_eligibility",
]
