"""On-demand judge for a deterministic match assessment (issue #60).

The judge is an *auditor*, not a scorer: it returns verdicts on the requirement set and on
the groundedness of the narrative claims, and the deterministic engine re-scores the
corrected set. A given verdict set therefore always produces the same number.

Boundaries (see AGENTS.md §10.2 "deterministic-first"):

- never called during discovery or bulk scans — opt-in, one job at a time;
- never invents requirements (``missing_requirements`` is reported, never scored);
- a failing or malformed LLM response is non-fatal: the assessment is returned unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from backend.config import SCORE_JUDGE_MAX_ROUNDS
from backend.llm_queue import LLMPriority

logger = logging.getLogger("score_judge")

MAX_JD_CHARS = 12000
MAX_RESUME_CHARS = 6000
MAX_CLAIMS = 12

Verdict = Literal["valid", "not_a_requirement", "wrong_category", "wrong_status"]
Category = Literal["must_have", "nice_to_have"]
Status = Literal["met", "partial", "missing"]


class RequirementVerdict(BaseModel):
    """One requirement the judge examined."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(default="", description="Requirement text, copied verbatim from the input")
    verdict: Verdict = Field(default="valid")
    corrected_category: Optional[Category] = Field(
        default=None, description="Only for verdict=wrong_category"
    )
    corrected_status: Optional[Status] = Field(
        default=None, description="Only for verdict=wrong_status"
    )
    reason: str = Field(default="", description="One short sentence citing the JD/resume evidence")


class GroundednessCheck(BaseModel):
    """Whether one narrative claim is traceable to the JD or the resume."""

    model_config = ConfigDict(extra="ignore")

    claim: str = Field(default="")
    grounded: bool = Field(default=True)
    reason: str = Field(default="")


class ScoreAudit(BaseModel):
    """Structured verdict from the scoring judge."""

    model_config = ConfigDict(extra="ignore")

    requirements: List[RequirementVerdict] = Field(
        default_factory=list,
        description="Only the requirements that are NOT valid; omitted means valid",
    )
    missing_requirements: List[str] = Field(
        default_factory=list,
        description="Real JD requirements absent from the assessment (short phrases only)",
    )
    groundedness: List[GroundednessCheck] = Field(default_factory=list)
    score_opinion: Literal["agree", "too_high", "too_low", "unreliable"] = Field(
        default="agree",
        description="unreliable when the requirement list is too sparse to judge the score",
    )
    critique: str = Field(default="")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


def assessment_hash(assessment) -> str:
    """Stable fingerprint of a requirement set, used as the audit cache key."""
    items = [
        [r.text, r.kind, r.category, r.status]
        for r in getattr(assessment, "requirements", []) or []
    ]
    blob = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _requirement_lines(assessment) -> str:
    lines = []
    for i, r in enumerate(getattr(assessment, "requirements", []) or [], start=1):
        lines.append(f"{i}. [{r.category}/{r.status}] {r.text}")
    return "\n".join(lines) or "(none)"


def build_audit_prompt(
    assessment,
    jd: str,
    resume_text: str,
    claims: Optional[Sequence[str]] = None,
    scored: Optional[Dict[str, Any]] = None,
) -> str:
    """Assemble the user prompt: JD, resume, scored requirements, the score, and claims."""
    claim_lines = "\n".join(f"- {c}" for c in (claims or [])[:MAX_CLAIMS]) or "(none)"
    if scored:
        score_block = (
            f"score={scored.get('score', 0):.1f}  band={scored.get('band', '')}  "
            f"must_coverage={scored.get('must_coverage', 0):.2f} "
            f"({scored.get('must_count', 0)} must-have requirements)  "
            f"nice_coverage={scored.get('nice_coverage', 0):.2f} "
            f"({scored.get('nice_count', 0)} nice-to-have requirements)\n"
        )
    else:
        score_block = "(not provided)\n"
    return (
        "JOB DESCRIPTION:\n"
        + (jd or "")[:MAX_JD_CHARS]
        + "\n\nRESUME TEXT:\n"
        + (resume_text or "")[:MAX_RESUME_CHARS]
        + "\n\nWHAT THE SCORER PRODUCED:\n"
        + score_block
        + "\nREQUIREMENTS THE SCORER EXTRACTED:\n"
        + _requirement_lines(assessment)
        + "\n\nCLAIMS THE SCORER MADE ABOUT THIS CANDIDATE:\n"
        + claim_lines
        + "\n\nAudit the requirements, the score and the claims. Return JSON only."
    )


def audit_assessment(
    assessment,
    jd: str,
    resume_text: str,
    claims: Optional[Sequence[str]] = None,
    provider: Optional[str] = None,
    priority: LLMPriority = LLMPriority.ON_DEMAND,
    scored: Optional[Dict[str, Any]] = None,
) -> Optional[ScoreAudit]:
    """Run one audit round. Returns ``None`` when the LLM is unavailable or misbehaves."""
    if not getattr(assessment, "requirements", None):
        return None
    from backend.ai_helper import generate_structured
    from backend.prompts import SCORE_AUDIT_SYSTEM

    try:
        return generate_structured(
            schema=ScoreAudit,
            system_prompt=SCORE_AUDIT_SYSTEM,
            user_prompt=build_audit_prompt(assessment, jd, resume_text, claims, scored=scored),
            provider=provider,
            temperature=0.0,
            max_tokens=2000,
            priority=priority,
            task_type="match_audit",
            task_name="Score Audit",
        )
    except Exception as exc:  # noqa: BLE001 - the audit is a read-only gate
        logger.warning("Score audit failed (%s); keeping the deterministic assessment.", exc)
        return None


def _normalise(text: str) -> str:
    return " ".join((text or "").lower().split())


def apply_audit(assessment, audit: ScoreAudit, allow_drops: bool = False):
    """Apply verdicts to the requirement set. Pure: returns a new assessment.

    ``missing_requirements`` is deliberately ignored — the judge may report requirements the
    scorer missed, but it may never add them, so a score can only lose invented requirements
    or have real ones re-labelled.

    ``not_a_requirement`` is **report-only unless ``allow_drops``**. Removing a requirement can
    only raise must-have coverage, so an over-eager judge inflates scores by deleting real skills
    — measured on jobs 339/820/983, where it dropped Machine Learning, LLM, Git and GitHub. The
    verdict is still returned for a human to see; it just does not mutate the score by default.
    When drops are allowed they are honoured only for ``kind="skill"``: years, seniority and
    education come from JD parsing rather than free text, so calling them "prose" is a mistake.
    """
    if not audit:
        return assessment
    verdicts: Dict[str, RequirementVerdict] = {}
    for v in audit.requirements:
        key = _normalise(v.text)
        if key:
            verdicts[key] = v

    kept = []
    for req in getattr(assessment, "requirements", []) or []:
        v = verdicts.get(_normalise(req.text))
        if v is None or v.verdict == "valid":
            kept.append(req)
            continue
        if v.verdict == "not_a_requirement":
            if not allow_drops or req.kind != "skill":
                kept.append(req)
            continue
        update: Dict[str, Any] = {}
        if v.verdict == "wrong_category" and v.corrected_category:
            update["category"] = v.corrected_category
        if v.verdict == "wrong_status" and v.corrected_status:
            update["status"] = v.corrected_status
        kept.append(req.model_copy(update=update) if update else req)
    return assessment.model_copy(update={"requirements": kept})


def rescore(
    assessment,
    parsed_resume: Optional[dict],
    jd: str,
    job_title: str = "",
    config=None,
) -> dict:
    """Recompute features and the band from a (possibly corrected) assessment. No LLM."""
    from backend.match_classifier import (
        build_jd_hints,
        extract_match_features,
        get_classifier,
    )
    from backend.match_scoring import ScoringConfig, band_and_recommendation

    config = config or ScoringConfig()
    hints = build_jd_hints(jd, job_title)
    features = extract_match_features(parsed_resume, hints, assessment=assessment)
    score = float(get_classifier().predict(features))
    gated = features.must_count > 0 and features.must_coverage < config.must_floor
    band, recommendation = band_and_recommendation(
        score, config, gated=gated, over_qualified=features.over_qualified_flag > 0.5
    )
    return {
        "score": score,
        "band": band,
        "recommendation": recommendation,
        "must_coverage": features.must_coverage,
        "nice_coverage": features.nice_coverage,
        "must_count": features.must_count,
        "nice_count": features.nice_count,
    }


def validate_score(
    assessment,
    parsed_resume: Optional[dict],
    jd: str,
    resume_text: str = "",
    job_title: str = "",
    claims: Optional[Sequence[str]] = None,
    max_rounds: Optional[int] = None,
    provider: Optional[str] = None,
    config=None,
    allow_drops: bool = False,
) -> dict:
    """Audit → deterministic re-score → re-audit, bounded by ``SCORE_JUDGE_MAX_ROUNDS``.

    Returns a dict with the initial and final score, the requirement list actually used, the
    last audit, and how many rounds ran. When no LLM is available the assessment is returned
    unchanged with ``audit=None`` and ``rounds=0``.
    """
    rounds_allowed = SCORE_JUDGE_MAX_ROUNDS if max_rounds is None else max_rounds
    before = rescore(assessment, parsed_resume, jd, job_title, config)
    current = assessment
    audit: Optional[ScoreAudit] = None
    rounds = 0
    corrected_last_round = False

    while rounds <= rounds_allowed:
        audit = audit_assessment(
            current,
            jd,
            resume_text,
            claims=claims,
            provider=provider,
            scored=before,
        )
        rounds += 1
        if audit is None:
            break
        corrected = apply_audit(current, audit, allow_drops=allow_drops)
        corrected_last_round = assessment_hash(corrected) != assessment_hash(current)
        current = corrected
        if not corrected_last_round:
            break

    after = rescore(current, parsed_resume, jd, job_title, config)
    return {
        "audit": audit,
        "assessment": current,
        "score_before": before["score"],
        "score_after": after["score"],
        "before": before,
        "after": after,
        "rounds": rounds,
        "converged": audit is not None and not corrected_last_round,
        "assessment_hash": assessment_hash(current),
    }
