"""LLM resilience: exponential-backoff retry around provider calls.

Provides transient-failure classification and a retry wrapper used by
``ai_helper._execute_generate_text`` around each provider handler invocation.

Retry policy:
- Only *transient* errors are retried: HTTP 429 (rate-limit / token exhaustion),
  5xx server errors, connection errors, and timeouts. Everything else (e.g. 400
  validation) propagates immediately so we never mask real bugs with needless waits.
- Backoff is exponential with an optional random jitter, bounded by ``backoff_max``.
- The max-retry / backoff values resolve from a per-task override (``TASK_PROFILES``)
  falling back to global config defaults (``LLM_MAX_RETRIES`` etc.), mirroring the
  maxify reference pattern of per-step retry config in code + config.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Optional, Tuple, TypeVar

import requests

from backend.config import (
    LLM_BACKOFF_BASE,
    LLM_BACKOFF_MAX,
    LLM_MAX_RETRIES,
    LLM_RETRY_JITTER,
)

logger = logging.getLogger("llm_resilience")

T = TypeVar("T")

# HTTP status codes that warrant a retry: rate limiting + transient server errors.
LLM_RESILIENCE_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})


def is_retryable(exc: BaseException) -> bool:
    """Return True when ``exc`` is a transient failure worth retrying."""
    # Connection / transport failures (raises over HTTP + provider SDK boundaries).
    if isinstance(exc, (requests.exceptions.ConnectionError, ConnectionError)):
        return True
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
        return True
    # HTTP errors are retried only for retryable status codes.
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if status in LLM_RESILIENCE_RETRYABLE_HTTP:
            return True
    return False


def resolve_retry_config(
    task_type: Optional[str] = None,
    *,
    max_retries: Optional[int] = None,
    backoff_base: Optional[float] = None,
    backoff_max: Optional[float] = None,
    jitter: Optional[bool] = None,
) -> Tuple[int, float, float, bool]:
    """Resolve ``(max_retries, backoff_base, backoff_max, jitter)`` for a task.

    Precedence (highest wins): explicit kwargs > per-task-profile override > global
    config defaults. The task profile may carry ``max_retries`` (and optionally
    ``backoff_base``/``backoff_max``) like the other ``TASK_PROFILES`` keys.
    """
    from backend.ai_helper import TASK_PROFILES  # local import to avoid a cycle

    profile = (TASK_PROFILES or {}).get(task_type) or {}

    resolved_max = (
        max_retries
        if max_retries is not None
        else profile.get("max_retries", LLM_MAX_RETRIES)
    )
    resolved_base = (
        backoff_base
        if backoff_base is not None
        else profile.get("backoff_base", LLM_BACKOFF_BASE)
    )
    resolved_cap = (
        backoff_max
        if backoff_max is not None
        else profile.get("backoff_max", LLM_BACKOFF_MAX)
    )
    resolved_jitter = jitter if jitter is not None else LLM_RETRY_JITTER
    return int(resolved_max), float(resolved_base), float(resolved_cap), bool(resolved_jitter)


def with_retry(
    step_name: str,
    fn: Callable[[], T],
    *,
    task_type: Optional[str] = None,
    max_retries: Optional[int] = None,
    backoff_base: Optional[float] = None,
    backoff_max: Optional[float] = None,
    jitter: Optional[bool] = None,
) -> T:
    """Run ``fn()`` with exponential backoff on transient failures.

    ``max_retries`` is the number of retries AFTER the first attempt. When omitted, it
    (and the backoff values) resolve from ``task_type`` config (per-profile then global).
    Retries happen only for ``is_retryable`` exceptions; others propagate immediately.
    """
    m_retries, b_base, b_cap, use_jitter = resolve_retry_config(
        task_type,
        max_retries=max_retries,
        backoff_base=backoff_base,
        backoff_max=backoff_max,
        jitter=jitter,
    )
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - centralized resilience
            last = exc
            if not is_retryable(exc):
                logger.warning("[%s] non-retryable error: %s", step_name, exc)
                raise
            if attempt >= m_retries:
                logger.error(
                    "[%s] exhausted %d retry(ies); last error: %s",
                    step_name,
                    m_retries,
                    exc,
                )
                raise
            attempt += 1
            delay = min(b_base * (2 ** (attempt - 1)), b_cap)
            if use_jitter:
                # 0.5x..1.5x jitter to avoid thundering-herd retry synchrony.
                delay *= random.uniform(0.5, 1.5)
            logger.info(
                "[%s] retryable error (%s); retry %d/%d in %.2fs",
                step_name,
                exc,
                attempt,
                m_retries,
                delay,
            )
            time.sleep(delay)
