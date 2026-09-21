"""
Tests for the structured, stateless resume↔job match endpoint used by the
browser-extension side panel (`POST /api/match/analyze`).

The endpoint must return the score and the analysis narrative from the SAME
structured result so the displayed score circle and the analysis text can never
diverge (the previous extension code hardcoded an 88% score).
"""

from unittest.mock import patch

from conftest import test_client as client
from backend.database import Resume
from backend.config import encrypt_data


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


@patch("backend.main.analyze_job_match")
def test_match_analyze_returns_structured_score_and_analysis(mock_analyze, db_session):
    _add_active_resume(db_session)
    mock_analyze.return_value = {
        "match_score": 47.5,
        "strengths": ["Python", "AWS"],
        "gaps": ["No Kubernetes"],
        "feedback": "Not recommended for immediate application.",
    }

    res = client.post("/api/match/analyze", json={
        "job_title": "Enterprise Architect",
        "company": "Financial Times",
        "job_description": "Enterprise architecture role requiring TOGAF.",
    })

    assert res.status_code == 200
    data = res.json()
    # Score and narrative come from the same structured result -> always in sync.
    assert data["match_score"] == 47.5
    assert data["strengths"] == ["Python", "AWS"]
    assert data["gaps"] == ["No Kubernetes"]
    assert data["feedback"].startswith("Not recommended")
    assert data["resume_available"] is True
    assert data["analysis_source"] == "llm"

    # Active resume + job description were forwarded to the analyzer.
    called_resume, called_desc = mock_analyze.call_args.args[:2]
    assert "Alice" in called_resume
    assert "TOGAF" in called_desc


@patch("backend.main.analyze_job_match")
def test_match_analyze_without_resume_returns_error_state(mock_analyze, db_session):
    res = client.post("/api/match/analyze", json={"job_title": "Backend Engineer"})

    assert res.status_code == 200
    data = res.json()
    assert data["match_score"] is None
    assert data["resume_available"] is False
    assert data["analysis_source"] == "error"
    assert data["error"]
    mock_analyze.assert_not_called()


@patch("backend.main.analyze_job_match")
def test_match_analyze_inline_resume_overrides_active_resume(mock_analyze, db_session):
    _add_active_resume(db_session, "ACTIVE_RESUME_MARKER")
    mock_analyze.return_value = {
        "match_score": 80.0,
        "strengths": ["Python"],
        "gaps": [],
        "feedback": "Strong fit.",
    }

    res = client.post("/api/match/analyze", json={
        "job_title": "Staff Engineer",
        "resume_text": "INLINE_RESUME_MARKER " * 20,
    })

    assert res.status_code == 200
    called_resume = mock_analyze.call_args.args[0]
    assert "INLINE_RESUME_MARKER" in called_resume
    assert "ACTIVE_RESUME_MARKER" not in called_resume


def test_match_analyze_requires_job_title():
    res = client.post("/api/match/analyze", json={"company": "Acme"})
    assert res.status_code == 422


@patch("backend.main.analyze_job_match")
def test_match_analyze_unreachable_model_returns_error_no_fake_score(mock_analyze, db_session):
    """When the model is unreachable it returns the empty 60% JobMatchResult default; the endpoint
    must surface an explicit error instead of fabricating a score."""
    _add_active_resume(db_session)
    mock_analyze.return_value = {"match_score": 60.0, "strengths": [], "gaps": [], "feedback": ""}

    res = client.post("/api/match/analyze", json={
        "job_title": "Principal Engineer",
        "job_description": "We need Python, Kubernetes, Terraform and AWS.",
    })

    assert res.status_code == 200
    data = res.json()
    assert data["analysis_source"] == "error"
    assert data["match_score"] is None
    assert data["error"]


@patch("backend.main.analyze_job_match", side_effect=RuntimeError("model offline"))
def test_match_analyze_exception_returns_error_no_fake_score(mock_analyze, db_session):
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


@patch("backend.main.analyze_job_match")
def test_match_analyze_generic_error_sentinel_returns_error(mock_analyze, db_session):
    """The analyzer swallows its own errors and returns a generic sentinel; the endpoint must not
    present that as a real score."""
    _add_active_resume(db_session)
    mock_analyze.return_value = {
        "match_score": 50.0,
        "strengths": [],
        "gaps": ["Unable to complete AI comparison."],
        "feedback": "Analysis encountered an error: model offline",
    }
    res = client.post("/api/match/analyze", json={
        "job_title": "Backend Engineer",
        "job_description": "Python backend role with AWS and Kubernetes.",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["analysis_source"] == "error"
    assert data["match_score"] is None
    assert data["error"]


