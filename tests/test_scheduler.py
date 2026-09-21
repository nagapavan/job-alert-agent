"""
Unit and API Integration Tests for PeriodicScheduler and Schedule Management Endpoints.
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.scheduler import scheduler, ScheduledJobConfig

client = TestClient(app)


class TestPeriodicScheduler:
    def test_scheduler_jobs_initialized(self):
        jobs = scheduler.get_all_jobs()
        assert len(jobs) >= 2
        ids = [j.id for j in jobs]
        assert "sched_google_jobs" in ids
        assert "sched_linkedin_sync" in ids

    def test_update_job_config(self):
        updated = scheduler.update_job("sched_google_jobs", is_enabled=True, interval_hours=4.5)
        assert updated is not None
        assert updated.interval_hours == 4.5
        assert updated.is_enabled is True

    @patch("backend.main._run_google_jobs_task_runner")
    def test_run_job_now_triggers_background_task(self, mock_runner):
        mock_runner.return_value = {"jobs_found": 5}
        task_id = scheduler.run_job_now("sched_google_jobs")
        assert task_id is not None
        assert task_id.startswith("tsk_")

        job = scheduler._jobs.get("sched_google_jobs")
        assert job.last_task_id == task_id
        assert job.last_run_at is not None


class TestSchedulerAPIEndpoints:
    def test_list_scheduled_jobs_endpoint(self):
        resp = client.get("/api/scheduler/jobs")
        assert resp.status_code == 200
        jobs = resp.json()
        assert len(jobs) >= 2

    def test_update_scheduled_job_endpoint(self):
        resp = client.patch("/api/scheduler/jobs/sched_linkedin_sync", json={
            "is_enabled": False,
            "interval_hours": 8.0
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "sched_linkedin_sync"
        assert data["is_enabled"] is False
        assert data["interval_hours"] == 8.0

        # Reset back to true
        client.patch("/api/scheduler/jobs/sched_linkedin_sync", json={"is_enabled": True})

    @patch("backend.main._run_linkedin_sync_task_runner")
    def test_trigger_scheduled_job_run_endpoint(self, mock_runner, monkeypatch):
        import backend.database as dbmod
        monkeypatch.setattr(dbmod, "is_feature_enabled", lambda db, feature: True)
        monkeypatch.setattr(dbmod, "has_consent", lambda db, key: True)
        mock_runner.return_value = {"jobs_found": 3}
        resp = client.post("/api/scheduler/jobs/sched_linkedin_sync/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["job_id"] == "sched_linkedin_sync"
        assert "task_id" in data

    @patch("backend.main._run_linkedin_sync_task_runner")
    def test_trigger_run_without_consent_returns_403(self, mock_runner, monkeypatch):
        """A consent-gated job that is skipped must surface 403, not a misleading 404."""
        import backend.database as dbmod
        monkeypatch.setattr(dbmod, "is_feature_enabled", lambda db, feature: True)
        monkeypatch.setattr(dbmod, "has_consent", lambda db, key: False)
        resp = client.post("/api/scheduler/jobs/sched_linkedin_sync/run")
        assert resp.status_code == 403
        assert "consent" in resp.json()["detail"].lower()
        mock_runner.assert_not_called()


def test_scheduler_drops_retired_persisted_jobs(tmp_path, monkeypatch):
    """Retired schedules persisted on disk are pruned and defaults are re-seeded."""
    import json
    import backend.scheduler as sched_mod

    cfg = tmp_path / "scheduler_config.json"
    cfg.write_text(json.dumps([
        {"id": "sched_hiring_posts", "name": "Retired Hiring Posts",
         "task_type": "hiring_posts_scan", "description": "retired",
         "interval_hours": 24.0, "is_enabled": False},
        {"id": "sched_google_jobs", "name": "Google", "task_type": "google_jobs_scan",
         "description": "kept", "interval_hours": 6.0, "is_enabled": True},
    ]))
    monkeypatch.setattr(sched_mod, "SCHEDULER_CONFIG_FILE", cfg)

    fresh = sched_mod.PeriodicScheduler(check_interval_seconds=9999)
    ids = [j.id for j in fresh.get_all_jobs()]
    assert "sched_hiring_posts" not in ids
    assert "sched_google_jobs" in ids
    assert "sched_linkedin_sync" in ids  # default re-seeded


def test_scheduler_skips_disabled_feature_for_automatic_run(monkeypatch):
    """Automatic (non-forced) runs of an opt-in channel are skipped when disabled."""
    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "is_feature_enabled", lambda db, feature: False)
    monkeypatch.setattr(dbmod, "has_consent", lambda db, key: False)
    assert scheduler.run_job_now("sched_linkedin_sync") is None
    # A consent-gated channel is still skipped on an explicit "Run now" without consent.
    with patch("backend.main._run_linkedin_sync_task_runner", return_value={"jobs_found": 0}):
        assert scheduler.run_job_now("sched_linkedin_sync", force=True) is None


def test_scheduler_forced_run_proceeds_with_consent(monkeypatch):
    """A forced run proceeds once the feature is enabled AND consent is recorded."""
    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "is_feature_enabled", lambda db, feature: True)
    monkeypatch.setattr(dbmod, "has_consent", lambda db, key: True)
    with patch("backend.main._run_linkedin_sync_task_runner", return_value={"jobs_found": 0}):
        assert scheduler.run_job_now("sched_linkedin_sync", force=True) is not None
