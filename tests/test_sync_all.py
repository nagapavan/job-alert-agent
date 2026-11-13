"""
Tests for the one-shot, feature-flag-aware 'Sync All' orchestrator and its endpoint.
All network/browser stages are mocked; no external calls are made.
"""

from unittest.mock import patch, MagicMock

import threading
import time

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from backend.main import (
    app,
    _run_sync_all_task_runner,
    _run_linkedin_sync_task_runner,
    _run_queued_linkedin_scan,
    _execute_ats_scrape,
    _LINKEDIN_SCAN_GATE,
    CancellationToken,
    LinkedInSyncResponse,
    ScrapeRequest,
    ScrapeResponse,
    ExternalSyncResponse,
)
from backend.database import Company, LinkedInScanRequest, OperationLog
from backend import linkedin_scan_queue

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


class TestSyncAllSessionHygiene:
    """A stage error must not poison the shared session or the run's own bookkeeping (#114)."""

    @patch("backend.main.is_feature_enabled", side_effect=lambda db, f: f == "ats_hirist")
    @patch("backend.main.get_feature_flags")
    @patch("backend.main._execute_external_sync")
    @patch("backend.main._execute_ats_scrape")
    def test_poisoned_stage_is_rolled_back_and_run_log_persisted(
        self, mock_ats, mock_ext, mock_flags, _mock_flag, db_session
    ):
        mock_flags.return_value = {
            "ats_portals": True,
            "google_jobs": False,
            "linkedin_sync": False,
        }

        def _poison_stage(_cb):
            # A failed statement leaves the session needing a rollback (PendingRollbackError).
            db_session.execute(text("INSERT INTO no_such_table VALUES (1)"))

        mock_ats.side_effect = _poison_stage
        mock_ext.return_value = ExternalSyncResponse(message="ok")

        with patch("backend.main.get_db", return_value=_iter_db(db_session)):
            result = _run_sync_all_task_runner()

        assert result["status"] == "partial"
        assert result["failed"] == 1
        assert result["completed"] == 1

        log = (
            db_session.query(OperationLog)
            .filter(OperationLog.operation_type == "sync_all")
            .first()
        )
        assert log is not None
        assert log.status == "Partial"

    @patch("backend.main.is_feature_enabled", side_effect=lambda db, f: f == "ats_hirist")
    @patch("backend.main.get_feature_flags")
    @patch("backend.main._execute_external_sync")
    @patch("backend.main._execute_ats_scrape")
    def test_run_log_persist_failure_does_not_fail_the_run(
        self, mock_ats, mock_ext, mock_flags, _mock_flag, db_session
    ):
        mock_flags.return_value = {
            "ats_portals": True,
            "google_jobs": False,
            "linkedin_sync": False,
        }
        mock_ats.return_value = ScrapeResponse(
            jobs_found=1, jobs_added=1, companies_updated=0, message="ok"
        )
        mock_ext.return_value = ExternalSyncResponse(message="ok")
        real_commit = db_session.commit

        def _commit_fails_on_run_log():
            # Only the run's own OperationLog write fails; preference bookkeeping still commits.
            pending = [
                obj
                for obj in db_session.new
                if isinstance(obj, OperationLog) and obj.operation_type == "sync_all"
            ]
            if pending:
                raise OperationalError(
                    "INSERT", {}, Exception("database is locked")
                )
            return real_commit()

        with patch("backend.main.get_db", return_value=_iter_db(db_session)), patch.object(
            db_session, "commit", side_effect=_commit_fails_on_run_log
        ):
            result = _run_sync_all_task_runner()

        assert result["status"] == "success"
        assert result["completed"] == 2


class TestLinkedInScanMutualExclusion:
    """Only one LinkedIn saved-jobs scan may drive the single-owner browser profile (#114/#115)."""

    @patch("backend.main._submit_queued_linkedin_scan", return_value="tsk_queued01")
    @patch("backend.main.has_consent", return_value=True)
    @patch("backend.main.is_feature_enabled", return_value=True)
    def test_manual_sync_is_queued_while_a_scan_runs(
        self, _flag, _consent, _submit, db_session
    ):
        assert _LINKEDIN_SCAN_GATE.try_acquire()
        try:
            res = client.post("/api/linkedin/sync")
        finally:
            _LINKEDIN_SCAN_GATE.release()

        assert res.status_code == 202
        body = res.json()
        assert body["queued"] is True
        assert body["task_id"] == "tsk_queued01"
        assert body["queue_position"] == 1
        assert "queued" in body["message"].lower()

        row = db_session.query(LinkedInScanRequest).first()
        assert row is not None
        assert row.status == linkedin_scan_queue.STATUS_QUEUED
        assert row.origin == "manual"
        assert row.id == body["request_id"]

    @patch("backend.playwright_app.is_profile_in_use", return_value=False)
    @patch("backend.main.has_consent", return_value=True)
    @patch("backend.main.is_feature_enabled", return_value=True)
    def test_background_stage_skips_while_a_scan_runs(
        self, _flag, _consent, _profile, db_session
    ):
        assert _LINKEDIN_SCAN_GATE.try_acquire()
        try:
            with patch("backend.main.get_db", return_value=_iter_db(db_session)):
                result = _run_linkedin_sync_task_runner()
        finally:
            _LINKEDIN_SCAN_GATE.release()

        assert result["skipped"] == "linkedin_sync_in_progress"
        assert result["jobs_found"] == 0


class TestLinkedInScanQueue:
    """A colliding manual scan is queued, runs when the gate frees, and survives a restart (#115)."""

    def test_queue_positions_follow_arrival_order(self, db_session):
        first = linkedin_scan_queue.enqueue(db_session, origin="manual")
        second = linkedin_scan_queue.enqueue(db_session, origin="manual")

        assert linkedin_scan_queue.queue_position(db_session, first.id) == 1
        assert linkedin_scan_queue.queue_position(db_session, second.id) == 2

    def test_gate_hands_ownership_to_waiters_in_arrival_order(self):
        order = []
        first_ready = threading.Event()
        assert _LINKEDIN_SCAN_GATE.try_acquire()
        try:
            def _waiter(name, started):
                started.set()
                if _LINKEDIN_SCAN_GATE.acquire(timeout=5):
                    order.append(name)
                    _LINKEDIN_SCAN_GATE.release()

            t1 = threading.Thread(target=_waiter, args=("first", first_ready))
            t1.start()
            first_ready.wait(5)
            t2 = threading.Thread(target=_waiter, args=("second", threading.Event()))
            t2.start()
            time.sleep(0.2)  # let both waiters register in order
        finally:
            _LINKEDIN_SCAN_GATE.release()

        t1.join(5)
        t2.join(5)
        assert order == ["first", "second"]

    @patch("backend.main._sync_linkedin_alerts_and_saved")
    def test_queued_request_runs_and_completes_when_gate_frees(
        self, mock_sync, db_session
    ):
        row = linkedin_scan_queue.enqueue(db_session, origin="manual")
        row_id = row.id
        mock_sync.return_value = LinkedInSyncResponse(
            total_processed=3,
            recommendations_found=0,
            recommendations_added=0,
            saved_jobs_checked=3,
            saved_jobs_added=1,
            already_existing=2,
            dismissed_filtered=0,
            closed_jobs_pruned=0,
            message="LinkedIn sync complete: scanned 3.",
        )

        with patch("backend.main.get_db", return_value=_iter_db(db_session)):
            result = _run_queued_linkedin_scan(row_id)

        assert result["saved_jobs_added"] == 1
        db_session.expunge_all()
        finished = db_session.get(LinkedInScanRequest, row_id)
        assert finished.status == linkedin_scan_queue.STATUS_COMPLETED
        assert "scanned 3" in finished.summary
        # The gate must be free again for the next scan.
        assert _LINKEDIN_SCAN_GATE.try_acquire()
        _LINKEDIN_SCAN_GATE.release()

    @patch("backend.main._sync_linkedin_alerts_and_saved")
    def test_cancelled_waiter_never_scans(self, mock_sync, db_session):
        row = linkedin_scan_queue.enqueue(db_session, origin="manual")
        row_id = row.id
        assert _LINKEDIN_SCAN_GATE.try_acquire()
        try:
            token = CancellationToken()
            token.cancel()
            with patch("backend.main.get_db", return_value=_iter_db(db_session)):
                result = _run_queued_linkedin_scan(row_id, cancel_token=token)
        finally:
            _LINKEDIN_SCAN_GATE.release()

        assert result["skipped"] == "cancelled_before_start"
        mock_sync.assert_not_called()
        db_session.expunge_all()
        assert (
            db_session.get(LinkedInScanRequest, row_id).status
            == linkedin_scan_queue.STATUS_CANCELLED
        )

    def test_stop_cancels_queued_requests(self, db_session):
        row = linkedin_scan_queue.enqueue(db_session, origin="manual")
        row_id = row.id

        res = client.post("/api/linkedin/sync/stop")

        assert res.status_code == 200
        assert "stopped" in res.json()["message"]
        db_session.expunge_all()
        assert (
            db_session.get(LinkedInScanRequest, row_id).status
            == linkedin_scan_queue.STATUS_CANCELLED
        )

    def test_restart_requeues_queued_and_retires_running_rows(self, db_session):
        queued = linkedin_scan_queue.enqueue(db_session, origin="manual")
        running = LinkedInScanRequest(
            origin="manual", status=linkedin_scan_queue.STATUS_RUNNING
        )
        db_session.add(running)
        db_session.commit()

        requeued = linkedin_scan_queue.recover_stale_requests(db_session)

        queued_id, running_id = queued.id, running.id
        assert requeued == [queued_id]
        db_session.expunge_all()
        assert (
            db_session.get(LinkedInScanRequest, running_id).status
            == linkedin_scan_queue.STATUS_CANCELLED
        )
        assert (
            db_session.get(LinkedInScanRequest, queued_id).status
            == linkedin_scan_queue.STATUS_QUEUED
        )


class TestAtsScrapeBatchCommits:
    """The scrape must not hold the SQLite write lock for the whole company loop (#114)."""

    @patch("backend.main.scrape_greenhouse_jobs")
    def test_commits_once_per_company(self, mock_gh, db_session):
        db_session.add_all(
            [
                Company(
                    name="Acme",
                    domain="acme.example",
                    careers_url="https://boards.greenhouse.io/acme",
                ),
                Company(
                    name="Globex",
                    domain="globex.example",
                    careers_url="https://boards.greenhouse.io/globex",
                ),
            ]
        )
        db_session.commit()
        mock_gh.return_value = [
            {
                "title": "Senior Backend Engineer",
                "url": "https://boards.greenhouse.io/acme/jobs/1",
                "description": "Python and PostgreSQL.",
                "location": "Remote",
                "source": "Greenhouse",
                "source_type": "Direct",
            }
        ]

        with patch.object(db_session, "commit", wraps=db_session.commit) as mock_commit:
            result = _execute_ats_scrape(ScrapeRequest(), db_session)

        assert result.jobs_added == 2
        # One batch commit per company plus the closing OperationLog write.
        assert mock_commit.call_count >= 3

