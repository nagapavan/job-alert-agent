"""
Unit and Integration Tests for BackgroundTaskEngine, Cooperative Cancellation,
Progress Reporting, and Task API Endpoints.
All external network/browser calls are strictly mocked.
"""

import time
import threading
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.task_engine import (
    BackgroundTaskEngine,
    TaskStatus,
    TaskProgress,
    CancellationToken
)

client = TestClient(app)


class TestBackgroundTaskEngine:
    def test_task_lifecycle_completion(self):
        engine = BackgroundTaskEngine(max_concurrent_tasks=2)
        done_event = threading.Event()

        def mock_worker(cancel_token=None, progress_cb=None):
            if progress_cb:
                progress_cb(step=1, label="Step 1", items_found=5, total_steps=1)
            done_event.set()
            return {"jobs_found": 5}

        task_id = engine.submit_task(
            task_type="test_scan",
            task_name="Test Worker",
            fn=mock_worker,
            total_steps=1
        )

        assert task_id.startswith("tsk_")
        assert done_event.wait(timeout=2.0), "Worker did not execute in time"

        # Allow thread wrapper to finalize status
        time.sleep(0.05)
        prog = engine.get_task(task_id)
        assert prog is not None
        assert prog.status == TaskStatus.COMPLETED
        assert prog.progress_percentage == 100.0
        assert prog.items_discovered == 5

    def test_task_cooperative_cancellation(self):
        engine = BackgroundTaskEngine(max_concurrent_tasks=2)
        started_event = threading.Event()

        def mock_cancellable_worker(cancel_token=None, progress_cb=None):
            started_event.set()
            # Wait until cancelled
            while True:
                if cancel_token:
                    cancel_token.check()
                time.sleep(0.01)

        task_id = engine.submit_task(
            task_type="long_task",
            task_name="Long Worker",
            fn=mock_cancellable_worker,
            total_steps=5
        )

        assert started_event.wait(timeout=2.0)
        # Cancel the task
        cancelled = engine.cancel_task(task_id)
        assert cancelled is True

        for _ in range(20):
            prog = engine.get_task(task_id)
            if prog and prog.status == TaskStatus.CANCELLED:
                break
            time.sleep(0.02)

        prog = engine.get_task(task_id)
        assert prog is not None
        assert prog.status == TaskStatus.CANCELLED

    def test_clear_completed_tasks(self):
        engine = BackgroundTaskEngine(max_concurrent_tasks=2)
        done_event = threading.Event()

        def fast_worker(**kw):
            done_event.set()
            return {"done": True}

        task_id = engine.submit_task(
            task_type="quick",
            task_name="Quick",
            fn=fast_worker
        )
        assert done_event.wait(timeout=2.0)
        time.sleep(0.05)

        assert engine.get_task(task_id).status == TaskStatus.COMPLETED
        cleared = engine.clear_completed_tasks()
        assert cleared >= 1
        assert engine.get_task(task_id) is None


class TestTaskAPIEndpoints:
    @patch("backend.main._run_google_jobs_task_runner")
    def test_dispatch_google_jobs_task_endpoint(self, mock_runner):
        mock_runner.return_value = {"jobs_found": 10}
        resp = client.post("/api/tasks/discovery/google-jobs", json={
            "queries": ["Senior Staff Engineer Bangalore"],
            "location": "India",
            "use_playwright": False
        })
        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data
        assert data["status"] == "QUEUED"
        assert "Google Jobs discovery scan queued" in data["message"]

    @patch("backend.main._run_linkedin_sync_task_runner")
    def test_dispatch_linkedin_sync_task_endpoint(self, mock_runner):
        mock_runner.return_value = {"jobs_found": 3}
        # LinkedIn automation is opt-in and consent-gated.
        client.put("/api/preferences", json={
            "features": {"linkedin_sync": True},
            "consents": {"linkedin_sync": {"ack": True}},
        })
        resp = client.post("/api/tasks/sync/linkedin")
        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data
        assert data["status"] == "QUEUED"

    def test_list_tasks_and_active_endpoints(self):
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        tasks = resp.json()
        assert isinstance(tasks, list)

        active_resp = client.get("/api/tasks/active")
        assert active_resp.status_code == 200
        assert isinstance(active_resp.json(), list)

    def test_unified_system_status_endpoint(self):
        resp = client.get("/api/system/unified-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "active_background_tasks_count" in data
        assert "active_background_tasks" in data
        assert "llm_queue" in data
        assert "queue_depth" in data["llm_queue"]
        assert "max_concurrency" in data["llm_queue"]


class TestQueueAdmission:
    """RUNNING means a worker slot is held, and a queued task can be given up (finding 10)."""

    def test_a_blocked_task_is_queued_not_running(self):
        engine = BackgroundTaskEngine(max_concurrent_tasks=1, queue_timeout_seconds=1.0)
        gate = threading.Event()
        started = threading.Event()

        def slow_worker(cancel_token=None, progress_cb=None):
            started.set()
            gate.wait(3.0)
            return {}

        first = engine.submit_task("test", "First", slow_worker)
        assert started.wait(2.0)
        second = engine.submit_task("test", "Second", slow_worker)

        time.sleep(0.1)
        assert engine.get_task(first).status == TaskStatus.RUNNING
        assert engine.get_task(second).status == TaskStatus.QUEUED

        gate.set()
        assert engine.get_task(first) is not None

    def test_waiting_past_the_timeout_cancels_instead_of_running(self):
        engine = BackgroundTaskEngine(max_concurrent_tasks=1, queue_timeout_seconds=0.2)
        gate = threading.Event()
        started = threading.Event()

        def slow_worker(cancel_token=None, progress_cb=None):
            started.set()
            gate.wait(3.0)
            return {}

        engine.submit_task("test", "First", slow_worker)
        assert started.wait(2.0)
        second = engine.submit_task("test", "Second", slow_worker)

        deadline = time.time() + 3.0
        while time.time() < deadline and engine.get_task(second).status != TaskStatus.CANCELLED:
            time.sleep(0.05)

        assert engine.get_task(second).status == TaskStatus.CANCELLED
        assert "no worker slot" in engine.get_task(second).step_label
        gate.set()

    def test_cancel_while_queued_never_executes_the_work(self):
        engine = BackgroundTaskEngine(max_concurrent_tasks=1, queue_timeout_seconds=5.0)
        gate = threading.Event()
        started = threading.Event()
        ran = threading.Event()

        def slow_worker(cancel_token=None, progress_cb=None):
            started.set()
            gate.wait(3.0)
            return {}

        def should_not_run(cancel_token=None, progress_cb=None):
            ran.set()
            return {}

        engine.submit_task("test", "First", slow_worker)
        assert started.wait(2.0)
        second = engine.submit_task("test", "Second", should_not_run)
        assert engine.cancel_task(second)

        gate.set()
        deadline = time.time() + 3.0
        while time.time() < deadline and engine.get_task(second).status != TaskStatus.CANCELLED:
            time.sleep(0.05)

        assert engine.get_task(second).status == TaskStatus.CANCELLED
        assert not ran.is_set()


class TestEventStreamBinding:
    def test_clear_event_loop_forgets_only_its_own_loop(self):
        engine = BackgroundTaskEngine(max_concurrent_tasks=1)
        older = MagicMock()
        newer = MagicMock()
        engine.set_event_loop(newer)
        engine.clear_event_loop(older)
        assert engine._loop is newer
        engine.clear_event_loop(newer)
        assert engine._loop is None

    def test_broadcast_failure_is_logged_not_swallowed(self, caplog):
        engine = BackgroundTaskEngine(max_concurrent_tasks=1)
        closed = MagicMock()
        closed.put_nowait.side_effect = RuntimeError("queue closed")
        engine.register_subscriber(closed)
        progress = TaskProgress(task_id="tsk_x", task_type="t", task_name="n")

        with caplog.at_level("WARNING"):
            engine._broadcast_event(progress)

        assert any(
            "broadcast failed" in record.getMessage().lower()
            for record in caplog.records
        )
