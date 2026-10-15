"""Tests for the batch match re-score runner and endpoint (LLM mocked)."""
import json
from unittest.mock import patch

from backend.config import encrypt_data
from backend.database import Job, Resume
from backend.main import _run_match_rescore_task_runner, _score_job_with_engine
from backend.match_scoring import RequirementAssessment, RequirementItem, compute_match

from conftest import test_client as client


def _result(items):
    assessment = RequirementAssessment(requirements=items, summary="summary")
    return assessment, compute_match(assessment)


@patch("backend.main.assess_job_requirements")
def test_score_job_with_engine_deterministic(mock_assess, db_session):
    # Signal-rich JD -> deterministic classifier, no LLM call.
    score, analysis, scored, method, at, band, rec, strengths, gaps = _score_job_with_engine(
        db_session, "resume text", "We need a Python engineer to build distributed APIs."
    )
    assert scored is True
    assert method == "classifier_v1"
    assert at is not None
    assert band is not None
    assert isinstance(strengths, list)
    assert isinstance(gaps, list)
    mock_assess.assert_not_called()

    # Too-short description -> unscored, and neither engine is called.
    mock_assess.reset_mock()
    assert _score_job_with_engine(db_session, "resume", "short") == (
        0, "", False, None, None, None, None, [], []
    )
    mock_assess.assert_not_called()


@patch("backend.main.assess_job_requirements")
def test_score_job_with_engine_llm_fallback(mock_assess, db_session):
    mock_assess.side_effect = lambda *a, **k: _result(
        [RequirementItem(text="Python", status="met")]
    )
    # JD-like but signal-less -> deterministic yields nothing -> bounded LLM fallback.
    score, analysis, scored, method, at, band, rec, strengths, gaps = _score_job_with_engine(
        db_session,
        "resume text",
        "We are looking for a motivated individual with strong communication skills to join our team.",
    )
    assert scored is True
    assert score == 100.0
    assert band == "Strong"
    assert rec == "apply"
    assert method == "requirement_coverage_v1"
    assert strengths == ["Python"]
    mock_assess.assert_called_once()


@patch("backend.main.assess_job_requirements")
def test_score_job_with_engine_llm_failure_unscored(mock_assess, db_session):
    mock_assess.side_effect = RuntimeError("llm offline")
    score, analysis, scored, method, at, band, rec, strengths, gaps = _score_job_with_engine(
        db_session,
        "resume text",
        "We are looking for a motivated individual with strong communication skills to join our team.",
    )
    assert scored is False
    assert method is None
    assert strengths == []
    assert gaps == []


@patch("backend.main.assess_job_requirements")
def test_score_job_with_engine_skips_non_jd_snippet(mock_assess, db_session):
    # Discovery artifacts (LinkedIn alert/saved snippets) have no JD cues -> stay unscored
    # without spending a bounded LLM call.
    result = _score_job_with_engine(
        db_session,
        "resume text",
        "LinkedIn Alert opening for Software Engineer | Remote at LinkedIn Network",
    )
    assert result == (0, "", False, None, None, None, None, [], [])
    mock_assess.assert_not_called()


@patch("backend.main.assess_job_requirements")
def test_rescore_updates_existing_jobs(mock_assess, db_session):
    resume = Resume(
        filename="r.md",
        content_encrypted=encrypt_data("resume text"),
        parsed_json_encrypted=encrypt_data(json.dumps({"skills": ["Python"]})),
        is_active=True,
    )
    db_session.add(resume)
    db_session.commit()

    jobs = [
        Job(company_id=1, title="Backend", description="We need a Python backend engineer to build APIs and services.", url="u1", status="To Apply", match_score=88.0, match_scored=False),
        Job(company_id=1, title="Sales", description="We are looking for a sales representative to own the enterprise territory.", url="u2", status="To Apply", match_score=88.0, match_scored=False),
    ]
    db_session.add_all(jobs)
    db_session.commit()
    job_ids = [j.id for j in jobs]

    mock_assess.side_effect = lambda *a, **k: _result(
        [RequirementItem(text="Python", status="met")]
    )

    with patch("backend.main.get_db", return_value=iter([db_session])):
        out = _run_match_rescore_task_runner(limit=10)

    assert out["rescored"] == 2
    methods = {}
    for job_id in job_ids:
        refreshed = db_session.query(Job).filter(Job.id == job_id).first()
        assert refreshed.match_scored is True
        assert refreshed.match_scored_at is not None
        # Structured insights are persisted alongside the narrative on rescore.
        assert refreshed.match_strengths_json is not None
        assert refreshed.match_gaps_json is not None
        assert isinstance(refreshed.match_strengths, list)
        assert isinstance(refreshed.match_gaps, list)
        methods[refreshed.title] = refreshed.score_method
    # Signal-rich job scored deterministically; signal-less job via the bounded LLM fallback.
    assert methods["Backend"] == "classifier_v1"
    assert methods["Sales"] == "requirement_coverage_v1"
    mock_assess.assert_called_once()
    assert out["deterministic"] == 1
    assert out["llm_fallback"] == 1


@patch("backend.main._deterministic_match")
@patch("backend.main.assess_job_requirements")
def test_score_job_with_engine_falls_back_on_deterministic_error(
    mock_assess, mock_det, db_session
):
    mock_det.side_effect = RuntimeError("boom")
    mock_assess.side_effect = lambda *a, **k: _result(
        [RequirementItem(text="Python", status="met")]
    )
    _, _, scored, method, _, _, _, _, _ = _score_job_with_engine(
        db_session, "resume text", "We need a Python engineer to build distributed APIs."
    )
    assert scored is True
    assert method == "requirement_coverage_v1"
    mock_assess.assert_called_once()


@patch("backend.main.DETERMINISTIC_BULK_SCORING", False)
@patch("backend.main.assess_job_requirements")
def test_score_job_with_engine_flag_disables_deterministic(mock_assess, db_session):
    mock_assess.side_effect = lambda *a, **k: _result(
        [RequirementItem(text="Python", status="met")]
    )
    _, _, scored, method, _, _, _, _, _ = _score_job_with_engine(
        db_session, "resume text", "We need a Python engineer to build distributed APIs."
    )
    assert scored is True
    assert method == "requirement_coverage_v1"
    mock_assess.assert_called_once()


@patch("backend.main.top_evidence", return_value=[])
@patch("backend.main.assess_job_requirements")
def test_rescore_skips_when_no_resume(mock_assess, mock_evidence, db_session):
    with patch("backend.main.get_db", return_value=iter([db_session])):
        out = _run_match_rescore_task_runner(limit=10)
    assert out["rescored"] == 0
    assert out.get("error") == "no_active_resume"
    mock_assess.assert_not_called()


@patch("backend.main.task_engine")
def test_rescore_endpoint_dispatches(mock_engine):
    mock_engine.submit_task.return_value = "tsk_test"
    res = client.post("/api/match/rescore", json={"limit": 25})
    assert res.status_code == 202
    assert res.json()["task_id"] == "tsk_test"
    mock_engine.submit_task.assert_called_once()
