"""The interactive login window is a task-engine-owned, cancellable task (finding 5).

The old implementation started an untracked daemon thread that slept 600 s, so the
shutdown drain could not cancel it and the single-owner Chrome profile stayed locked.
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.config import env_int
from backend.playwright_app import run_interactive_session
from backend.task_engine import CancellationToken


class FakePage:
    def __init__(self, on_wait=None):
        self.goto = MagicMock()
        self.bring_to_front = MagicMock()
        self._on_wait = on_wait
        self._closed = False

    def is_closed(self):
        return self._closed

    def wait_for_timeout(self, _ms):
        if self._on_wait:
            self._on_wait(self)


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.close = MagicMock()

    def new_page(self):
        return self.pages[0]


class FakePlaywright:
    def __init__(self, context):
        self._context = context

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _run(page, context, token=None, hold_seconds=60, configured_hold=None):
    with patch("playwright.sync_api.sync_playwright", return_value=FakePlaywright(context)), patch(
        "backend.playwright_app.clean_stale_profile_locks", return_value=True
    ) as clean, patch(
        "backend.playwright_app.get_persistent_browser_context", return_value=context
    ), patch(
        "backend.playwright_app._activate_browser_window"
    ):
        if configured_hold is not None:
            with patch(
                "backend.playwright_app.INTERACTIVE_SESSION_HOLD_SECONDS", configured_hold
            ):
                result = run_interactive_session(
                    "https://example.test/login",
                    cancel_token=token,
                    hold_seconds=hold_seconds,
                )
        else:
            result = run_interactive_session(
                "https://example.test/login",
                cancel_token=token,
                hold_seconds=hold_seconds,
            )
    return result, clean


def test_cancel_stops_the_hold_and_releases_the_profile():
    token = CancellationToken()
    page = FakePage(on_wait=lambda _page: token.cancel())
    context = FakeContext(page)

    with pytest.raises(InterruptedError):
        _run(page, context, token=token, hold_seconds=600)

    assert context.close.called


def test_user_closing_the_window_ends_the_session():
    def close_page(page):
        page._closed = True

    page = FakePage(on_wait=close_page)
    context = FakeContext(page)

    result, clean = _run(page, context, hold_seconds=600)

    assert result["closed_reason"] == "closed_by_user"
    assert context.close.called
    assert clean.called


def test_expired_hold_closes_the_context():
    context = FakeContext(FakePage())

    result, _ = _run(FakePage(), context, hold_seconds=0)

    assert result["closed_reason"] == "hold_expired"
    assert context.close.called


def test_default_hold_comes_from_config_not_a_ten_minute_sleep():
    result, _ = _run(FakePage(), FakeContext(FakePage()), hold_seconds=None, configured_hold=42)

    assert result["hold_seconds"] == 42


def test_busy_profile_is_reported_without_launching():
    with patch(
        "backend.playwright_app.clean_stale_profile_locks", return_value=False
    ), patch("playwright.sync_api.sync_playwright") as launch:
        with pytest.raises(Exception, match="in use"):
            run_interactive_session("https://example.test/login")

    assert not launch.called


def test_env_int_falls_back_and_clamps(monkeypatch):
    monkeypatch.setenv("JAA_TEST_ENV_INT", "not-a-number")
    assert env_int("JAA_TEST_ENV_INT", 7) == 7

    monkeypatch.setenv("JAA_TEST_ENV_INT", "-5")
    assert env_int("JAA_TEST_ENV_INT", 7, minimum=1) == 1

    monkeypatch.setenv("JAA_TEST_ENV_INT", "9999")
    assert env_int("JAA_TEST_ENV_INT", 7, maximum=300) == 300

    monkeypatch.delenv("JAA_TEST_ENV_INT")
    assert env_int("JAA_TEST_ENV_INT", 300, minimum=30, maximum=1800) == 300
