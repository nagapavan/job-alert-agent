"""Environment parsing bounds and secret-file permissions (findings 16 / 19)."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from backend.config import env_float, env_int, write_private_file

ROOT = Path(__file__).resolve().parent.parent


def test_bad_numeric_env_falls_back_instead_of_raising(monkeypatch):
    monkeypatch.setenv("JAA_BAD_INT", "not-a-number")
    assert env_int("JAA_BAD_INT", 120, minimum=1) == 120

    monkeypatch.setenv("JAA_BAD_FLOAT", "half")
    assert env_float("JAA_BAD_FLOAT", 0.5, minimum=0.0) == 0.5


def test_lower_bounds_stop_a_zero_from_disabling_safety(monkeypatch):
    monkeypatch.setenv("JAA_ZERO_INT", "0")
    assert env_int("JAA_ZERO_INT", 10, minimum=1) == 1

    monkeypatch.setenv("JAA_JITTER", "9")
    assert env_float("JAA_JITTER", 0.5, minimum=0.0, maximum=1.0) == 1.0


def test_module_constants_carry_their_bounds_at_import():
    """The knobs finding 16 names must not accept a disabling value from the environment."""
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "LINKEDIN_SAVED_JOB_DESC_FETCH_LIMIT": "0",
            "SCRAPE_PACING_JITTER": "9",
            "OPERATION_LOG_MAX_ROWS": "0",
            "LLM_TIMEOUT": "not-a-number",
        }
    )
    code = (
        "import backend.config as c, backend.database as d;"
        "print(c.LINKEDIN_SAVED_JOB_DESC_FETCH_LIMIT, c.SCRAPE_PACING_JITTER,"
        " d.OPERATION_LOG_MAX_ROWS, c.LLM_TIMEOUT)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["1", "1.0", "1", "120"]


def test_private_files_are_created_with_0600(tmp_path):
    """The mode is applied at creation, not by a chmod after the write (finding 19)."""
    target = tmp_path / "secret.txt"
    previous = os.umask(0o777)
    try:
        write_private_file(target, "s3cr3t")
    finally:
        os.umask(previous)

    assert target.read_text() == "s3cr3t"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_failing_chmod_is_logged_not_swallowed(tmp_path, caplog, monkeypatch):
    import backend.config as config_module

    def _deny(*_args, **_kwargs):
        raise OSError("operation not permitted")

    target = tmp_path / "secret.txt"
    monkeypatch.setattr(config_module.os, "chmod", _deny)

    with caplog.at_level("WARNING"):
        write_private_file(target, "s3cr3t")

    assert any(
        "permissions" in record.getMessage().lower() for record in caplog.records
    )
