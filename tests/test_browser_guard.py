"""The suite must never be able to launch a real browser.

A stale ``patch("backend.main.sync_playwright")`` in a test whose code moved to a router does not
fail — it patches a name nothing reads and the real Chromium runs, hitting the live site. On
2026-11-18 that produced seven live Google for Jobs fetches and an "unusual traffic" interstitial
from a test run. The guard therefore lives on ``BrowserType`` itself, which every module's imported
``sync_playwright`` still reaches.
"""

import pytest
from playwright.sync_api import BrowserType


def test_launching_a_real_browser_is_blocked():
    with pytest.raises(RuntimeError, match="real browser"):
        BrowserType.launch(object())


def test_launching_a_persistent_context_is_blocked():
    with pytest.raises(RuntimeError, match="real browser"):
        BrowserType.launch_persistent_context(object(), "/tmp/never-used-profile")
