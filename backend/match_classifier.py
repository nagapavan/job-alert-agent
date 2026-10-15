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

logger = logging.getLogger("match_classifier")

BASE_DIR = Path(__file__).resolve().parent.parent
CLASSIFIER_PATH = BASE_DIR / "data" / "match_classifier.pkl"


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

_SKILL_ALIASES = {
    "golang": "go",
    "postgres": "postgresql",
    "k8s": "kubernetes",
    "amazon web services": "aws",
    "google cloud platform": "gcp",
    "google cloud": "gcp",
    "microsoft azure": "azure",
    "llms": "llm",
    "apache kafka": "kafka",
}

# Broad JD terms credit a partial match when the resume has related technologies.
_CONCEPT_GROUPS = {
    "distributed systems": {
        "kafka", "grpc", "microservices", "rabbitmq", "redis",
        "elasticsearch", "celery", "system design", "kubernetes",
    },
    "microservices": {"grpc", "docker", "kubernetes", "kafka", "rabbitmq", "rest api"},
    "system design": {"microservices", "distributed systems", "grpc", "kafka", "rest api"},
    "machine learning": {"llm", "nlp", "pytorch", "tensorflow", "pandas", "numpy", "scikit-learn"},
}

_CLOUD_PROVIDERS = {"aws", "gcp", "azure"}

# Higher rank satisfies a lower education requirement (Masters meets Bachelor's, not vice versa).
_EDU_RANK = {"high_school": 0, "associate": 1, "bachelor": 2, "master": 3, "phd": 4}

_NICE_SECTION_RE = re.compile(
    r"(preferred qualifications?|nice[\s-]?to[\s-]?have|good[\s-]?to[\s-]?have|"
    r"bonus points?|a plus|desirable|stand out|standout|ideally)",
    re.I,
)

# Company-intro block; skills mentioned only in this marketing prose (e.g. "40-year old
# C++ code") are not treated as requirements. We skip from the intro to the next heading,
# rather than requiring an explicit "Requirements" marker, so skill lists that appear
# before a heading are still captured.
_INTRO_RE = re.compile(
    r"(about us|about the company|about the team|about the role|who we are|"
    r"company overview|our story|our mission)",
    re.I,
)
_SECTION_HEADING_RE = re.compile(
    r"(what you'?ll do|what you'?ll be doing|how you'?ll make an impact|responsibilities|"
    r"the role|role expectations|your role|who you are|what you bring|what you'?ll need|"
    r"what we'?re looking for|key qualifications|preferred qualifications|requirements|"
    r"qualifications|must[\s-]?have|skills|you have)",
    re.I,
)

_SENIORITY_RANK = {
    "intern": 0, "junior": 1, "mid": 2, "senior": 3, "lead": 4,
    "staff": 5, "principal": 6, "manager": 7, "director": 8,
}


def _skill_token(pattern: str) -> re.Pattern:
    """Boundary that tolerates symbols like C++ / Node.js / CI/CD."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(pattern) + r"(?![A-Za-z0-9])", re.I)


def _normalize_skill(name: str) -> str:
    t = (name or "").strip().lower()
    return _SKILL_ALIASES.get(t, t)


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
    from backend.parser import COMMON_TECH_SKILLS
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
    # Fall back to scanning the raw resume text when structured skills are absent
    # (e.g. resumes ingested before parsing, or inline resume text from the extension).
    if not resume_skills and resume_text:
        resume_skills = {
            _normalize_skill(s)
            for s in COMMON_TECH_SKILLS
            if _skill_token(s).search(resume_text.lower())
        }
    resume_blob = " ".join(
        [resume_text or ""] + [str(s) for s in ((parsed_resume or {}).get("skills") or [])]
    ).lower()

    nice_match = _NICE_SECTION_RE.search(jd)
    nice_start = nice_match.start() if nice_match else len(jd)
    # Skip the company-intro block (if any) so marketing prose is not read as requirements.
    # Only honor the skip when it still leaves extractable skills; otherwise a nav/footer
    # "About Us" could wipe the entire posting. Fall back to scanning the whole JD.
    intro_match = _INTRO_RE.search(jd)
    scan_start = 0
    if intro_match:
        next_heading = _SECTION_HEADING_RE.search(jd, intro_match.end())
        candidate = next_heading.start() if next_heading else intro_match.end()
        if candidate < len(jd) and any(
            _skill_token(s).search(jd_lower, candidate) for s in COMMON_TECH_SKILLS
        ):
            scan_start = candidate

    # Parse experience signals first, so a technology that carries a years requirement is
    # not also credited as a plain (presence-only) skill. Scoped past the intro to avoid
    # prose like "40-year old C++ code".
    hints = jd_signals.parse_experience(jd[scan_start:] if scan_start else jd)
    candidate_years = resume_signals.estimate_total_years(parsed_resume)
    tech_hints = [h for h in hints if h.get("scope") == "technology" and h.get("technology")]
    total_hints = [h for h in hints if (h.get("scope") or "total") == "total"]
    if not total_hints and not tech_hints:
        total_hints = hints
    tech_required = {_normalize_skill(h["technology"]) for h in tech_hints}

    seen = set()
    cloud_hits = []
    for skill in COMMON_TECH_SKILLS:
        norm = _normalize_skill(skill)
        if norm in seen or norm in tech_required:
            continue
        m = _skill_token(skill).search(jd_lower, scan_start)
        if not m:
            continue
        seen.add(norm)
        category = NICE_TO_HAVE if m.start() >= nice_start else MUST_HAVE
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
        # "Bachelor's or Master's" lists alternatives -> the lowest listed level is the
        # requirement; a higher degree still satisfies it.
        req_rank = min(_EDU_RANK.get(level, 0) for level in education)
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
                text=f"Education: {', '.join(education)}",
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
        reqs.append(
            RequirementItem(
                text=cert,
                kind="certification",
                category=NICE_TO_HAVE,
                status=MET if cert.lower() in resume_blob else MISSING,
            )
        )

    return RequirementAssessment(
        requirements=reqs,
        summary=f"Deterministic assessment: {len(reqs)} requirements extracted from JD signals.",
    )


def train_classifier_from_db(db_session, output_path: Optional[Path] = None):
    """
    Train a logistic regression classifier on historical assessments.

    Loads jobs with match_scored=True, extracts features, and trains.
    Saves to data/match_classifier.pkl (or custom output_path).
    """
    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        logger.error(
            "scikit-learn not installed. Install with: pip install scikit-learn"
        )
        return False

    from backend.database import Job
    from backend import jd_signals, resume_signals, match_scoring

    output = output_path or CLASSIFIER_PATH
    output.parent.mkdir(parents=True, exist_ok=True)

    # Query training data: jobs with assessed matches
    jobs = (
        db_session.query(Job)
        .filter(Job.match_scored == True, Job.match_score != None)
        .limit(500)  # Cap training set for now
        .all()
    )

    if len(jobs) < 10:
        logger.warning(
            f"Insufficient training data ({len(jobs)} jobs); classifier not trained"
        )
        return False

    logger.info(f"Training classifier on {len(jobs)} historical assessments")

    X_list = []
    y_list = []

    for job in jobs:
        try:
            # Re-extract signals deterministically
            jd_hints = jd_signals.parse_experience(job.description or "")
            parsed_resume = None  # We don't have resume context here; use None
            assessment = None  # We don't have the full assessment; use basic heuristic

            features = extract_match_features(parsed_resume, jd_hints, assessment)
            X = TrainedClassifier._features_to_array(features)

            # Target: threshold match_score at 60% for binary classification
            # (this is a simplification; you can tune or make it regression)
            y = 1 if (job.match_score or 0) >= 60 else 0

            X_list.append(X)
            y_list.append(y)
        except Exception as e:
            logger.debug(f"Skipped job {job.id}: {e}")
            continue

    if len(X_list) < 10:
        logger.warning("Not enough valid samples to train classifier")
        return False

    X_train = np.array(X_list, dtype=np.float32)
    y_train = np.array(y_list)

    # Train logistic regression
    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(X_train, y_train)

    # Save model
    with open(output, "wb") as f:
        pickle.dump(model, f)

    logger.info(f"✓ Trained classifier saved to {output}")
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
