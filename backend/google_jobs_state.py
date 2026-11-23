"""Run policy for the Google Jobs channel: block cooldown and query rotation.

Google answers automated traffic with an anti-bot interstitial (``/sorry/index?continue=...``)
once it decides an egress address is suspicious. Retrying immediately cannot succeed, so a block
starts a cooldown.

Query rotation spreads the query set across runs: each run scans a bounded subset starting at a
persisted cursor, so coverage is complete over several runs rather than in one burst.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from backend.config import BASE_DIR

logger = logging.getLogger("google_jobs_state")

STATE_PATH = Path(
    os.environ.get("GOOGLE_JOBS_STATE_FILE", str(BASE_DIR / "data" / "google_jobs_state.json"))
)
COOLDOWN_HOURS = float(os.environ.get("GOOGLE_JOBS_COOLDOWN_HOURS", "168"))
QUERIES_PER_RUN = int(os.environ.get("GOOGLE_JOBS_QUERIES_PER_RUN", "3"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> dict:
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text("utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001 - policy state must never break a run
        logger.debug("Could not read Google Jobs state: %s", exc)
    return {}


# The last write's outcome, so a run can say "the cooldown is not durable" instead of assuming it
# (finding 49). Scans are single-run threads, so one module-level slot is enough.
_last_save_error: Optional[str] = None


def last_save_error() -> Optional[str]:
    """Why the most recent state write failed, or None when it succeeded."""
    return _last_save_error


def _save_state(state: dict) -> bool:
    global _last_save_error
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)
        _last_save_error = None
        return True
    except Exception as exc:  # noqa: BLE001 - best effort, never raise into a run
        _last_save_error = str(exc)
        logger.warning(f"Could not persist Google Jobs state to {STATE_PATH}: {exc}")
        return False


def cooling_down_until() -> Optional[datetime]:
    """The cooldown deadline while it is still in the future, else ``None``."""
    raw = load_state().get("blocked_until")
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until if until > _now() else None


def cooldown_reason() -> str:
    """Human-readable cooldown note, or ``""`` when the channel may run."""
    until = cooling_down_until()
    if not until:
        return ""
    return (
        f"Google blocked the last run; cooling down until "
        f"{until.strftime('%Y-%m-%d %H:%M UTC')}."
    )


def record_block(reason: str = "") -> datetime:
    """Start a cooldown after Google served the anti-bot interstitial."""
    until = _now() + timedelta(hours=COOLDOWN_HOURS)
    state = load_state()
    state["blocked_at"] = _now().isoformat()
    state["blocked_until"] = until.isoformat()
    state["blocked_reason"] = (reason or "")[:200]
    _save_state(state)
    return until


def record_run(queries_used: int, total: int, reached: Optional[int] = None) -> None:
    """Advance the rotation cursor after a run that was not blocked.

    The cooldown is cleared only when at least one query actually reached Google (finding 50):
    a run where every query raised says nothing about whether the block expired, so it must not
    silently re-enable the channel. ``reached=None`` keeps the clearing behaviour for callers
    that have no per-query outcome.
    """
    state = load_state()
    if reached is None or reached > 0:
        state.pop("blocked_until", None)
        state.pop("blocked_reason", None)
        state.pop("blocked_at", None)
    cursor = int(state.get("query_cursor", 0) or 0)
    state["query_cursor"] = (cursor + max(0, queries_used)) % max(1, total)
    state["last_run_at"] = _now().isoformat()
    _save_state(state)


def select_queries(
    queries: Sequence[str], limit: Optional[int] = None
) -> Tuple[List[str], int]:
    """Rotating subset of ``queries`` for this run, plus the full query count."""
    items = list(queries)
    total = len(items)
    if not total:
        return [], 0
    count = min(limit if limit is not None else QUERIES_PER_RUN, total)
    cursor = int(load_state().get("query_cursor", 0) or 0) % total
    return [items[(cursor + offset) % total] for offset in range(count)], total
