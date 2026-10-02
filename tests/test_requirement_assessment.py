"""Tests for the requirement-assessment orchestration (LLM mocked)."""
import pytest
from unittest.mock import patch

from backend.ai_helper import assess_job_requirements
from backend.match_scoring import RequirementAssessment, RequirementItem


def _item(text, status, category="must_have", **kw):
    return RequirementItem(text=text, status=status, category=category, **kw)


@patch("backend.ai_helper.generate_structured")
def test_scores_deterministically_from_assessment(mock_gen):
    mock_gen.return_value = RequirementAssessment(requirements=[
        _item("Python", "met", kind="skill"),
        _item("Kubernetes", "missing", kind="skill"),
        _item("Go", "met", category="nice_to_have", kind="skill"),
    ])
    _, result = assess_job_requirements("resume", "We need a Python engineer to build distributed backend services.")
    # must 0.5, nice 1.0 -> 0.7*0.5 + 0.3*1.0 = 0.65 -> 65 (Moderate, caution).
    assert result.score == 65.0
    assert result.must_coverage == 0.5
    assert result.apply_recommendation == "apply_with_caution"
    mock_gen.assert_called_once()


@patch("backend.ai_helper.generate_structured")
def test_normalizes_unknown_status_and_category(mock_gen):
    mock_gen.return_value = RequirementAssessment(requirements=[
        RequirementItem(text="X", status="bogus", category="weird"),
    ])
    assessment, result = assess_job_requirements("resume", "We need a Python engineer to build distributed backend services.")
    assert assessment.requirements[0].status == "unknown"
    assert assessment.requirements[0].category == "must_have"
    assert result.score is None  # nothing verifiable


@patch("backend.ai_helper.generate_structured")
def test_over_qualified_total_flag_is_advisory(mock_gen):
    mock_gen.return_value = RequirementAssessment(requirements=[
        _item("Senior Engineer", "met", kind="seniority"),
        _item("3+ years experience", "met", kind="experience_years", scope="total", min_years=3.0),
    ])
    parsed = {"experience": [{"title": "Senior Engineer", "dates": "2014 - Present"}]}
    assessment, result = assess_job_requirements("resume", "We need a Python engineer to build distributed backend services.", parsed_resume=parsed)
    assert assessment.over_qualified is True
    assert result.over_qualified is True
    assert result.score == 100.0  # advisory only; must-haves all met
    assert result.apply_recommendation == "apply_with_caution"


@patch("backend.ai_helper.generate_structured")
def test_over_qualified_technology_specific_not_flagged(mock_gen):
    mock_gen.return_value = RequirementAssessment(requirements=[
        _item("3+ years of Python", "met", kind="experience_years", scope="technology", technology="Python", min_years=3.0),
    ])
    parsed = {"experience": [{"title": "Engineer", "dates": "2014 - Present"}]}
    assessment, result = assess_job_requirements("resume", "We need a Python engineer to build distributed backend services.", parsed_resume=parsed)
    assert assessment.over_qualified is False
    assert result.over_qualified is False


def test_empty_inputs_raise():
    with pytest.raises(ValueError):
        assess_job_requirements("", "job")
    with pytest.raises(ValueError):
        assess_job_requirements("resume", "")
