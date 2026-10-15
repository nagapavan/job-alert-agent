"""Tests for the LLM resilience / exponential-backoff retry layer.

Covers: retryable vs non-retryable exception classification, exponential backoff
retry behavior, max-retry exhaustion, jitter bounds, per-task-profile retry config
resolution, and the wired integration in ``_execute_generate_text`` (429/5xx retried,
400/non-transient not retried).
"""
import time

import pytest
import requests

from backend import llm_resilience
from backend.llm_resilience import (
    LLM_RESILIENCE_RETRYABLE_HTTP,
    is_retryable,
    resolve_retry_config,
    with_retry,
)


# ---------------------------------------------------------------- classification

def test_is_retryable_http_429():
    err = requests.exceptions.HTTPError()
    err.response = requests.models.Response()
    err.response.status_code = 429
    assert is_retryable(err) is True


def test_is_retryable_http_5xx():
    for code in (500, 502, 503, 504):
        err = requests.exceptions.HTTPError()
        err.response = requests.models.Response()
        err.response.status_code = code
        assert is_retryable(err) is True, f"{code} should be retryable"


def test_is_retryable_http_400_not_retried():
    err = requests.exceptions.HTTPError()
    err.response = requests.models.Response()
    err.response.status_code = 400
    assert is_retryable(err) is False


def test_is_retryable_connection_and_timeout():
    assert is_retryable(requests.exceptions.ConnectionError("refused")) is True
    assert is_retryable(requests.exceptions.Timeout("slow")) is True
    assert is_retryable(TimeoutError("timed out")) is True


def test_is_retryable_non_transient_raised():
    assert is_retryable(ValueError("bad input")) is False
    assert is_retryable(KeyError("missing")) is False


def test_resilience_retryable_http_constant_matches_logic():
    assert 429 in LLM_RESILIENCE_RETRYABLE_HTTP
    for code in (500, 502, 503, 504):
        assert code in LLM_RESILIENCE_RETRYABLE_HTTP


# ---------------------------------------------------------------- retry behavior

def test_with_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError("transient")
        return "ok"

    result = with_retry("test_step", flaky, max_retries=3, backoff_base=0.001)
    assert result == "ok"
    assert calls["n"] == 3


def test_with_retry_does_not_retry_first_success():
    calls = {"n": 0}

    def good():
        calls["n"] += 1
        return "ok"

    assert with_retry("test_step", good, max_retries=3, backoff_base=0.001) == "ok"
    assert calls["n"] == 1


def test_with_retry_exhausts_and_raises_last_error():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("down")

    with pytest.raises(requests.exceptions.ConnectionError):
        with_retry("test_step", always_fails, max_retries=2, backoff_base=0.001)
    # 1 initial attempt + 2 retries
    assert calls["n"] == 3


def test_with_retry_propagates_non_retryable_immediately():
    calls = {"n": 0}

    def bad_input():
        calls["n"] += 1
        raise ValueError("bad")

    with pytest.raises(ValueError):
        with_retry("test_step", bad_input, max_retries=3, backoff_base=0.001)
    assert calls["n"] == 1


def test_with_retry_zero_retries_means_single_attempt():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise requests.exceptions.Timeout("slow")

    with pytest.raises(requests.exceptions.Timeout):
        with_retry("test_step", always_fails, max_retries=0, backoff_base=0.001)
    assert calls["n"] == 1


def test_with_retry_backoff_is_monotonic_and_bounded():
    """Verify exponential backoff grows and never exceeds the cap."""
    delays = []
    original_sleep = llm_resilience.time.sleep

    def fake_sleep(d):
        delays.append(d)

    llm_resilience.time.sleep = fake_sleep
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("down")

    try:
        with_retry(
            "test_step",
            always_fails,
            max_retries=3,
            backoff_base=0.5,
            backoff_max=2.0,
            jitter=False,
        )
    except requests.exceptions.ConnectionError:
        pass
    finally:
        llm_resilience.time.sleep = original_sleep

    assert len(delays) == 3
    # backoff: attempt1=0.5, attempt2=1.0, attempt3=2.0 (capped)
    assert delays[0] <= 0.5 + 1e-6
    assert delays[1] >= delays[0]
    assert delays[2] <= 2.0 + 1e-6  # capped at backoff_max


def test_with_retry_jitter_within_bounds():
    """When jitter is enabled, delays stay <= the scheduled value + some margin."""
    delays = []
    original_sleep = llm_resilience.time.sleep
    llm_resilience.time.sleep = lambda d: delays.append(d)
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("down")

    try:
        with_retry(
            "test_step", always_fails, max_retries=2, backoff_base=1.0, jitter=True
        )
    except requests.exceptions.ConnectionError:
        pass
    finally:
        llm_resilience.time.sleep = original_sleep

    assert len(delays) == 2
    # attempt1 base=1.0, attempt2 base=2.0, each jittered to 0.5x..1.5x
    for d in delays:
        assert 0 <= d <= 3.0 + 1e-6


# ---------------------------------------------------------------- config resolution

def test_resolve_retry_config_defaults_when_no_profile():
    max_retries, base, cap, jitter = resolve_retry_config("unknown_task")
    assert max_retries == 3
    assert base == 0.5
    assert cap == 5.0
    assert isinstance(jitter, bool)


def test_resolve_retry_config_honors_per_profile_override(monkeypatch):
    monkeypatch.setattr(
        "backend.ai_helper.TASK_PROFILES",
        {"custom_task": {"max_retries": 7}},
    )
    max_retries, _, _, _ = resolve_retry_config("custom_task")
    assert max_retries == 7


# ---------------------------------------------------------------- integration

def test_execute_generate_text_retries_transient_then_succeeds():
    """A provider that transiently 429s then succeeds must be retried (not failed)."""
    from backend import ai_helper

    calls = {"n": 0}

    def flaky_handler(system_prompt, user_prompt, json_mode, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            err = requests.exceptions.HTTPError("rate limited")
            err.response = requests.models.Response()
            err.response.status_code = 429
            raise err
        return "hello"

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "0.001")
    try:
        monkeypatch.setattr(
            "backend.ai_helper.PROVIDER_REGISTRY",
            {
                "openai": {
                    "id": "openai",
                    "name": "OpenAI",
                    "handler": lambda *a, **kw: flaky_handler(*a, **kw),
                    "is_local": False,
                    "priority": 4,
                }
            },
        )
        monkeypatch.setattr(ai_helper, "is_cloud_fallback_allowed", lambda: True)
        in_func = ai_helper._execute_generate_text
        result = in_func(
            "system", "user", provider="openai", task_type="job_match_scoring"
        )
        assert result == "hello"
        assert calls["n"] == 2
    finally:
        monkeypatch.undo()


def test_execute_generate_text_non_transient_not_retried():
    """A non-transient (e.g. 400 validation) failure must not be retried."""
    from backend import ai_helper

    calls = {"n": 0}

    def failing_handler(system_prompt, user_prompt, json_mode, **kw):
        calls["n"] += 1
        err = requests.exceptions.HTTPError("bad request")
        err.response = requests.models.Response()
        err.response.status_code = 400
        raise err

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("LLM_MAX_RETRIES", "3")
    monkeypatch.setenv("LLM_BACKOFF_BASE", "0.001")
    try:
        monkeypatch.setattr(
            "backend.ai_helper.PROVIDER_REGISTRY",
            {
                "openai": {
                    "id": "openai",
                    "name": "OpenAI",
                    "handler": lambda *a, **kw: failing_handler(*a, **kw),
                    "is_local": False,
                    "priority": 4,
                }
            },
        )
        monkeypatch.setattr(ai_helper, "is_cloud_fallback_allowed", lambda: True)
        with pytest.raises(requests.exceptions.HTTPError):
            ai_helper._execute_generate_text(
                "system", "user", provider="openai", task_type="job_match_scoring"
            )
        assert calls["n"] == 1
    finally:
        monkeypatch.undo()
