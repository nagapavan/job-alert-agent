"""Deterministic requirement-coverage scoring for resume↔job matching.

The LLM only extracts requirements and labels each ``met`` / ``partial`` / ``missing``;
the numeric score is computed here so it is reproducible, explainable, and testable
without a model.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

SCORE_METHOD = "requirement_coverage_v1"

MET = "met"
PARTIAL = "partial"
MISSING = "missing"
UNKNOWN = "unknown"
KNOWN_STATUSES = (MET, PARTIAL, MISSING)
CREDIT = {MET: 1.0, PARTIAL: 0.5, MISSING: 0.0}

MUST_HAVE = "must_have"
NICE_TO_HAVE = "nice_to_have"

APPLY = "apply"
APPLY_WITH_CAUTION = "apply_with_caution"
SKIP = "skip"
UNKNOWN_RECOMMENDATION = "unknown"


class RequirementItem(BaseModel):
    """One requirement parsed from a job description and assessed against the resume."""

    model_config = ConfigDict(extra="ignore")

    text: str = ""
    kind: str = "other"
    category: str = MUST_HAVE
    status: str = UNKNOWN
    weight: Optional[float] = None
    scope: Optional[str] = None          # experience_years: "total" | "technology"
    technology: Optional[str] = None
    min_years: Optional[float] = None
    max_years: Optional[float] = None
    over_qualified: bool = False
    evidence: str = ""


class RequirementAssessment(BaseModel):
    """Structured assessment returned by the extractor (no numeric score)."""

    model_config = ConfigDict(extra="ignore")

    requirements: List[RequirementItem] = Field(default_factory=list)
    summary: str = ""
    over_qualified: bool = False


@dataclass(frozen=True)
class ScoringConfig:
    """Tunable weights and band cutoffs (overridable from user preferences)."""

    must_weight: float = 3.0
    nice_weight: float = 1.0
    must_floor: float = 0.5
    cap_when_below_floor: float = 55.0
    must_blend: float = 0.7
    nice_blend: float = 0.3
    strong: float = 85.0
    good: float = 70.0
    moderate: float = 55.0
    weak: float = 40.0

    @classmethod
    def from_mapping(cls, values: Optional[Dict[str, Any]]) -> "ScoringConfig":
        if not values:
            return cls()
        allowed = set(cls.__dataclass_fields__)
        clean = {
            k: float(v)
            for k, v in values.items()
            if k in allowed and v is not None
        }
        return replace(cls(), **clean)


@dataclass(frozen=True)
class MatchResult:
    score: Optional[float]
    band: str
    apply_recommendation: str
    must_coverage: Optional[float]
    nice_coverage: Optional[float]
    over_qualified: bool
    missing_must_haves: List[str]
    unverified: List[str]
    met_count: int
    known_count: int
    method: str = SCORE_METHOD


def _effective_weight(req: RequirementItem, config: ScoringConfig) -> float:
    if req.weight is not None:
        return max(0.0, float(req.weight))
    return config.must_weight if req.category == MUST_HAVE else config.nice_weight


def _coverage(reqs: List[RequirementItem], config: ScoringConfig) -> Optional[float]:
    total = sum(_effective_weight(r, config) for r in reqs)
    if total <= 0:
        return None
    earned = sum(_effective_weight(r, config) * CREDIT[r.status] for r in reqs)
    return earned / total


def _band(score: Optional[float], config: ScoringConfig) -> str:
    if score is None:
        return "Unknown"
    if score >= config.strong:
        return "Strong"
    if score >= config.good:
        return "Good"
    if score >= config.moderate:
        return "Moderate"
    if score >= config.weak:
        return "Weak"
    return "Poor"


def compute_match(
    assessment: RequirementAssessment, config: Optional[ScoringConfig] = None
) -> MatchResult:
    """Score an assessment: must-haves dominate, a must-have floor gates the result,
    unknown requirements are excluded from the denominator, and over-qualification is
    advisory (never lowers the score)."""
    config = config or ScoringConfig()
    reqs = list(assessment.requirements)
    known = [r for r in reqs if r.status in KNOWN_STATUSES]
    must = [r for r in known if r.category == MUST_HAVE]
    nice = [r for r in known if r.category == NICE_TO_HAVE]
    unverified = [r.text for r in reqs if r.status not in KNOWN_STATUSES and r.text]

    must_cov = _coverage(must, config)
    nice_cov = _coverage(nice, config)

    if must_cov is None and nice_cov is None:
        score = None
    elif must_cov is None:
        score = nice_cov * 100.0
    elif nice_cov is None:
        score = must_cov * 100.0
    else:
        score = (config.must_blend * must_cov + config.nice_blend * nice_cov) * 100.0

    gated = must_cov is not None and must_cov < config.must_floor
    if gated and score is not None:
        score = min(score, config.cap_when_below_floor)

    over_qualified = assessment.over_qualified or any(r.over_qualified for r in reqs)
    band = _band(score, config)

    if score is None:
        recommendation = UNKNOWN_RECOMMENDATION
    elif gated or band in ("Weak", "Poor"):
        recommendation = SKIP
    elif over_qualified or band == "Moderate":
        recommendation = APPLY_WITH_CAUTION
    else:
        recommendation = APPLY

    missing_must = sorted(
        (r for r in must if r.status == MISSING),
        key=lambda r: _effective_weight(r, config),
        reverse=True,
    )

    return MatchResult(
        score=round(score, 1) if score is not None else None,
        band=band,
        apply_recommendation=recommendation,
        must_coverage=round(must_cov, 3) if must_cov is not None else None,
        nice_coverage=round(nice_cov, 3) if nice_cov is not None else None,
        over_qualified=over_qualified,
        missing_must_haves=[r.text for r in missing_must if r.text],
        unverified=unverified,
        met_count=sum(1 for r in known if r.status == MET),
        known_count=len(known),
    )
