"""Persisted single-flight queue for LinkedIn saved-jobs scans (issue #114).

The manual ``POST /api/linkedin/sync`` and the Sync-All stage both drive the single-owner
browser profile, so only one scan may run at a time. ``is_profile_in_use()`` cannot serialize
them (it only reflects a browser that already holds the profile), so the gate below does.

A request that arrives while a scan is running is not rejected: it is written to
``linkedin_scan_requests`` and executed by a background task when the gate frees. The row is the
durable part — ``recover_stale_requests`` re-queues it after a restart, which is what the
incident needed (the reload worker killed the in-flight request).
"""

import datetime
import logging
import threading
import time
from collections import deque
from typing import List, Optional

from backend.database import LinkedInScanRequest, utc_now

logger = logging.getLogger("linkedin_scan_queue")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# Finished rows older than this are pruned on startup so the queue table stays bounded.
RETENTION_DAYS = 7


class LinkedInScanGate:
    """Non-reentrant mutex that hands ownership to waiters in arrival order.

    ``threading.Lock`` has no ordering, so a queued request could be overtaken by a later one and
    the reported queue position would be a guess. Waiters wait on a ticket instead.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._held = False
        self._waiters: deque = deque()

    def try_acquire(self) -> bool:
        """Take the gate only if nothing holds it and nobody is queued ahead."""
        with self._cond:
            if not self._held and not self._waiters:
                self._held = True
                return True
            return False

    def acquire(self, timeout: Optional[float] = None, should_cancel=None) -> bool:
        """Wait for this caller's turn. Returns False on timeout or cancellation."""
        ticket = object()
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            self._waiters.append(ticket)
            try:
                while True:
                    if should_cancel is not None and should_cancel():
                        return False
                    if not self._held and self._waiters[0] is ticket:
                        self._waiters.popleft()
                        self._held = True
                        return True
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        self._cond.wait(min(0.25, remaining))
                    else:
                        self._cond.wait(0.25)
            finally:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                self._cond.notify_all()

    def release(self) -> None:
        with self._cond:
            self._held = False
            self._cond.notify_all()

    def queue_position(self) -> int:
        """1-based position a caller arriving right now would take."""
        with self._cond:
            return len(self._waiters) + (1 if self._held else 0) + 1


gate = LinkedInScanGate()


def enqueue(db, origin: str = "manual") -> LinkedInScanRequest:
    """Persist a scan request and return the row (position is reported by ``queue_position``)."""
    row = LinkedInScanRequest(origin=origin, status=STATUS_QUEUED, requested_at=utc_now())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def queue_position(db, request_id: int) -> int:
    """Number of requests that will run before ``request_id``, plus one."""
    ahead = (
        db.query(LinkedInScanRequest)
        .filter(
            LinkedInScanRequest.status == STATUS_QUEUED,
            LinkedInScanRequest.id <= request_id,
        )
        .count()
    )
    return max(1, ahead)


def attach_task_id(db, request_id: int, task_id: str) -> None:
    row = db.get(LinkedInScanRequest, request_id)
    if row is None:
        return
    row.task_id = task_id
    db.commit()


def mark_running(db, request_id: int) -> None:
    row = db.get(LinkedInScanRequest, request_id)
    if row is None:
        return
    row.status = STATUS_RUNNING
    row.started_at = utc_now()
    db.commit()


def mark_finished(
    db,
    request_id: int,
    status: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    row = db.get(LinkedInScanRequest, request_id)
    if row is None:
        return
    row.status = status
    row.completed_at = utc_now()
    row.summary = (summary or "")[:2000] or None
    row.error_message = (error or "")[:2000] or None
    db.commit()


def cancel_pending(db) -> List[LinkedInScanRequest]:
    """Mark every queued request cancelled and return them (callers cancel their tasks)."""
    rows = (
        db.query(LinkedInScanRequest)
        .filter(LinkedInScanRequest.status == STATUS_QUEUED)
        .all()
    )
    for row in rows:
        row.status = STATUS_CANCELLED
        row.completed_at = utc_now()
        row.summary = "Cancelled by the user before it started."
    db.commit()
    return rows


def recover_stale_requests(db) -> List[int]:
    """Re-queue requests left behind by a previous process and prune old finished rows.

    A row still ``queued`` was never started, so it is safe to run now. A row still ``running``
    belongs to a process that died mid-scan: its outcome is unknown, so it is marked cancelled
    rather than silently re-running a browser scan.
    """
    requeue: List[int] = []
    try:
        interrupted = (
            db.query(LinkedInScanRequest)
            .filter(LinkedInScanRequest.status == STATUS_RUNNING)
            .all()
        )
        for row in interrupted:
            row.status = STATUS_CANCELLED
            row.completed_at = utc_now()
            row.summary = "Interrupted: the server stopped while this scan was running."
        if interrupted:
            db.commit()

        queued = (
            db.query(LinkedInScanRequest)
            .filter(LinkedInScanRequest.status == STATUS_QUEUED)
            .order_by(LinkedInScanRequest.id)
            .all()
        )
        requeue = [row.id for row in queued]

        cutoff = utc_now() - datetime.timedelta(days=RETENTION_DAYS)
        db.query(LinkedInScanRequest).filter(
            LinkedInScanRequest.completed_at.isnot(None),
            LinkedInScanRequest.completed_at < cutoff,
        ).delete(synchronize_session=False)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - recovery must never block startup
        logger.warning(f"LinkedIn scan queue recovery failed: {exc}")
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []
    return requeue
