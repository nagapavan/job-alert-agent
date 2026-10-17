"""Graceful shutdown on SIGTERM / SIGINT.

The app runs background daemons (scheduler, task engine, LLM queue) and owns a
single-user Playwright browser profile. Without any handling, an abrupt ``kill``/
``Ctrl-C`` abandoned in-flight work and frequently left Chromium's ``SingletonLock``
behind, so the next launch raised ``BrowserProfileBusy``.

**Integration model — let the ASGI server own signals; drain from the lifecycle.**

Uvicorn installs its own SIGTERM/SIGINT handler when the server starts
(``Server.capture_signals`` -> ``handle_exit``), which sets an internal ``should_exit``
flag and then runs uvicorn's graceful shutdown (including the ASGI shutdown event). We
therefore do **not** register signal handlers ourselves: the ASGI app is imported during
``Server._serve``, so anything we registered would clobber uvicorn's, and both
alternatives were observed to break ``--reload``:

- raising out of our handler aborted uvicorn's event loop and killed the reloader parent
  (clean drain, no reload, dead port);
- swallowing the signal left the child alive, so the reloader blocked forever in
  ``process.join()``.

The drain lives in the ASGI **shutdown** lifecycle (see ``main.py``), which uvicorn calls
on every graceful-exit path: ``--reload`` child, ``--workers``, and standalone.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("shutdown")

# Bounded drain: a wedged task must not block shutdown forever.
DRAIN_DEADLINE_SECONDS = 10.0

_state_lock = threading.Lock()
_shutting_down = False


def is_shutting_down() -> bool:
    """True once a shutdown signal has been received (or teardown started)."""
    return _shutting_down


def _mark_shutting_down() -> bool:
    """Set the flag exactly once. Returns True for the first caller only."""
    global _shutting_down
    with _state_lock:
        if _shutting_down:
            return False
        _shutting_down = True
        return True


def _drain(deadline: float) -> None:
    """Ordered cooperative teardown of running subsystems."""
    import time

    # 1. Scheduler: stop triggering new runs.
    try:
        from backend.scheduler import scheduler

        scheduler.stop()
        logger.info("Shutdown: scheduler stopped.")
    except Exception as exc:  # noqa: BLE001 - teardown must never raise
        logger.debug("Shutdown: scheduler stop skipped: %s", exc)

    # 2. Task engine: cooperatively cancel queued/running tasks.
    try:
        from backend.task_engine import task_engine

        cancelled = task_engine.cancel_all()
        if cancelled:
            logger.info("Shutdown: cancelling %d in-flight task(s).", cancelled)
        # Bounded wait for tasks to observe cancellation.
        while time.time() < deadline:
            if not task_engine.get_active_tasks():
                break
            time.sleep(0.1)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Shutdown: task cancellation skipped: %s", exc)

    # 3. LLM queue: stop dispatching new provider calls.
    try:
        from backend.llm_queue import llm_queue

        llm_queue.stop()
        logger.info("Shutdown: LLM queue stopped.")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Shutdown: LLM queue stop skipped: %s", exc)

    # 4. Browser profile: release the lock LAST, only when no live process owns it.
    try:
        from backend.playwright_app import clean_stale_profile_locks

        if clean_stale_profile_locks():
            logger.info("Shutdown: browser profile locks released.")
        else:
            logger.info("Shutdown: browser profile still in use; locks left intact.")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Shutdown: profile lock cleanup skipped: %s", exc)


def run_shutdown() -> None:
    """Run the drain exactly once (idempotent).

    Called from the ASGI shutdown lifecycle. Safe to call repeatedly; only the first
    invocation drains.
    """
    if not _mark_shutting_down():
        logger.debug("Shutdown already completed; skipping repeat drain.")
        return
    import time

    logger.info("Shutdown signal received; draining background work...")
    _drain(time.time() + DRAIN_DEADLINE_SECONDS)
