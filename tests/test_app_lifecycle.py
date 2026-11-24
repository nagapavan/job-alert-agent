"""Import purity and middleware ordering.

Two defects shared one root cause: work that belongs to a *running* server happened at import
time, and a middleware that must run *inside* another was declared outside it.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from conftest import test_client as client

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- import purity

def test_importing_the_app_does_not_open_the_database(tmp_path):
    """Importing a module must not create a schema, run migrations or flip WAL on a database."""
    db_file = tmp_path / "import-only.db"
    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_file}",
            "JOB_ALERT_AGENT_NO_SCHEDULER": "1",
            "PYTHONPATH": str(ROOT),
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import backend.main"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    assert not db_file.exists(), "importing backend.main still initialised the database"


def test_the_three_startup_handlers_are_one_lifespan():
    """init_db()/observability run first inside the lifespan; the drain runs on exit (#25)."""
    import inspect

    from backend import main as main_module

    app = main_module.app
    assert app.router.on_startup == []
    assert app.router.on_shutdown == []

    source = inspect.getsource(main_module._lifespan)
    assert source.index("_bootstrap_runtime()") < source.index(
        "_recover_queued_linkedin_scans()"
    )
    assert "_graceful_shutdown_drain()" in source


def test_the_lifespan_runs_startup_then_drain(monkeypatch):
    """Startup work happens before the server serves, and the drain runs after it stops."""
    import asyncio

    from backend import main as main_module

    calls = []
    monkeypatch.setattr(main_module, "_bootstrap_runtime", lambda: calls.append("bootstrap"))
    monkeypatch.setattr(
        main_module, "_startup_operation_log_maintenance", lambda: calls.append("maintenance")
    )
    monkeypatch.setattr(
        main_module, "_recover_queued_linkedin_scans", lambda: calls.append("recover")
    )
    monkeypatch.setattr(
        main_module, "_graceful_shutdown_drain", lambda: calls.append("drain")
    )

    async def run():
        async with main_module._lifespan(main_module.app):
            calls.append("serving")

    asyncio.run(run())

    assert calls == ["bootstrap", "maintenance", "recover", "serving", "drain"]


# ---------------------------------------------------------------- middleware order

def test_a_firewall_rejection_is_logged_with_a_request_id():
    """The firewall returns 401 without call_next, so observability must wrap it (#92/#93)."""
    from backend import main as main_module

    with patch.object(main_module, "logger") as mock_logger:
        res = client.get("/api/jobs", headers={"origin": "https://evil.example"})

    assert res.status_code == 401
    assert res.headers.get("X-Request-ID"), "a rejection must be correlatable"
    logged = [str(call.args[0]) for call in mock_logger.info.call_args_list if call.args]
    assert any("-> 401" in line for line in logged), logged
