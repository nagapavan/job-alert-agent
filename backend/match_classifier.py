"""
Fast, deterministic ML-based job match classifier.

Replaces expensive full-cycle LLM extraction with lightweight feature-based scoring.
Trained on historical job assessments from the database.
"""

import json
import logging
import pickle
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
