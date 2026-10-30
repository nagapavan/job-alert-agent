"""
Tests for the structured, stateless resume↔job match endpoint used by the
browser-extension side panel (`POST /api/match/analyze`).

The endpoint scores deterministically first (no LLM) via a lightweight
requirement extractor + classifier; the LLM extractor is only a fallback when
the deterministic path cannot produce any requirements.
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


# A JD/title pair with no recognizable deterministic signals, forcing the LLM fallback.
_NO_SIGNAL_TITLE = "Engineer"
_NO_SIGNAL_JD = "We are a small team that values curiosity and craft."


def test_match_analyze_deterministic_classifier_path_no_llm(db_session):
    """Signal-rich JD is scored deterministically without calling the LLM extractor."""
    _add_active_resume(db_session)
    with patch("backend.main.assess_job_requirements") as mock_assess:
        res = client.post("/api/match/analyze", json={
            "job_title": "Backend Engineer",
            "company": "Acme",
            "job_description": "We need Python, Kubernetes, Terraform and AWS.",
        })

    assert res.status_code == 200
    data = res.json()
    assert data["analysis_source"] == "classifier"
    assert data["match_method"] == "classifier_v1"
    assert data["match_score"] is not None
    assert data["must_coverage"] == 0.75
    # This JD has no preferred section, so the score is measured over the components it does
    # define (must 60 + seniority 15 + education 5 = 80) instead of scoring the empty
    # nice-to-have set as 0% (issue #70): 78.1, not the pre-#70 58.1.
    assert 75 <= data["match_score"] <= 80
    assert data["band"] == "Good"
    # Transparent breakdown so the score is explainable.
    assert len(data["requirements"]) == 4
    assert "Python" in data["strengths"]
    assert "Terraform" in data["gaps"]
    assert "must-have coverage 75%" in data["feedback"]
    mock_assess.assert_not_called()


def test_match_analyze_detects_ai_stack_and_preferred_section(db_session):
    """AI/GenAI/NLU requirements are detected, and the Preferred section is not dropped."""
    _add_active_resume(
        db_session,
        "Alice. Skills: LLM, GenAI, NLU, prompt design, RAG, embeddings, React, Node.js, CI/CD.",
    )
    with patch("backend.main.assess_job_requirements") as mock_assess:
        res = client.post("/api/match/analyze", json={
            "job_title": "Senior Software Engineer",
            "job_description": (
                "Required experience and skills\n"
                "12+ years software engineering experience.\n"
                "Strong JavaScript/Node.js, React and API integration skills.\n"
                "Hands-on experience with AI/GenAI or ML-driven features (LLM prompting, NLU).\n"
                "Preferred Experience\n"
                "Payroll and benefits domain depth.\n"
            ),
        })
    assert res.status_code == 200
    data = res.json()
    assert data["analysis_source"] == "classifier"
    # The AI vocabulary is scored (not filtered out by a fixed tech list).
    assert data["nice_coverage"] >= 0.0
    req_text = " ".join(r["text"] for r in data["requirements"])
    assert "LLM" in req_text or "NLU" in req_text
    # Preferred section is parsed as nice-to-have (previously reported "nice-to-have 0%").
    assert any(r["category"] == "nice_to_have" for r in data["requirements"])
    mock_assess.assert_not_called()


def test_feedback_says_no_nice_to_have_requirements_when_the_jd_states_none(db_session):
    """An empty nice-to-have set is "the JD states none", not "you met 0%" (issue #70)."""
    _add_active_resume(db_session, "Alice. Skills: Python, Kubernetes.")
    res = client.post("/api/match/analyze", json={
        "job_title": "Backend Engineer",
        "job_description": "Requirements\nMust have Python and Kubernetes.",
    })
    assert res.status_code == 200
    feedback = res.json()["feedback"]
    assert "no nice-to-have requirements" in feedback
    assert "nice-to-have 0%" not in feedback


def test_feedback_still_reports_nice_coverage_when_the_jd_has_a_preferred_section(db_session):
    _add_active_resume(db_session, "Alice. Skills: Python, Kubernetes.")
    res = client.post("/api/match/analyze", json={
        "job_title": "Backend Engineer",
        "job_description": (
            "Requirements\nMust have Python.\n"
            "Preferred\nKubernetes experience is a plus.\n"
        ),
    })
    assert res.status_code == 200
    feedback = res.json()["feedback"]
    assert "nice-to-have 100%" in feedback
    assert "no nice-to-have requirements" not in feedback


def test_match_analyze_unconfirmable_tenure_asks_user_to_decide(db_session):
    """Tech presented without dated tenure -> partial + explicit 'please decide' note."""
    _add_active_resume(db_session, "Alice. Skills: Golang, Python.")
    res = client.post("/api/match/analyze", json={
        "job_title": "Backend Engineer",
        "job_description": "What You Bring: At least 3-years experience in Golang.",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["analysis_source"] == "classifier"
    assert "please decide" in data["feedback"]
    golang = [r for r in data["requirements"] if r.get("technology") == "Golang"]
    assert golang and golang[0]["status"] == "partial"


def test_match_analyze_without_resume_returns_error_state(db_session):
    with patch("backend.main.assess_job_requirements") as mock_assess:
        res = client.post("/api/match/analyze", json={"job_title": "Backend Engineer"})

    assert res.status_code == 200
    data = res.json()
    assert data["match_score"] is None
    assert data["resume_available"] is False
    assert data["analysis_source"] == "error"
    assert data["error"]
    mock_assess.assert_not_called()


def test_match_analyze_requires_job_title():
    res = client.post("/api/match/analyze", json={"company": "Acme"})
    assert res.status_code == 422


@patch("backend.main.assess_job_requirements")
def test_match_analyze_llm_fallback_returns_structured_score(mock_assess, db_session):
    """When deterministic extraction yields nothing, the LLM fallback is used."""
    _add_active_resume(db_session)
    mock_assess.side_effect = lambda *a, **k: _result([
        RequirementItem(text="Python", status="met"),
        RequirementItem(text="AWS", status="met"),
        RequirementItem(text="Kubernetes", status="missing"),
        RequirementItem(text="TOGAF", status="missing"),
    ])

    res = client.post("/api/match/analyze", json={
        "job_title": _NO_SIGNAL_TITLE,
        "job_description": _NO_SIGNAL_JD,
    })

    assert res.status_code == 200
    data = res.json()
    # 2 of 4 must-haves met -> 50%, Weak band.
    assert data["match_score"] == 50.0
    assert data["band"] == "Weak"
    assert data["must_coverage"] == 0.5
    assert "Kubernetes" in data["gaps"]
    assert data["feedback"] == "Neutral summary."
    assert data["resume_available"] is True
    assert data["analysis_source"] == "llm"
    assert data["score_method"] == "requirement_coverage_v1"
    assert len(data["requirements"]) == 4

    called_resume, called_desc = mock_assess.call_args.args[:2]
    assert "Alice" in called_resume
    assert _NO_SIGNAL_JD in called_desc


@patch("backend.main.assess_job_requirements")
def test_match_analyze_inline_resume_overrides_active_resume(mock_assess, db_session):
    _add_active_resume(db_session, "ACTIVE_RESUME_MARKER")
    mock_assess.side_effect = lambda *a, **k: _result([
        RequirementItem(text="Python", status="met"),
    ])

    res = client.post("/api/match/analyze", json={
        "job_title": _NO_SIGNAL_TITLE,
        "job_description": _NO_SIGNAL_JD,
        "resume_text": "INLINE_RESUME_MARKER " * 20,
    })

    assert res.status_code == 200
    called_resume = mock_assess.call_args.args[0]
    assert "INLINE_RESUME_MARKER" in called_resume
    assert "ACTIVE_RESUME_MARKER" not in called_resume


@patch("backend.main.assess_job_requirements")
def test_match_analyze_empty_assessment_returns_error_no_fake_score(mock_assess, db_session):
    """No extracted requirements -> explicit error, never a fabricated score."""
    _add_active_resume(db_session)
    mock_assess.side_effect = lambda *a, **k: _result([])

    res = client.post("/api/match/analyze", json={
        "job_title": _NO_SIGNAL_TITLE,
        "job_description": _NO_SIGNAL_JD,
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
        "job_title": _NO_SIGNAL_TITLE,
        "job_description": _NO_SIGNAL_JD,
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
        "job_title": _NO_SIGNAL_TITLE,
        "job_description": _NO_SIGNAL_JD,
    })
    assert res.status_code == 200
    data = res.json()
    assert data["match_score"] is None
    assert data["analysis_source"] == "llm"
    assert data["unverified"] == ["Clearance"]
