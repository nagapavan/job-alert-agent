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
