"""
Tests for the structured, stateless resume↔job match endpoint used by the
browser-extension side panel (`POST /api/match/analyze`).

The endpoint extracts requirements and computes the score deterministically via
``assess_job_requirements``; the returned score and the derived analysis can never diverge.
"""

from unittest.mock import patch

from conftest import test_client as client
from backend.database import Resume
from backend.config import encrypt_data
from backend.match_scoring import RequirementAssessment, RequirementItem, compute_match


def _add_active_resume(db_session, text="Alice Cloud Engineer. Skills: Python, AWS, Kubernetes."):
    resume = Resume(
        filename="resume.md",
        content_encrypted=encrypt_data(text),
        is_active=True,
    )
    db_session.add(resume)
    db_session.commit()
    db_session.refresh(resume)
    return resume


def _result(items, summary="Neutral summary."):
    assessment = RequirementAssessment(requirements=items, summary=summary)
    return assessment, compute_match(assessment)


@patch("backend.main.assess_job_requirements")
def test_match_analyze_returns_structured_score_and_analysis(mock_assess, db_session):
    _add_active_resume(db_session)
    mock_assess.side_effect = lambda *a, **k: _result([
        RequirementItem(text="Python", status="met"),
        RequirementItem(text="AWS", status="met"),
        RequirementItem(text="Kubernetes", status="missing"),
        RequirementItem(text="TOGAF", status="missing"),
    ])

    res = client.post("/api/match/analyze", json={
        "job_title": "Enterprise Architect",
        "company": "Financial Times",
        "job_description": "Enterprise architecture role requiring TOGAF.",
    })

    assert res.status_code == 200
    data = res.json()
    # 2 of 4 must-haves met -> 50%, Weak band.
    assert data["match_score"] == 50.0
    assert data["band"] == "Weak"
    assert data["must_coverage"] == 0.5
    assert data["strengths"] == ["Python", "AWS"]
    assert "Kubernetes" in data["gaps"]
    assert data["feedback"] == "Neutral summary."
    assert data["resume_available"] is True
    assert data["analysis_source"] == "llm"
    assert data["score_method"] == "requirement_coverage_v1"
    assert len(data["requirements"]) == 4

    called_resume, called_desc = mock_assess.call_args.args[:2]
    assert "Alice" in called_resume
    assert "TOGAF" in called_desc


@patch("backend.main.assess_job_requirements")
def test_match_analyze_without_resume_returns_error_state(mock_assess, db_session):
    res = client.post("/api/match/analyze", json={"job_title": "Backend Engineer"})

    assert res.status_code == 200
    data = res.json()
    assert data["match_score"] is None
    assert data["resume_available"] is False
    assert data["analysis_source"] == "error"
    assert data["error"]
    mock_assess.assert_not_called()


@patch("backend.main.assess_job_requirements")
def test_match_analyze_inline_resume_overrides_active_resume(mock_assess, db_session):
    _add_active_resume(db_session, "ACTIVE_RESUME_MARKER")
    mock_assess.side_effect = lambda *a, **k: _result([
        RequirementItem(text="Python", status="met"),
    ])

    res = client.post("/api/match/analyze", json={
        "job_title": "Staff Engineer",
        "job_description": "Staff engineer to lead the platform team building distributed services.",
        "resume_text": "INLINE_RESUME_MARKER " * 20,
    })

    assert res.status_code == 200
    called_resume = mock_assess.call_args.args[0]
    assert "INLINE_RESUME_MARKER" in called_resume
    assert "ACTIVE_RESUME_MARKER" not in called_resume


def test_match_analyze_requires_job_title():
    res = client.post("/api/match/analyze", json={"company": "Acme"})
    assert res.status_code == 422


@patch("backend.main.assess_job_requirements")
def test_match_analyze_empty_assessment_returns_error_no_fake_score(mock_assess, db_session):
    """No extracted requirements -> explicit error, never a fabricated score."""
    _add_active_resume(db_session)
    mock_assess.side_effect = lambda *a, **k: _result([])

    res = client.post("/api/match/analyze", json={
        "job_title": "Principal Engineer",
        "job_description": "We need Python, Kubernetes, Terraform and AWS.",
    })

    assert res.status_code == 200
    data = res.json()
    assert data["analysis_source"] == "error"
    assert data["match_score"] is None
    assert data["error"]


@patch("backend.main.assess_job_requirements", side_effect=RuntimeError("model offline"))
def test_match_analyze_exception_returns_error_no_fake_score(mock_assess, db_session):
    _add_active_resume(db_session)
    res = client.post("/api/match/analyze", json={
        "job_title": "Backend Engineer",
        "job_description": "Python backend role with AWS and Kubernetes.",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["analysis_source"] == "error"
    assert data["match_score"] is None
    assert data["error"]


@patch("backend.main.assess_job_requirements")
def test_match_analyze_unverified_requirements_yield_no_score(mock_assess, db_session):
    """Requirements that cannot be judged are 'unverified' -> no score, but not an error."""
    _add_active_resume(db_session)
    mock_assess.side_effect = lambda *a, **k: _result([
        RequirementItem(text="Clearance", status="unknown"),
    ])
    res = client.post("/api/match/analyze", json={
        "job_title": "Backend Engineer",
        "job_description": "Requires active security clearance.",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["match_score"] is None
    assert data["analysis_source"] == "llm"
    assert data["unverified"] == ["Clearance"]
