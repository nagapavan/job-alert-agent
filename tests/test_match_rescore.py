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
def test_score_job_with_engine_shapes(mock_assess, db_session):
    mock_assess.side_effect = lambda *a, **k: _result(
        [RequirementItem(text="Python", status="met")]
    )
    score, analysis, scored, method, at, band, rec = _score_job_with_engine(
        db_session, "resume text", "We need a Python engineer to build distributed APIs."
    )
    assert scored is True
    assert score == 100.0
    assert band == "Strong"
    assert rec == "apply"
    assert method == "requirement_coverage_v1"
    assert at is not None

    # Too-short description -> unscored, and the model is never called.
    mock_assess.reset_mock()
    assert _score_job_with_engine(db_session, "resume", "short") == (
        0, "", False, None, None, None, None
    )
    mock_assess.assert_not_called()


@patch("backend.main.top_evidence", return_value=[])
@patch("backend.main.assess_job_requirements")
def test_rescore_updates_existing_jobs(mock_assess, mock_evidence, db_session):
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
        Job(company_id=1, title="Sales", description="We need a sales representative to own the enterprise territory.", url="u2", status="To Apply", match_score=88.0, match_scored=False),
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
    for job_id in job_ids:
        refreshed = db_session.query(Job).filter(Job.id == job_id).first()
        assert refreshed.match_score == 100.0
        assert refreshed.match_scored is True
        assert refreshed.score_method == "requirement_coverage_v1"
        assert refreshed.match_scored_at is not None


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
