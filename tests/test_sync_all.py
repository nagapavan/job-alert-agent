"""
Tests for the one-shot, feature-flag-aware 'Sync All' orchestrator and its endpoint.
All network/browser stages are mocked; no external calls are made.
"""

from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import (
    app,
    _run_sync_all_task_runner,
    ScrapeResponse,
    ExternalSyncResponse,
)

client = TestClient(app)


def _iter_db(session):
    return iter([session])


class TestSyncAllEndpoint:
    @patch("backend.main._run_sync_all_task_runner")
    def test_endpoint_queues_task(self, mock_runner):
        mock_runner.return_value = {"status": "success"}
        resp = client.post("/api/tasks/sync/all")
        assert resp.status_code == 202
        data = resp.json()
        assert data["task_id"].startswith("tsk_")
        assert "channel" in data["message"].lower()


class TestSyncAllOrchestrator:
    @patch("backend.main.is_feature_enabled", side_effect=lambda db, f: f == "ats_hirist")
    @patch("backend.main.get_feature_flags")
    @patch("backend.main._execute_external_sync")
    @patch("backend.main._run_google_jobs_task_runner")
    @patch("backend.main._execute_ats_scrape")
    def test_runs_enabled_and_skips_disabled(
        self, mock_ats, mock_google, mock_ext, mock_flags, _mock_flag, db_session
    ):
        mock_flags.return_value = {"ats_portals": True, "google_jobs": False, "linkedin_sync": False}
        mock_ats.return_value = ScrapeResponse(jobs_found=1, jobs_added=1, companies_updated=0, message="ok")
        mock_ext.return_value = ExternalSyncResponse(message="ok")

        with patch("backend.main.get_db", return_value=_iter_db(db_session)):
            result = _run_sync_all_task_runner()

        mock_ats.assert_called_once()
        mock_google.assert_not_called()
        mock_ext.assert_called_once()
        assert result["completed"] == 2
        assert result["failed"] == 0

    @patch("backend.main.is_feature_enabled", side_effect=lambda db, f: f == "ats_hirist")
    @patch("backend.main.get_feature_flags")
    @patch("backend.main._execute_external_sync")
    @patch("backend.main._run_google_jobs_task_runner")
    @patch("backend.main._execute_ats_scrape")
    def test_continues_after_stage_failure(
        self, mock_ats, mock_google, mock_ext, mock_flags, _mock_flag, db_session
    ):
        mock_flags.return_value = {"ats_portals": True, "google_jobs": False, "linkedin_sync": False}
        mock_ats.side_effect = RuntimeError("portal scan blew up")
        mock_ext.return_value = ExternalSyncResponse(message="ok")

        with patch("backend.main.get_db", return_value=_iter_db(db_session)):
            result = _run_sync_all_task_runner()

        assert result["failed"] == 1
        assert result["completed"] == 1
        mock_ext.assert_called_once()

    @patch("backend.main.has_consent", return_value=False)
    @patch("backend.main.is_feature_enabled", side_effect=lambda db, f: f == "ats_hirist")
    @patch("backend.main.get_feature_flags")
    @patch("backend.main._run_linkedin_sync_task_runner")
    @patch("backend.main._execute_external_sync")
    @patch("backend.main._run_google_jobs_task_runner")
    @patch("backend.main._execute_ats_scrape")
    def test_linkedin_stage_skipped_without_consent(
        self,
        mock_ats,
        mock_google,
        mock_ext,
        mock_li,
        mock_flags,
        _mock_flag,
        _mock_consent,
        db_session,
    ):
        """A consent-required channel is omitted from Sync All even when its flag is on."""
        mock_flags.return_value = {"ats_portals": False, "google_jobs": False, "linkedin_sync": True}
        mock_ext.return_value = ExternalSyncResponse(message="ok")

        with patch("backend.main.get_db", return_value=_iter_db(db_session)):
            _run_sync_all_task_runner()

        mock_li.assert_not_called()
