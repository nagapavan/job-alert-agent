"""Deterministic job-match scorer.

Maps the features derived from a requirement assessment (coverage of must-haves and
nice-to-haves, seniority, education, tenure, over-qualification) to a 0-100 score with
fixed weights. No model file, no training step, no per-user state: the same JD and resume
always produce the same score, which is what makes the stored score reproducible and the
audit in ``score_judge`` able to re-derive it.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional, Tuple, List

from backend import section_intent

logger = logging.getLogger("match_classifier")

@dataclass
class MatchFeatures:
    """Input features for the classifier (all deterministic, no LLM)."""

    must_coverage: float  # 0-1: % of must-haves met
    nice_coverage: float  # 0-1: % of nice-to-haves met
    seniority_fit: float  # 0-1: candidate seniority matches job level
    education_match: float  # 0-1: education requirements met
    experience_years_delta: float  # normalized experience gap (-1 to 1)
    over_qualified_flag: float  # 1 if over-qualified, 0 otherwise
    must_count: int  # absolute must-have count
    nice_count: int  # absolute nice-to-have count
    # Presence flags: a component the JD does not define must leave the denominator rather
    # than contribute a constant. Trailing defaults keep existing positional construction valid.
    seniority_known: bool = True
    education_mentioned: bool = True


class DeterministicClassifier:
    """Weighted rule-based scoring over the assessment features (no model, no ML)."""

    def predict(self, features: MatchFeatures) -> float:
        """Return match score 0-100 based on requirement coverage and fit."""
        # Weighted combination of signals, renormalised over the components the JD actually
        # defines (issue #70). An empty requirement set means "this JD states none", not "the
        # candidate met none", so scoring it as 0% would silently cap the score. Must-haves
        # dominate; nice-to-haves matter; seniority and education are secondary.
        # A component the JD leaves undefined (no education requirement, unparseable
        # seniority) is excluded instead of scoring a constant (issue #108).
        components = [
            (features.must_coverage, 60, features.must_count > 0),
            (features.nice_coverage, 20, features.nice_count > 0),
            (features.seniority_fit, 15, features.seniority_known),
            (features.education_match, 5, features.education_mentioned),
        ]
        # With neither requirement set present there is nothing to verify, so keep the legacy
        # full-scale formula and let the score stay low rather than renormalise over 20 points.
        if features.must_count == 0 and features.nice_count == 0:
            components = [
                (features.must_coverage, 60, True),
                (features.nice_coverage, 20, True),
                (features.seniority_fit, 15, features.seniority_known),
                (features.education_match, 5, features.education_mentioned),
            ]
        total_weight = sum(weight for _, weight, present in components if present)
        if total_weight <= 0:
            return 0.0
        score = (
            sum(value * weight for value, weight, present in components if present)
            / total_weight
            * 100.0
        )
        score = min(100.0, max(0.0, score))

        # Penalize experience mismatch (too junior or too senior)
        if features.experience_years_delta < -0.3:  # Too junior
            score *= 0.8
        elif features.over_qualified_flag > 0.5:  # Over-qualified (flagged as advisory)
            pass  # Don't penalize; let user decide

        return score


def build_jd_hints(description: str, job_title: str = "") -> dict:
    """JD-side hints consumed by ``extract_match_features`` (years bounds, seniority, education).

    Single implementation shared by live scoring and the scoring judge, so an audit always
    re-scores against the same JD interpretation as the original score.
    """
    from backend import jd_signals

    exp_hints = jd_signals.parse_experience(description or "")
    min_years = max(
        (h.get("min_years") for h in exp_hints if h.get("min_years")), default=None
    )
    max_years = max(
        (h.get("max_years") for h in exp_hints if h.get("max_years")), default=None
    )
    return {
        "min_years": min_years or 0,
        "max_years": max_years or 100,
        "seniority_level": jd_signals.seniority_level(job_title or ""),
        "education_mentioned": bool(jd_signals.parse_education(description or "")),
    }


def extract_match_features(
    parsed_resume: Optional[dict],
    jd_hints: dict,
    assessment,
) -> MatchFeatures:
    """
    Extract deterministic features from resume, JD signals, and assessment.

    All computation is deterministic (no LLM); uses results from jd_signals.py
    and resume_signals.py.
    """
    from backend import jd_signals, resume_signals

    # Experience signals
    candidate_years = (
        resume_signals.estimate_total_years(parsed_resume) if parsed_resume else 0
    ) or 0  # Handle None from estimate_total_years
    job_min_years = float(jd_hints.get("min_years", 0) or 0)
    job_max_years = float(jd_hints.get("max_years", 100) or 100)

    # Normalize experience delta: -1 (too junior) to +1 (over-qualified)
    if candidate_years < job_min_years:
        exp_delta = -1.0 + (candidate_years / max(job_min_years, 1))
    elif candidate_years > job_max_years:
        exp_delta = min(1.0, (candidate_years - job_max_years) / 20.0)  # Cap at 1
    else:
        exp_delta = 0.0

    # Seniority fit: rank distance, not equality. A senior candidate against a staff role is
    # a near miss, not the same as a junior candidate (issue #108). Unparseable either side is
    # excluded from scoring rather than scored as a constant.
    candidate_seniority = (
        resume_signals.latest_seniority(parsed_resume) if parsed_resume else "unknown"
    )
    job_seniority = jd_hints.get("seniority_level", "unknown")
    cand_rank = _SENIORITY_RANK.get(str(candidate_seniority or "").lower())
    job_rank = _SENIORITY_RANK.get(str(job_seniority or "").lower())
    seniority_known = cand_rank is not None and job_rank is not None
    if not seniority_known:
        seniority_fit = 0.0
    else:
        # Over-level is a full fit; each level short costs 0.15 (senior -> staff = 0.70),
        # floored at 0. The old 1.0/0.6 split could not tell a near miss from a large gap.
        seniority_fit = max(0.0, 1.0 - 0.15 * max(0, job_rank - cand_rank))

    # Education: read the education requirement itself. The JD mentioning education is not
    # evidence the candidate meets it, and a JD that mentions none must not contribute a
    # constant (issue #108).
    education_match = 0.0
    education_mentioned = False

    # Requirement coverage (from assessment if available)
    must_coverage = 0.0
    nice_coverage = 0.0
    must_count = 0
    nice_count = 0
    over_qualified = 0.0

    if assessment and hasattr(assessment, "requirements"):
        from backend.match_scoring import CREDIT, KNOWN_STATUSES

        reqs = assessment.requirements or []
        must_reqs = [r for r in reqs if r.category == "must_have"]
        nice_reqs = [r for r in reqs if r.category == "nice_to_have"]

        # Only judgeable requirements count toward the must/nice totals and coverage: an
        # all-unknown posting is not a zero-coverage posting, so it must not trip the
        # must-have floor (finding 87).
        known_must = [r for r in must_reqs if r.status in KNOWN_STATUSES]
        known_nice = [r for r in nice_reqs if r.status in KNOWN_STATUSES]
        must_count = len(known_must)
        nice_count = len(known_nice)

        # One coverage implementation (finding 88): delegate to match_scoring so weights
        # cannot diverge between the classifier features and compute_match.
        from backend.match_scoring import ScoringConfig, _coverage as _scoring_coverage

        must_cov = _scoring_coverage(known_must, ScoringConfig())
        nice_cov = _scoring_coverage(known_nice, ScoringConfig())
        must_coverage = must_cov or 0.0
        nice_coverage = nice_cov or 0.0

        edu_reqs = [r for r in reqs if getattr(r, "kind", "") == "education"]
        if edu_reqs:
            known_edu = [r for r in edu_reqs if r.status in KNOWN_STATUSES]
            if known_edu:
                education_mentioned = True
                education_match = sum(CREDIT[r.status] for r in known_edu) / len(known_edu)

        over_qualified = 1.0 if getattr(assessment, "over_qualified", False) else 0.0

    return MatchFeatures(
        must_coverage=must_coverage,
        nice_coverage=nice_coverage,
        seniority_fit=seniority_fit,
        education_match=education_match,
        experience_years_delta=exp_delta,
        over_qualified_flag=over_qualified,
        must_count=must_count,
        nice_count=nice_count,
        seniority_known=seniority_known,
        education_mentioned=education_mentioned,
    )


# ---------------------------------------------------------------------------
# Deterministic requirement extraction (no LLM)
# Builds the same RequirementAssessment shape the LLM produces, using only the
# JD text + parsed resume, so must/nice coverage can be computed without an LLM.
# ---------------------------------------------------------------------------

# Skill vocabulary, aliases and concept groups come from the curated taxonomy
# (data/skill_taxonomy.json) so the vocabulary can grow without code changes.
from backend import skill_taxonomy as _taxonomy
from backend.skill_taxonomy import (
    alias_surfaces as _alias_surfaces,
    normalize_skill as _normalize_skill,
)

# Higher rank satisfies a lower education requirement (Masters meets Bachelor's, not vice versa).
_EDU_RANK = {"high_school": 0, "associate": 1, "bachelor": 2, "master": 3, "phd": 4}

_SENIORITY_RANK = {
    "intern": 0, "junior": 1, "mid": 2, "senior": 3, "lead": 4,
    "staff": 5, "principal": 6, "manager": 7, "director": 8,
}

# A job title is not a skill requirement. Residual mining can credit the posting's own title
# (e.g. "Senior Staff Engineer") whenever the resume contains it, which both adds a bogus
# requirement and double-counts seniority (issue #108).
_TITLE_MODIFIERS = {
    "senior", "sr", "staff", "principal", "lead", "junior", "jr", "mid", "associate",
    "chief", "head", "vp", "director", "manager", "entry", "level", "i", "ii", "iii", "iv",
}
_ROLE_NOUNS = {
    "engineer", "engineering", "developer", "architect", "manager", "director", "scientist",
    "analyst", "consultant", "specialist", "programmer", "intern", "lead",
}


def _is_title_phrase(phrase: str, job_title: str = "") -> bool:
    """True when a mined phrase is the posting's title or nothing but a role label."""
    norm = _normalize_skill(phrase or "")
    if not norm:
        return True
    if job_title and norm == _normalize_skill(job_title):
        return True
    words = [w for w in re.split(r"[^a-z0-9+#]+", norm) if w]
    if not words:
        return True
    return all(w in _TITLE_MODIFIERS or w in _ROLE_NOUNS for w in words) and any(
        w in _ROLE_NOUNS for w in words
    )


def _skill_token(pattern: str) -> re.Pattern:
    """Boundary that tolerates symbols like C++ / Node.js / CI/CD."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(pattern) + r"(?![A-Za-z0-9])", re.I)


def _skill_patterns(skill: str) -> "List[re.Pattern]":
    """Word-boundary patterns for a skill, including alias surface spellings.

    e.g. for canonical "node.js" this also matches a raw "NodeJS" in free text,
    since resumes/JDs frequently write it without the dot.
    """
    patterns = [_skill_token(skill)]
    patterns += [_skill_token(surface) for surface in _alias_surfaces(skill)]
    return patterns


def _ambiguous_skill_match(text: str, skill: str) -> Optional[re.Match]:
    """Match a skill whose name is also an ordinary English word (issue #50).

    "Workday", "Go", "React" and "Express" are curated taxonomy skills *and* common nouns, so a
    case-insensitive scan turns prose into requirements ("during the workday", "we go fast",
    "you react to change", "we express ourselves"). Such a skill only counts when an occurrence
    carries a capital letter ("Workday integration", "Go developer") or one of its unambiguous
    aliases appears ("golang", "react.js", "expressjs").

    ``text`` must be the original-case JD, not a lowercased copy.
    """
    for match in _skill_token(skill).finditer(text or ""):
        if match.group(0) != match.group(0).lower():
            return match
    for surface in _alias_surfaces(skill):
        match = _skill_token(surface).search(text or "")
        if match:
            return match
    return None


def _resume_skill_scan(resume_text: str) -> "set[str]":
    """Skills detected from raw resume text (union of canonical + alias surfaces)."""
    lowered = (resume_text or "").lower()
    found = set()
    for s in _taxonomy.DISPLAY_SKILLS:
        if any(p.search(lowered) for p in _skill_patterns(s)):
            found.add(_normalize_skill(s))
    return found


def build_deterministic_assessment(
    job_description: str,
    parsed_resume: Optional[dict],
    resume_text: str = "",
    job_title: str = "",
):
    """Build a RequirementAssessment from JD/resume signals without any LLM call.

    Covers the concrete, high-signal requirements (skills, years, seniority,
    education, certifications) that dominate requirement-coverage scoring.
    """
    from backend import jd_signals, resume_signals
    from backend.match_scoring import (
        RequirementAssessment,
        RequirementItem,
        MUST_HAVE,
        NICE_TO_HAVE,
        MET,
        MISSING,
        PARTIAL,
        UNKNOWN,
    )

    jd = job_description or ""
    jd_lower = jd.lower()
    reqs: List[RequirementItem] = []
    resume_skills = {
        _normalize_skill(s) for s in ((parsed_resume or {}).get("skills") or [])
    }
    # Always merge skills detected from the raw resume text. Structured skills can be
    # stale or incomplete (e.g. a language listed in prose rather than the skills list),
    # so the free-text scan keeps the profile current without dropping structured data.
    # The alias-aware scan also catches variant spellings like "NodeJS".
    if resume_text:
        resume_skills |= _resume_skill_scan(resume_text)
    resume_blob = " ".join(
        [resume_text or ""] + [str(s) for s in ((parsed_resume or {}).get("skills") or [])]
    ).lower()

    # Section-aware: split the JD into required / preferred / intro spans using the
    # shared section-intent primitive, then blank out company-intro and perks/offer
    # prose so marketing sentences (e.g. "40-year old C++ code") and benefits
    # paragraphs are not read as requirements. Offsets are preserved by masking, so
    # category lookups stay aligned with the original text.
    spans = section_intent.jd_section_spans(jd)
    intro_spans = [s for s in spans if s.label in ("intro", "boilerplate")]
    jd_scan = section_intent.mask_spans(jd, intro_spans)
    jd_scan_lower = jd_scan.lower()

    def _category_at(offset: int) -> str:
        label = section_intent.label_at(spans, offset)
        return NICE_TO_HAVE if label == "preferred" else MUST_HAVE

    # Parse experience signals first, so a technology that carries a years requirement is
    # not also credited as a plain (presence-only) skill. Scoped past the intro to avoid
    # prose like "40-year old C++ code".
    hints = jd_signals.parse_experience(jd_scan)
    candidate_years = resume_signals.estimate_total_years(parsed_resume)
    tech_hints = [h for h in hints if h.get("scope") == "technology" and h.get("technology")]
    total_hints = [h for h in hints if (h.get("scope") or "total") == "total"]
    if not total_hints and not tech_hints:
        total_hints = hints
    tech_required = {_normalize_skill(h["technology"]) for h in tech_hints}

    seen = set()
    cloud_hits = []
    for skill in _taxonomy.DISPLAY_SKILLS:
        norm = _normalize_skill(skill)
        if norm in seen or norm in tech_required:
            continue
        # Match the display name OR any alias surface (e.g. "golang" -> Go,
        # "genai" -> LLM, "prompt design" -> Prompt Engineering). Skills whose name is also an
        # ordinary word are matched against the original-case text instead, so prose does not
        # become a requirement (issue #50).
        if norm in _taxonomy.AMBIGUOUS_SKILLS:
            m = _ambiguous_skill_match(jd_scan, skill)
        else:
            m = None
            for pattern in _skill_patterns(skill):
                m = pattern.search(jd_scan_lower)
                if m:
                    break
        if not m:
            continue
        seen.add(norm)
        category = _category_at(m.start())
        if norm in resume_skills:
            status = MET
        elif _taxonomy.CONCEPT_GROUPS.get(norm, set()) & resume_skills:
            status = PARTIAL
        else:
            status = MISSING
        if norm in _taxonomy.CLOUD_PROVIDERS:
            cloud_hits.append((skill, category, status))
            continue
        reqs.append(
            RequirementItem(text=skill, kind="skill", category=category, status=status)
        )

    # Collapse "one of AWS/GCP/Azure"-style alternatives into a single requirement so
    # a candidate with any one provider is not penalized for the others.
    if len(cloud_hits) > 1:
        cstatus = MET if any(s == MET for _, _, s in cloud_hits) else (
            PARTIAL if any(s == PARTIAL for _, _, s in cloud_hits) else MISSING
        )
        ccategory = MUST_HAVE if any(c == MUST_HAVE for _, c, _ in cloud_hits) else NICE_TO_HAVE
        names = "/".join(dict.fromkeys(s for s, _, _ in cloud_hits))
        reqs.append(
            RequirementItem(
                text=f"Cloud (one of {names})",
                kind="skill",
                category=ccategory,
                status=cstatus,
            )
        )
    else:
        for s, c, st in cloud_hits:
            reqs.append(RequirementItem(text=s, kind="skill", category=c, status=st))

    # Residual JD units (Tier A/B): phrases the taxonomy does not cover are credited only
    # when the candidate's own resume evidences them; otherwise they are queued for
    # taxonomy review rather than scored (never invent requirements from JD prose).
    try:
        from backend import requirements_extractor

        for item in requirements_extractor.extract_residual_requirements(
            jd, spans, resume_text=resume_text or resume_blob
        ):
            if _is_title_phrase(item["text"], job_title):
                continue
            # A residual the resume evidences at full token overlap is MET, not PARTIAL;
            # partial is only for weaker evidence (finding 96).
            support = float(item.get("score") or 0.0)
            reqs.append(
                RequirementItem(
                    text=item["text"],
                    kind="skill",
                    category=item["category"],
                    status=MET if support >= 0.999 else PARTIAL,
                    evidence=item.get("evidence", ""),
                )
            )
    except Exception as exc:  # noqa: BLE001 - residual mining is best-effort
        logger.debug(f"Residual requirement extraction skipped: {exc}")

    # Technology-scoped years (e.g. "3 years Golang"): the resume confirms presence of the
    # technology but not the exact tenure, so presence earns partial — never a full "met".
    for h in tech_hints[:3]:
        tech = h.get("technology")
        min_y = h.get("min_years")
        tech_years = resume_signals.estimate_technology_years(parsed_resume, tech)
        present = _normalize_skill(tech) in resume_skills or bool(
            re.search(
                r"(?<![A-Za-z0-9])" + re.escape(str(tech).lower()) + r"(?![A-Za-z0-9])",
                resume_blob,
            )
        )
        if tech_years is not None:
            evidence = f"~{tech_years:g} yrs"
            if min_y is None or tech_years >= min_y:
                status = MET
            elif tech_years >= min_y * 0.7:
                status = PARTIAL
            else:
                status = MISSING
        else:
            # Present somewhere in the resume but no dated work history mentions it ->
            # partial, and explicitly flagged as unconfirmable (never a silent "met").
            status = PARTIAL if present else MISSING
            evidence = "unconfirmed" if present else "not found"
        label = f"{min_y:g}+ years {tech}" if min_y is not None else f"{tech} experience"
        reqs.append(
            RequirementItem(
                text=label,
                kind="experience_years",
                category=MUST_HAVE,
                status=status,
                scope="technology",
                technology=tech,
                min_years=min_y,
                max_years=h.get("max_years"),
                evidence=evidence,
            )
        )

    # Total years of experience
    for h in total_hints[:1]:
        min_y = h.get("min_years")
        if min_y is None:
            continue
        if candidate_years is None:
            status = UNKNOWN
        elif candidate_years >= min_y:
            status = MET
        elif candidate_years >= min_y * 0.7:
            status = PARTIAL
        else:
            status = MISSING
        reqs.append(
            RequirementItem(
                text=(h.get("snippet") or f"{min_y:g}+ years experience"),
                kind="experience_years",
                category=MUST_HAVE,
                status=status,
                scope="total",
                min_years=min_y,
                max_years=h.get("max_years"),
            )
        )

    # Seniority level. Kept as a requirement (not only the 15-point feature) so the score
    # audit and the strengths/partials panels can see it; the feature uses a rank gradient
    # while the requirement keeps the met/partial/missing vocabulary the judge understands.
    job_sen = jd_signals.seniority_level(job_title or "")
    cand_sen = resume_signals.latest_seniority(parsed_resume)
    if job_sen and job_sen != "unknown":
        # An unreadable resume (no parseable roles) is not a senior. Do not let the
        # `_SENIORITY_RANK` default turn "unknown" into a silent MET.
        if not cand_sen or cand_sen == "unknown" or cand_sen not in _SENIORITY_RANK:
            status = UNKNOWN
        else:
            jr = _SENIORITY_RANK.get(job_sen, 3)
            cr = _SENIORITY_RANK.get(cand_sen, 3)
            status = MET if cr >= jr else (PARTIAL if cr == jr - 1 else MISSING)
        reqs.append(
            RequirementItem(
                text=f"{job_sen} level", kind="seniority", category=MUST_HAVE, status=status
            )
        )

    # Education — a higher degree satisfies a lower requirement, never the reverse.
    education = jd_signals.parse_education(jd)
    if education:
        # "Bachelor's or Master's" / "BS/MS/PhD" lists alternatives -> the lowest listed
        # level is the requirement; a higher degree still satisfies it.
        edu_ranked = sorted(education, key=lambda lvl: _EDU_RANK.get(lvl, 0))
        req_level = edu_ranked[0]
        req_rank = _EDU_RANK.get(req_level, 0)
        edu_entries = (parsed_resume or {}).get("education") or []
        edu_text = " ".join(str(e.get("degree") or "") for e in edu_entries)
        cand_levels = jd_signals.parse_education(edu_text)
        if not cand_levels and resume_text:
            cand_levels = jd_signals.parse_education(resume_text)
        cand_rank = max((_EDU_RANK.get(level, 0) for level in cand_levels), default=None)
        if cand_rank is None:
            # No verifiable degree on the resume -> cannot judge (excluded from scoring).
            status = UNKNOWN
        else:
            status = MET if cand_rank >= req_rank else MISSING
        reqs.append(
            RequirementItem(
                text=f"Education: {req_level}",
                kind="education",
                category=MUST_HAVE,
                status=status,
                evidence="",
            )
        )

    # Certifications (nice-to-have; skip ones already covered as skills)
    for cert in jd_signals.parse_certifications(jd):
        if _normalize_skill(cert) in seen:
            continue
        # Word-boundary match so a resume's "CKAD" does not credit the "CKA" cert.
        cert_present = jd_signals.cert_in_text(cert, resume_blob)
        reqs.append(
            RequirementItem(
                text=cert,
                kind="certification",
                category=NICE_TO_HAVE,
                status=MET if cert_present else MISSING,
            )
        )

    return RequirementAssessment(
        requirements=reqs,
        summary=f"Deterministic assessment: {len(reqs)} requirements extracted from JD signals.",
    )


# One deterministic scorer for the whole process; it holds no state.
CLASSIFIER = DeterministicClassifier()
