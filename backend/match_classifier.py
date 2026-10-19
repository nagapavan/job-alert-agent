"""
Fast, deterministic ML-based job match classifier.

Replaces expensive full-cycle LLM extraction with lightweight feature-based scoring.
Trained on historical job assessments from the database.
"""

import json
import logging
import pickle
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List
import numpy as np

from backend import section_intent

logger = logging.getLogger("match_classifier")

BASE_DIR = Path(__file__).resolve().parent.parent
CLASSIFIER_PATH = BASE_DIR / "data" / "match_classifier.pkl"

# Training floors. Learning is refused below these so a model fitted on a handful of
# outcomes cannot silently ship and degrade scoring (see backend/learning_signals.py).
MIN_TRAINING_SAMPLES = 50
MIN_PER_CLASS_SAMPLES = 15


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


class SimpleHeuristicClassifier:
    """
    Fallback deterministic classifier when no trained model exists.
    Uses weighted rule-based scoring (no ML).
    """

    def predict(self, features: MatchFeatures) -> float:
        """Return match score 0-100 based on requirement coverage and fit."""
        # Weighted combination of signals
        score = (
            features.must_coverage * 60  # Must-haves dominate
            + features.nice_coverage * 20  # Nice-to-haves matter
            + features.seniority_fit * 15  # Seniority alignment
            + features.education_match * 5  # Education is secondary
        )
        score = min(100.0, max(0.0, score))

        # Penalize experience mismatch (too junior or too senior)
        if features.experience_years_delta < -0.3:  # Too junior
            score *= 0.8
        elif features.over_qualified_flag > 0.5:  # Over-qualified (flagged as advisory)
            pass  # Don't penalize; let user decide

        return score


class TrainedClassifier:
    """Lightweight logistic regression trained on historical assessments."""

    def __init__(self, model):
        self.model = model
        self.fallback = SimpleHeuristicClassifier()

    def predict(self, features: MatchFeatures) -> float:
        """Return match score 0-100."""
        try:
            X = self._features_to_array(features)
            # Logistic regression outputs probability [0, 1]; scale to [0, 100]
            prob = self.model.predict_proba(X.reshape(1, -1))[0, 1]
            return prob * 100.0
        except Exception as e:
            logger.warning(f"Classifier inference failed, using fallback: {e}")
            return self.fallback.predict(features)

    @staticmethod
    def _features_to_array(features: MatchFeatures) -> np.ndarray:
        """Convert MatchFeatures to numpy array for sklearn."""
        return np.array(
            [
                features.must_coverage,
                features.nice_coverage,
                features.seniority_fit,
                features.education_match,
                features.experience_years_delta,
                features.over_qualified_flag,
                min(features.must_count / 10.0, 1.0),  # Normalize by typical max
                min(features.nice_count / 10.0, 1.0),
            ],
            dtype=np.float32,
        )


def load_classifier() -> SimpleHeuristicClassifier | TrainedClassifier:
    """
    Load trained classifier if available, else return heuristic fallback.
    """
    if CLASSIFIER_PATH.exists():
        try:
            with open(CLASSIFIER_PATH, "rb") as f:
                model = pickle.load(f)
            logger.info(f"Loaded trained classifier from {CLASSIFIER_PATH}")
            return TrainedClassifier(model)
        except Exception as e:
            logger.warning(f"Failed to load classifier: {e}; using fallback heuristic")
            return SimpleHeuristicClassifier()
    else:
        logger.info(f"No classifier found at {CLASSIFIER_PATH}; using heuristic fallback")
        return SimpleHeuristicClassifier()


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

    # Seniority fit
    candidate_seniority = (
        resume_signals.latest_seniority(parsed_resume) if parsed_resume else "unknown"
    )
    job_seniority = jd_hints.get("seniority_level", "unknown")
    seniority_fit = 1.0 if candidate_seniority == job_seniority else 0.6

    # Education match
    education_match = 1.0 if jd_hints.get("education_mentioned") else 0.5

    # Requirement coverage (from assessment if available)
    must_coverage = 0.0
    nice_coverage = 0.0
    must_count = 0
    nice_count = 0
    over_qualified = 0.0

    if assessment and hasattr(assessment, "requirements"):
        reqs = assessment.requirements or []
        must_reqs = [r for r in reqs if r.category == "must_have"]
        nice_reqs = [r for r in reqs if r.category == "nice_to_have"]

        must_count = len(must_reqs)
        nice_count = len(nice_reqs)

        if must_reqs:
            met_must = sum(1 for r in must_reqs if r.status == "met")
            must_coverage = met_must / len(must_reqs)

        if nice_reqs:
            met_nice = sum(1 for r in nice_reqs if r.status == "met")
            nice_coverage = met_nice / len(nice_reqs)

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
    )


# ---------------------------------------------------------------------------
# Deterministic requirement extraction (no LLM)
# Builds the same RequirementAssessment shape the LLM produces, using only the
# JD text + parsed resume, so must/nice coverage can be computed without an LLM.
# ---------------------------------------------------------------------------

# Skill vocabulary, aliases and concept groups come from the curated taxonomy
# (data/skill_taxonomy.json) so the vocabulary can grow without code changes.
from backend.skill_taxonomy import (
    CLOUD_PROVIDERS as _CLOUD_PROVIDERS,
    CONCEPT_GROUPS as _CONCEPT_GROUPS,
    DISPLAY_SKILLS as _DISPLAY_SKILLS,
    alias_surfaces as _alias_surfaces,
    normalize_skill as _normalize_skill,
)

# Higher rank satisfies a lower education requirement (Masters meets Bachelor's, not vice versa).
_EDU_RANK = {"high_school": 0, "associate": 1, "bachelor": 2, "master": 3, "phd": 4}

_SENIORITY_RANK = {
    "intern": 0, "junior": 1, "mid": 2, "senior": 3, "lead": 4,
    "staff": 5, "principal": 6, "manager": 7, "director": 8,
}


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


def _resume_skill_scan(resume_text: str) -> "set[str]":
    """Skills detected from raw resume text (union of canonical + alias surfaces)."""
    lowered = (resume_text or "").lower()
    found = set()
    for s in _DISPLAY_SKILLS:
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
    for skill in _DISPLAY_SKILLS:
        norm = _normalize_skill(skill)
        if norm in seen or norm in tech_required:
            continue
        # Match the display name OR any alias surface (e.g. "golang" -> Go,
        # "genai" -> LLM, "prompt design" -> Prompt Engineering).
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
        elif _CONCEPT_GROUPS.get(norm, set()) & resume_skills:
            status = PARTIAL
        else:
            status = MISSING
        if norm in _CLOUD_PROVIDERS:
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
            reqs.append(
                RequirementItem(
                    text=item["text"],
                    kind="skill",
                    category=item["category"],
                    status=PARTIAL,
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

    # Seniority level
    job_sen = jd_signals.seniority_level(job_title or "")
    cand_sen = resume_signals.latest_seniority(parsed_resume)
    if job_sen and job_sen != "unknown":
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


def train_classifier_from_db(db_session, output_path: Optional[Path] = None):
    """Train a logistic-regression classifier on *implicit user outcomes*.

    Labels are derived by :mod:`backend.learning_signals` from real behaviour (the user
    applied, or deliberately dismissed a role) — never from the model's own previous
    output. System-generated states (discovery auto-filtering, rows pre-set at ingestion)
    are excluded, because they carry no preference signal.

    Refuses to train below ``MIN_TRAINING_SAMPLES`` with at least
    ``MIN_PER_CLASS_SAMPLES`` in each class. This is deliberate: a model fitted on a
    handful of positives would memorise noise and silently degrade scoring while looking
    like it had learned something.
    """
    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        logger.error(
            "scikit-learn not installed. Install with: pip install scikit-learn"
        )
        return False

    from backend.database import Job
    from backend import learning_signals, jd_signals

    output = output_path or CLASSIFIER_PATH
    output.parent.mkdir(parents=True, exist_ok=True)

    decisions = learning_signals.collect_decisions(db_session)
    trainable = [d for d in decisions if d.is_trainable]
    excluded = len(decisions) - len(trainable)

    positives = [d for d in trainable if d.label in (
        learning_signals.POSITIVE, learning_signals.STRONG_POSITIVE
    )]
    negatives = [d for d in trainable if d.label in (
        learning_signals.NEGATIVE, learning_signals.NEGATIVE_OUTCOME
    )]

    if len(trainable) < MIN_TRAINING_SAMPLES:
        logger.warning(
            "Insufficient training signal: %d trainable decisions (need >= %d). "
            "%d rows excluded as system-generated. Not training.",
            len(trainable), MIN_TRAINING_SAMPLES, excluded,
        )
        return False
    if min(len(positives), len(negatives)) < MIN_PER_CLASS_SAMPLES:
        logger.warning(
            "Insufficient class balance: %d positive / %d negative "
            "(need >= %d each). Not training.",
            len(positives), len(negatives), MIN_PER_CLASS_SAMPLES,
        )
        return False

    logger.info(
        "Training classifier on %d user decisions (%d positive, %d negative); "
        "%d system-generated rows excluded",
        len(trainable), len(positives), len(negatives), excluded,
    )

    X_list = []
    y_list = []

    for decision in trainable:
        try:
            job = db_session.query(Job).filter(Job.id == decision.job_id).first()
            if job is None:
                continue
            # Re-derive features from the job's own JD so training uses the same code path
            # as live scoring (train/serve parity). Stored match_*_json columns are NULL for
            # progressed rows, so they cannot be used as features.
            assessment = build_deterministic_assessment(
                job.description or "", None, job_title=job.title or ""
            )
            if not assessment.requirements:
                continue
            jd_hints = {
                "min_years": 0,
                "max_years": 100,
                "seniority_level": jd_signals.seniority_level(job.title or ""),
                "education_mentioned": bool(jd_signals.parse_education(job.description or "")),
            }
            features = extract_match_features(None, jd_hints, assessment=assessment)
            X_list.append(TrainedClassifier._features_to_array(features))
            y_list.append(
                1 if decision.label in (
                    learning_signals.POSITIVE, learning_signals.STRONG_POSITIVE
                ) else 0
            )
        except Exception as e:
            logger.debug(f"Skipped job {decision.job_id}: {e}")
            continue

    if len(X_list) < MIN_TRAINING_SAMPLES:
        logger.warning(
            "Only %d rows yielded usable features (need >= %d). Not training.",
            len(X_list), MIN_TRAINING_SAMPLES,
        )
        return False

    X_train = np.array(X_list, dtype=np.float32)
    y_train = np.array(y_list)

    if len(set(y_train.tolist())) < 2:
        logger.warning("Training data contains a single class; not training.")
        return False

    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(X_train, y_train)

    with open(output, "wb") as f:
        pickle.dump(model, f)

    logger.info(f"Trained classifier saved to {output}")
    logger.info(f"  Features: {X_train.shape[1]}, Samples: {X_train.shape[0]}")
    logger.info(f"  Positive class: {y_train.sum()} / {len(y_train)}")

    return True


# Singleton instance (lazy-loaded on first use)
_classifier_instance = None


def get_classifier():
    """Get the global classifier instance (lazy-loaded)."""
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = load_classifier()
    return _classifier_instance
