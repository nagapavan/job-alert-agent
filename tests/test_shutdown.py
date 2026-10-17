"""Tests for graceful shutdown (backend/shutdown.py).

Shutdown is driven by the ASGI shutdown lifecycle, NOT by our own signal handlers:
uvicorn owns SIGTERM/SIGINT and calls the lifecycle on every graceful-exit path. The
regression this guards against is a clean drain followed by *no reload* and a dead port,
caused by hijacking uvicorn's signal handling.
"""
import signal
from unittest.mock import MagicMock

import pytest

from backend import shutdown


@pytest.fixture(autouse=True)
def reset_shutdown_state():
    """Isolate module-level shutdown state between tests."""
    shutdown._shutting_down = False
    yield
    shutdown._shutting_down = False


class TestFlag:
    def test_mark_once(self):
        assert shutdown._mark_shutting_down() is True
        assert shutdown._mark_shutting_down() is False
        assert shutdown.is_shutting_down() is True


class TestNoSignalHijacking:
    def test_no_signal_handlers_registered(self):
        """We must not clobber uvicorn's handler; otherwise --reload breaks.

        Two observed failure modes: raising out of the handler killed the reloader
        parent, and swallowing the signal left the child alive so the reloader blocked
        forever in process.join().
        """
        assert not hasattr(shutdown, "install_shutdown_handlers")
        assert not hasattr(shutdown, "_handle_signal")
        # The module must not even import the signal module any more.
        assert not hasattr(shutdown, "signal")

    def test_module_exposes_only_drain_api(self):
        for name in ("run_shutdown", "is_shutting_down", "_drain"):
            assert hasattr(shutdown, name)


class TestDrain:
    def test_run_shutdown_is_idempotent(self, monkeypatch):
        calls = {"n": 0}

        def fake_drain(deadline):
            calls["n"] += 1

        monkeypatch.setattr(shutdown, "_drain", fake_drain)
        shutdown.run_shutdown()
        shutdown.run_shutdown()
        assert calls["n"] == 1

    def test_drain_stops_subsystems_in_order(self, monkeypatch):
        order = []

        fake_scheduler = MagicMock()
        fake_scheduler.stop.side_effect = lambda: order.append("scheduler")
        monkeypatch.setattr("backend.scheduler.scheduler", fake_scheduler)

        fake_engine = MagicMock()
        fake_engine.cancel_all.side_effect = lambda: order.append("tasks") or 1
        fake_engine.get_active_tasks.return_value = []
        monkeypatch.setattr("backend.task_engine.task_engine", fake_engine)

        fake_queue = MagicMock()
        fake_queue.stop.side_effect = lambda: order.append("llm")
        monkeypatch.setattr("backend.llm_queue.llm_queue", fake_queue)

        monkeypatch.setattr(
            "backend.playwright_app.clean_stale_profile_locks",
            lambda: order.append("profile") or True,
        )

        shutdown._drain(deadline=0)
        # Profile lock must be released LAST, after nothing is using it.
        assert order == ["scheduler", "tasks", "llm", "profile"]

    def test_drain_survives_subsystem_errors(self, monkeypatch):
        monkeypatch.setattr(
            "backend.scheduler.scheduler",
            MagicMock(stop=MagicMock(side_effect=RuntimeError("boom"))),
        )
        monkeypatch.setattr(
            "backend.task_engine.task_engine",
            MagicMock(cancel_all=MagicMock(side_effect=RuntimeError("boom"))),
        )
        # Must not raise despite every subsystem failing.
        shutdown._drain(deadline=0)

    def test_drain_does_not_delete_live_profile_locks(self, monkeypatch):
        """A live browser owns the profile -> locks must be left intact."""
        monkeypatch.setattr("backend.scheduler.scheduler", MagicMock())
        monkeypatch.setattr(
            "backend.task_engine.task_engine",
            MagicMock(cancel_all=MagicMock(return_value=0), get_active_tasks=MagicMock(return_value=[])),
        )
        monkeypatch.setattr("backend.llm_queue.llm_queue", MagicMock())
        monkeypatch.setattr(
            "backend.playwright_app.clean_stale_profile_locks", lambda: False
        )
        shutdown._drain(deadline=0)  # must not raise


class TestHealthEndpoint:
    def test_health_reports_shutting_down(self):
        from conftest import test_client as client

        res = client.get("/api/health")
        assert res.status_code == 200
        assert res.json()["shutting_down"] is False

        shutdown._shutting_down = True
        res = client.get("/api/health")
        assert res.json()["shutting_down"] is True
        assert res.json()["status"] == "shutting_down"


class TestComponentStops:
    def test_task_engine_cancel_all(self):
        from backend.task_engine import task_engine

        assert task_engine.cancel_all() == 0

    def test_llm_queue_stop_sets_flag(self):
        from backend.llm_queue import llm_queue

        original = llm_queue._running
        try:
            llm_queue.stop()
            assert llm_queue._running is False
        finally:
            llm_queue._running = original
