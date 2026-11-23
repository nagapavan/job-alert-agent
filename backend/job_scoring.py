"""The resume <-> job scoring pipeline: deterministic first, bounded LLM fallback second.

Extracted from ``main.py`` so the discovery, LinkedIn, Gmail and rescore paths can share one
implementation without importing the app module (which would be a cycle). Every scoring entry point
goes through ``score_job_with_engine``; the deterministic tier runs in milliseconds with no LLM call,
and the LLM extractor is only reached for a substantive JD the deterministic extractor cannot use.

Names keep their leading underscore: ``main`` re-exports them under the same names, and the existing
tests import them from there.
"""

import datetime
import json
import logging
import re
from collections import namedtuple
from typing import Any, List, Optional

from sqlalchemy.orm import Session

from backend.ai_helper import MIN_JOB_DESCRIPTION_CHARS, assess_job_requirements
from backend.config import (
    DETERMINISTIC_BULK_SCORING,
    LLM_INTERACTIVE_TIMEOUT,
    decrypt_data,
)
from backend.database import Resume, UserPreference, get_scoring_config, get_user_preferences
from backend.match_scoring import ScoringConfig

logger = logging.getLogger(__name__)


ScoringResult = namedtuple(
    "ScoringResult",
    ["score", "analysis", "scored", "method", "timestamp", "band", "recommendation",
     "strengths", "gaps", "partials"],
)

def _normalize_extra_skills(raw: Any) -> List[str]:
    """Dedupe/trim a raw extra-skills payload (list or JSON) case-insensitively."""
    items = raw if isinstance(raw, list) else []
    seen: set = set()
    out: List[str] = []
    for s in items:
        token = str(s).strip()
        key = token.lower()
        if token and key not in seen:
            seen.add(key)
            out.append(token)
    return out


def get_extra_skills(pref: UserPreference) -> List[str]:
    """Reads the user-owned extra-skills list from preferences JSON (deduped, trimmed)."""
    try:
        raw = json.loads(pref.extra_skills_json or "[]")
    except Exception:
        raw = []
    return _normalize_extra_skills(raw)


def _merge_extra_skills(parsed_resume: Optional[dict], extra_skills: List[str]) -> Optional[dict]:
    """Merges the user-owned extra skills on top of the resume-derived skill list.

    Returns a shallow copy (never mutates the caller's dict). Case-insensitive dedupe so a
    skill the resume already covers is not double-counted by the scorer.
    """
    existing = [str(s) for s in ((parsed_resume or {}).get("skills") or [])]
    seen = {s.lower() for s in existing}
    additions: List[str] = []
    for s in extra_skills or []:
        token = (s or "").strip()
        key = token.lower()
        if token and key not in seen:
            seen.add(key)
            additions.append(token)
    merged = dict(parsed_resume or {})
    merged["skills"] = existing + additions
    return merged

def _derive_strengths_gaps(assessment, match=None) -> tuple:
    """Derive the strengths/gaps/partials bullet lists from the requirement breakdown.

    Returns ``(strengths, gaps, partials)``:
    - strengths: requirements whose status is ``met`` (top up to 4 with partials so the
      panel is never empty when the resume meets at least some requirements).
    - gaps: must-have requirements with status ``missing`` (truly absent from the resume).
    - partials: must-have requirements with status ``partial`` (resume has *some*
      evidence but not full coverage — e.g. "10+ years" when the candidate has fewer).
      Surfaced separately so a moderate score is explainable without polluting the
      Skills Insights aggregation (which only counts truly-missing skills).

    ``match`` is optional; missing must-haves are derived from the assessment when it is
    omitted (used by the deterministic classifier path).
    """
    reqs = assessment.requirements
    strengths = [r.text for r in reqs if r.status == "met" and r.text]
    partials = [
        r.text
        for r in reqs
        if r.category == "must_have" and r.status == "partial" and r.text
    ]
    # Case-insensitive helper so JD-text duplicates that differ only in case are treated
    # as the same requirement.
    def _norm(s: str) -> str:
        return s.strip().lower()

    seen_strengths = {_norm(s) for s in strengths}
    # Pad strengths to 4 with partials first, so the padding items are removed from the
    # partials list afterward (an item never appears in BOTH panels).
    if len(strengths) < 4:
        for p in partials:
            if len(strengths) >= 4:
                break
            if _norm(p) not in seen_strengths:
                seen_strengths.add(_norm(p))
                strengths.append(p)
    strengths = list(dict.fromkeys(strengths))[:4]  # case-sensitive dedupe + hard cap
    # Now build partials: drop anything already claimed by strengths (case-insensitive),
    # and dedupe among themselves.
    seen_partials: set = set()
    unique_partials: List[str] = []
    for p in partials:
        key = _norm(p)
        if key not in seen_strengths and key not in seen_partials:
            seen_partials.add(key)
            unique_partials.append(p)
    partials = unique_partials[:4]
    if match is not None:
        gaps = list(match.missing_must_haves)
    else:
        gaps = [
            r.text
            for r in reqs
            if r.category == "must_have" and r.status == "missing" and r.text
        ]
    for req in reqs:
        if req.status == "missing" and req.text and req.text not in gaps:
            gaps.append(req.text)
    return strengths[:4], gaps[:3], partials[:4]


def _parsed_resume_of(resume):
    """Decrypt and parse a resume's structured JSON, or None."""
    if not (resume and getattr(resume, "parsed_json_encrypted", None)):
        return None
    try:
        return json.loads(decrypt_data(resume.parsed_json_encrypted))
    except Exception:
        return None


def _active_resume_row(db: Session):
    return (
        db.query(Resume)
        .filter(Resume.is_active == True)  # noqa: E712 - ORM filter
        .order_by(Resume.created_at.desc())
        .first()
    )


def _score_provenance(db: Session) -> dict:
    """The resume version a score computed now belongs to (issue #56).

    Returns kwargs for ``Job(...)`` / setattr on an existing row. Empty when there is no resume,
    so an unscored row is never given provenance it does not have.
    """
    resume = _active_resume_row(db)
    if not resume:
        return {}
    return {
        "scored_resume_id": resume.id,
        "scored_resume_fingerprint": resume.fingerprint,
    }


_JD_CUE_RE = re.compile(
    r"\b(experience|responsibilit\w*|qualification\w*|requirements?|skills?|"
    r"proficien\w*|knowledge|you(?:'ll| will)|we(?:'re| are) looking for|"
    r"years? of|degree|what you(?:'ll| will)|about (?:the|this) role)\b",
    re.IGNORECASE,
)


def _looks_like_job_description(text: str) -> bool:
    """Substantive JDs carry role/qualification cues; discovery artifacts (LinkedIn
    alert/saved snippets, Gmail recruiter lines, truncated link previews) do not, so
    they should not consume a bounded LLM fallback call."""
    return bool(_JD_CUE_RE.search(text or ""))


def _deterministic_analysis_text(det) -> str:
    """Compact strengths/gaps/partials narrative for the deterministic classifier path."""
    strengths, gaps, partials = det.get("strengths") or [], det.get("gaps") or [], det.get("partials") or []
    parts = []
    if strengths:
        parts.append(f"Strengths: {', '.join(strengths)}")
    if partials:
        parts.append(f"Partial coverage: {', '.join(partials)}")
    if gaps:
        parts.append(f"Gaps: {', '.join(gaps)}")
    if parts:
        return ". ".join(parts)
    return det["feedback"]


def _score_job_with_engine(
    db, resume_text, description, parsed_resume=None, job_title=""
) -> ScoringResult:
    """Score a discovered job: deterministic engine first, bounded LLM fallback.

    Returns a :class:`ScoringResult` namedtuple ``(score, analysis, scored, method, timestamp,
    band, recommendation, strengths, gaps, partials)``. Failures degrade to an unscored baseline
    rather than fabricating a score.
    """
    unscored = ScoringResult(0, "", False, None, None, None, None, [], [], [])
    if not (resume_text or "").strip() or not (description or "").strip():
        return unscored
    if len(description.strip()) < MIN_JOB_DESCRIPTION_CHARS:
        # No substantive job description -> do not fabricate a score.
        return unscored

    # The user-owned extra-skills profile is evidence on top of the resume: every scoring
    # path (deterministic + LLM fallback) reads parsed_resume, so merge once here.
    try:
        extra = get_extra_skills(get_user_preferences(db))
    except Exception:
        extra = []
    if extra:
        parsed_resume = _merge_extra_skills(parsed_resume, extra)

    det_errored = False
    det_match_details = None
    if DETERMINISTIC_BULK_SCORING:
        try:
            det_match_details = _deterministic_match(
                db, resume_text, description, parsed_resume=parsed_resume, job_title=job_title
            )
        except Exception as det_err:
            logger.warning(
                f"Deterministic scoring errored ({det_err}); falling back to the LLM engine."
            )
            det_errored = True
            det_match_details = None
    if det_match_details:
        return ScoringResult(
            score=det_match_details["score"],
            analysis=_deterministic_analysis_text(det_match_details),
            scored=True,
            method=det_match_details["method"],
            timestamp=datetime.datetime.now(datetime.timezone.utc),
            band=det_match_details["band"],
            recommendation=det_match_details["recommendation"],
            strengths=list(det_match_details.get("strengths") or []),
            gaps=list(det_match_details.get("gaps") or []),
            partials=list(det_match_details.get("partials") or []),
        )

    # A deterministic *error* always falls back (per requirement); a clean "no requirements"
    # result on a non-JD discovery artifact does not, so it does not burn an LLM call.
    if not det_errored and not _looks_like_job_description(description):
        # Applies with the deterministic path off too: a discovery artifact must not burn a
        # bounded LLM call (finding 91).
        return unscored

    # Bounded LLM fallback for signal-less JDs the deterministic extractor cannot use.
    try:
        assessment, match = assess_job_requirements(
            resume_text,
            description,
            parsed_resume=parsed_resume,
            scoring_config=ScoringConfig.from_mapping(get_scoring_config(db)),
            timeout=LLM_INTERACTIVE_TIMEOUT,
        )
    except Exception as e:
        logger.warning(f"Match scoring skipped: {e}")
        return unscored

    if match.score is None or not assessment.requirements:
        return ScoringResult(
            score=0,
            analysis=(assessment.summary if assessment else ""),
            scored=False,
            method=None,
            timestamp=None,
            band=None,
            recommendation=None,
            strengths=[],
            gaps=[],
            partials=[],
        )
    l_strengths, l_gaps, l_partials = _derive_strengths_gaps(assessment, match)
    return ScoringResult(
        score=match.score,
        analysis=_match_analysis_text(assessment, match),
        scored=True,
        method=match.method,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        band=match.band,
        recommendation=match.apply_recommendation,
        strengths=list(l_strengths),
        gaps=list(l_gaps),
        partials=list(l_partials),
    )

def _match_analysis_text(assessment, match) -> str:
    strengths, gaps, partials = _derive_strengths_gaps(assessment, match)
    parts = []
    if strengths:
        parts.append(f"Strengths: {', '.join(strengths)}")
    if partials:
        parts.append(f"Partial coverage: {', '.join(partials)}")
    if gaps:
        parts.append(f"Gaps: {', '.join(gaps)}")
    if parts:
        return ". ".join(parts)
    return assessment.summary or ""

def _deterministic_match(
    db,
    resume_text: str,
    description: str,
    parsed_resume: dict | None = None,
    job_title: str = "",
    location: str = "",
) -> dict | None:
    """Deterministic requirement+classifier match. None when no usable requirements."""
    # FAST PATH: deterministic requirement extraction + ML/heuristic classifier.
    # Runs in milliseconds with no LLM call, so bulk ATS scoring does not saturate
    # the LLM queue. Falls through to the LLM extractor only if it cannot produce
    # a usable assessment.
    from backend.match_classifier import (
        CLASSIFIER,
        build_deterministic_assessment,
        build_jd_hints,
        extract_match_features,
    )
    from backend.match_scoring import band_and_recommendation

    try:
        # Deterministic requirements so must/nice coverage is real (no LLM).
        assessment = build_deterministic_assessment(
            description,
            parsed_resume,
            resume_text=resume_text,
            job_title=job_title or "",
        )
        if not assessment.requirements:
            return None

        known = [r for r in assessment.requirements if r.status in ("met", "partial", "missing")]
        if not known:
            return None

        jd_hints = build_jd_hints(description, job_title or "")
        features = extract_match_features(
            parsed_resume, jd_hints, assessment=assessment
        )
        score = CLASSIFIER.predict(features)

        scoring_config = ScoringConfig.from_mapping(get_scoring_config(db))
        # Same must-have floor as compute_match: a posting the candidate only half-qualifies
        # for must not present as a high score off the secondary components (issue #108).
        gated = (
            features.must_count > 0
            and features.must_coverage < scoring_config.must_floor
        )
        if gated:
            score = min(score, scoring_config.cap_when_below_floor)
        over_qualified = features.over_qualified_flag > 0.5
        band, recommendation = band_and_recommendation(
            score, scoring_config, gated=gated, over_qualified=over_qualified
        )

        eligible, ineligibility_reason = _check_eligibility(db, job_title, location, description)

        # Surface the requirement breakdown so the score is transparent, not a black box.
        strengths, gaps, partials = _derive_strengths_gaps(assessment)
        known_reqs = [
            r
            for r in assessment.requirements
            if r.status in ("met", "partial", "missing")
        ]
        met_count = sum(1 for r in known_reqs if r.status == "met")
        unverified = [
            r.text
            for r in assessment.requirements
            if r.status not in ("met", "partial", "missing") and r.text
        ]
        # An empty nice-to-have set means the JD states none (issue #70); reporting it as
        # "0%" reads as "you met none of them", which is the opposite of what happened.
        coverage_bits = [f"must-have coverage {round(features.must_coverage * 100)}%"]
        if features.nice_count > 0:
            coverage_bits.append(f"nice-to-have {round(features.nice_coverage * 100)}%")
        else:
            coverage_bits.append("no nice-to-have requirements")
        feedback = (
            f"Matched {met_count} of {len(known_reqs)} requirements "
            f"({', '.join(coverage_bits)})."
        )
        # No partial-coverage sentence here: the count would have to match the panel's
        # `partials` list (must-have only, capped, minus entries padded into strengths), and
        # any "see below" wording is wrong in at least one of the two renderers.
        # Surface technology-tenure comparisons so the user can judge borderline cases.
        tenure_notes = []
        for r in assessment.requirements:
            if (
                r.kind != "experience_years"
                or r.scope != "technology"
                or not r.technology
            ):
                continue
            need = (
                f"{r.min_years:g}+ yrs"
                if r.min_years is not None
                else "the required experience"
            )
            if r.evidence == "unconfirmed":
                tenure_notes.append(
                    f"{r.technology}: requires {need}, but the resume does not state the tenure "
                    f"— not enough information to confirm; please decide"
                )
            elif r.evidence == "not found":
                tenure_notes.append(
                    f"{r.technology}: requires {need}, but no evidence was found in the resume"
                )
            else:
                tenure_notes.append(
                    f"{r.technology}: requires {need}, resume {r.evidence} ({r.status})"
                )
        if tenure_notes:
            feedback += " Tenure check — " + "; ".join(tenure_notes) + "."
        return {
            "assessment": assessment,
            "features": features,
            "score": score,
            "band": band,
            "recommendation": recommendation,
            "must_coverage": features.must_coverage,
            "nice_coverage": features.nice_coverage,
            "over_qualified": over_qualified,
            "strengths": strengths,
            "gaps": gaps,
            "partials": partials,
            "unverified": unverified,
            "feedback": feedback,
            "eligible": eligible,
            "ineligibility_reason": ineligibility_reason,
            "method": "classifier_v1",
            "score_method": "classifier_v1",
        }

    except Exception as classifier_err:
        # Re-raise so the caller can mark this a *deterministic error* (det_errored) and
        # still fall back to the LLM. Swallowing it here made a genuine bug
        # indistinguishable from "no requirements" (finding 86).
        logger.warning(
            f"Classifier-based scoring failed: {classifier_err}; falling back to LLM"
        )
        raise

def _check_eligibility(db, job_title: str, location: str, description: str) -> tuple[bool, Optional[str]]:
    """Checks if the job is eligible based on user preferences."""
    try:
        # Evaluate even when the posting carries no location: title, skip-topic and work-mode
        # filters still apply, and a blank location must not exempt a posting (finding 95).
        from backend.preference_filter import PreferenceFilter

        reason = PreferenceFilter.from_db(db).rejection_reason(
            job_title, location, description
        )
        if reason:
            return False, reason
    except Exception as e:
        logger.debug(f"Eligibility check skipped: {e}")
    return True, None

