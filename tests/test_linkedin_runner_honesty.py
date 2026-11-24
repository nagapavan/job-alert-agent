"""LinkedIn background-run honesty: scoring, exception status and channel outcomes.

Findings 58-60. The background runner used to score every alert at 0, the manual route
reported an exception as a 200 with counts it never committed, and neither path recorded a
per-channel outcome, so "no jobs" and "the scan blew up" looked the same in the run history.
"""

import json
from unittest.mock import MagicMock, patch

from backend.database import (
    Company,
    Job,
    OperationLog,
    get_user_preferences,
    record_consent,
)


def _enable_linkedin(db):
    pref = get_user_preferences(db)
    pref.features_json = '{"linkedin_sync": true}'
    db.commit()
    record_consent(db, "linkedin_sync")


def _alert(**overrides):
    payload = {
        "title": "Staff Software Engineer",
        "company": "Acme",
        "location": "Remote, India",
        "url": "https://www.linkedin.com/jobs/view/123",
        "description": "Acme is hiring staff engineers.",
        "source": "LinkedIn Job Alert",
        "source_type": "LinkedIn",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- finding 58

@patch("backend.routers.linkedin.sync_playwright")
@patch("backend.routers.linkedin._score_provenance", return_value={})
@patch("backend.routers.linkedin._score_job_with_engine")
@patch("backend.routers.linkedin.evaluate_job_due_diligence", return_value={"is_ghost_job": False})
@patch("backend.routers.linkedin.generate_embeddings", return_value=[0.0] * 8)
@patch("backend.routers.linkedin.apply_dismissal_filter")
@patch("backend.routers.linkedin.get_db")
@patch("backend.routers.linkedin.scan_and_sync_linkedin_saved_jobs", return_value=[])
@patch("backend.routers.linkedin.scan_linkedin_job_alerts")
@patch("backend.playwright_app.is_profile_in_use", return_value=False)
def test_background_alert_rows_are_scored_not_zero(
    _mock_in_use,
    mock_alerts,
    _mock_saved,
    mock_get_db,
    mock_dismissal,
    _mock_emb,
    _mock_dd,
    mock_score,
    _mock_prov,
    mock_playwright,
    db_session,
):
    from backend.routers.linkedin import _run_linkedin_sync_task_runner

    mock_get_db.return_value = iter([db_session])
    mock_playwright.return_value = MagicMock()
    mock_alerts.return_value = [_alert()]
    mock_dismissal.return_value = MagicMock(match=False, status="To Apply", analysis=None)
    mock_score.return_value = (
        88.0,
        "Strong match.",
        True,
        "classifier_v1",
        None,
        "Strong",
        "apply",
        ["Staff scope"],
        [],
        [],
    )
    _enable_linkedin(db_session)

    result = _run_linkedin_sync_task_runner()

    assert result["alerts_ingested"] == 1
    job = db_session.query(Job).filter(Job.title == "Staff Software Engineer").one()
    assert job.match_score == 88.0
    assert job.match_scored is True
    assert job.score_method == "classifier_v1"


# ---------------------------------------------------------------- finding 59

@patch("backend.routers.linkedin.sync_playwright")
def test_manual_sync_failure_returns_500_with_unpersisted_counts_zeroed(
    mock_playwright, db_session
):
    from backend.routers.linkedin import _sync_linkedin_alerts_and_saved

    mock_playwright.side_effect = RuntimeError("browser crashed")
    _enable_linkedin(db_session)

    response = _sync_linkedin_alerts_and_saved(db_session)

    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["recommendations_added"] == 0
    assert body["saved_jobs_added"] == 0
    assert body["closed_jobs_pruned"] == 0
    assert "failed" in body["message"].lower()
    assert "browser crashed" in body["message"]


# ---------------------------------------------------------------- finding 60

@patch("backend.routers.linkedin.sync_playwright")
@patch("backend.routers.linkedin.get_db")
@patch("backend.routers.linkedin.scan_and_sync_linkedin_saved_jobs")
@patch("backend.routers.linkedin.scan_linkedin_job_alerts")
@patch("backend.playwright_app.is_profile_in_use", return_value=False)
def test_background_run_records_a_channel_outcome_per_scan(
    _mock_in_use, mock_alerts, mock_saved, mock_get_db, mock_playwright, db_session
):
    from backend.routers.linkedin import _run_linkedin_sync_task_runner

    mock_get_db.return_value = iter([db_session])
    mock_playwright.return_value = MagicMock()
    mock_saved.side_effect = RuntimeError("saved jobs scan exploded")
    mock_alerts.side_effect = RuntimeError("alerts scan exploded")
    _enable_linkedin(db_session)

    result = _run_linkedin_sync_task_runner()

    by_channel = {entry["channel"]: entry for entry in result["channels"]}
    assert by_channel["linkedin_saved_jobs"]["outcome"] == "error"
    assert by_channel["linkedin_alerts"]["outcome"] == "error"

    log = (
        db_session.query(OperationLog)
        .filter(OperationLog.operation_type == "linkedin_sync")
        .order_by(OperationLog.id.desc())
        .first()
    )
    details = json.loads(log.details_json)
    assert {entry["channel"] for entry in details["channels"]} == {
        "linkedin_saved_jobs",
        "linkedin_alerts",
    }
