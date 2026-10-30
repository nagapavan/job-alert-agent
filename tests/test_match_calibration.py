"""Calibration corpus for the requirement-coverage matcher.

The deterministic tests run in CI and check the engine's band/recommendation against
hand-authored ground-truth assessments. The live test (opt-in) runs the real extractor
against the synthetic resume/JD pairs and reports band accuracy.
"""
import json
import os
from pathlib import Path

import pytest

from backend.match_scoring import (
    RequirementAssessment,
    RequirementItem,
    compute_match,
)

FIXTURES = Path(__file__).parent / "fixtures" / "match_calibration.jsonl"

VALID_BANDS = {"Strong", "Good", "Moderate", "Weak", "Poor", "Unknown"}


def _load_cases():
    cases = []
    for line in FIXTURES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


CASES = _load_cases()
IDS = [c["id"] for c in CASES]


def test_calibration_fixtures_well_formed():
    assert len(CASES) >= 8
    for case in CASES:
        assert case["resume"].strip()
        assert case["jd"].strip()
        assert case["expected_band"] in VALID_BANDS
        assert case["requirements"], f"{case['id']} has no ground-truth requirements"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_engine_matches_ground_truth(case):
    assessment = RequirementAssessment(
        requirements=[RequirementItem(**r) for r in case["requirements"]],
        over_qualified=case.get("over_qualified", False),
    )
    result = compute_match(assessment)
    assert result.band == case["expected_band"], case["id"]
    assert result.apply_recommendation == case["expected_recommendation"], case["id"]


# --- Live extraction calibration (opt-in: RUN_LIVE_LLM_TESTS=1 + a running provider) ---
RUN_LIVE_LLM_TESTS = os.environ.get("RUN_LIVE_LLM_TESTS", "").lower() in ("1", "true", "yes")


def _provider_available() -> bool:
    try:
        from backend.ai_helper import detect_active_llm_provider

        return detect_active_llm_provider().get("status") != "offline"
    except Exception:
        return False


@pytest.mark.live
@pytest.mark.skipif(
    not (RUN_LIVE_LLM_TESTS and _provider_available()),
    reason="Live calibration is opt-in. Set RUN_LIVE_LLM_TESTS=1 with a local LLM running.",
)
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_live_extraction_matches_expected_band(case):
    from backend.ai_helper import assess_job_requirements

    _, result = assess_job_requirements(case["resume"], case["jd"])
    assert result.band == case["expected_band"], (
        f"{case['id']}: expected {case['expected_band']}, got {result.band} "
        f"(score={result.score})"
    )


# --- Score scale: an empty nice-to-have set must not cost points (issue #70) ---
def test_score_scale_renormalises_when_the_jd_states_no_nice_to_haves():
    """Pin the scale so a future weight change cannot silently re-introduce the 20-point hole.

    A JD with no preferred section scores over must 60 + seniority 15 + education 5 (80 points),
    so an otherwise perfect fit reaches 100 instead of being capped at 80.
    """
    from backend.match_classifier import MatchFeatures, SimpleHeuristicClassifier

    classifier = SimpleHeuristicClassifier()

    def score(must_cov, nice_cov, must_count, nice_count):
        return classifier.predict(
            MatchFeatures(
                must_coverage=must_cov,
                nice_coverage=nice_cov,
                seniority_fit=1.0,
                education_match=1.0,
                experience_years_delta=0.0,
                over_qualified_flag=0.0,
                must_count=must_count,
                nice_count=nice_count,
            )
        )

    assert score(1.0, 0.0, 8, 0) == pytest.approx(100.0)
    assert score(0.5, 0.0, 8, 0) == pytest.approx(62.5)
    # A present nice-to-have set keeps the full 100-point scale.
    assert score(0.5, 1.0, 8, 4) == pytest.approx(70.0)
    assert score(0.5, 0.0, 8, 4) == pytest.approx(50.0)
