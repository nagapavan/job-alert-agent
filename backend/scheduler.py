"""
Periodic Automation Scheduler for Hands-Free Background Discovery.
Manages automated scans for Google Jobs followed queries, LinkedIn sync, and hiring posts
with configurable intervals, rate-limit jitter, and auto-persistence.
"""

import os
import json
import random
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, ConfigDict

from backend.config import BASE_DIR
from backend.task_engine import task_engine

logger = logging.getLogger("scheduler")

SCHEDULER_CONFIG_FILE = BASE_DIR / "data" / "scheduler_config.json"


class ScheduledJobConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    task_type: str
    description: str
    interval_hours: float = 6.0
    is_enabled: bool = True
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    last_status: Optional[str] = "IDLE"
    last_items_count: int = 0
    last_task_id: Optional[str] = None


DEFAULT_SCHEDULES: List[Dict[str, Any]] = [
    {
        "id": "sched_google_jobs",
        "name": "Google for Jobs Followed Queries Scan",
        "task_type": "google_jobs_scan",
        "description": "Scrapes and evaluates all followed alert queries in Google for Jobs with ATS alignment scoring.",
        "interval_hours": 6.0,
        "is_enabled": True
    },
    {
        "id": "sched_linkedin_sync",
        "name": "LinkedIn Saved Jobs & Due Diligence Sync",
        "task_type": "linkedin_sync",
        "description": "Synchronizes saved jobs, checks external ATS portal links, and evaluates ghost job risks.",
        "interval_hours": 12.0,
        "is_enabled": True
    }
]


class PeriodicScheduler:
    """
    In-process background scheduler for hands-free discovery.
    Runs an asynchronous daemon thread checking trigger timestamps.
    """
    def __init__(self, check_interval_seconds: int = 30):
        self._check_interval = check_interval_seconds
        self._jobs: Dict[str, ScheduledJobConfig] = {}
        self._lock = threading.Lock()
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Operation-log rotation cadence (seconds). Seeded to now so the first prune waits one
        # full interval (avoids touching the DB immediately on import/in tests).
        self._maintenance_interval = int(os.environ.get("OPERATION_LOG_PRUNE_INTERVAL_SECONDS", "3600"))
        self._last_maintenance_at = datetime.now(timezone.utc)
        # Reason the most recent run_job_now() skipped/declined, so callers (the API) can
        # distinguish "job not found" from a policy skip (e.g. missing consent).
        self._last_skip_reason: Optional[str] = None
        self._load_config()

    def _load_config(self):
        """Loads schedule configuration from disk or seeds defaults."""
        SCHEDULER_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if SCHEDULER_CONFIG_FILE.exists():
            try:
                with open(SCHEDULER_CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        job = ScheduledJobConfig.model_validate(item)
                        # Re-calculate next_run_at if in the past
                        if job.is_enabled and (not job.next_run_at or job.next_run_at < datetime.now(timezone.utc)):
                            jitter = random.uniform(0.1, 0.5)
                            job.next_run_at = datetime.now(timezone.utc) + timedelta(hours=job.interval_hours * jitter)
                        self._jobs[job.id] = job
            except Exception as e:
                logger.warning(f"Failed loading scheduler config: {e}. Seeding defaults.")

        # Drop retired schedules no longer defined in DEFAULT_SCHEDULES (e.g. the removed
        # hiring_posts_scan) so stale persisted config can't linger and log "unknown task type".
        valid_ids = {d["id"] for d in DEFAULT_SCHEDULES}
        for retired_id in [jid for jid in self._jobs if jid not in valid_ids]:
            logger.info(f"Removing retired scheduled job '{retired_id}' from persisted config.")
            del self._jobs[retired_id]

        # Seed defaults for any missing job definitions
        now = datetime.now(timezone.utc)
        for d in DEFAULT_SCHEDULES:
            if d["id"] not in self._jobs:
                jitter_hrs = random.uniform(0.1, 0.5)
                job = ScheduledJobConfig(
                    id=d["id"],
                    name=d["name"],
                    task_type=d["task_type"],
                    description=d["description"],
                    interval_hours=d["interval_hours"],
                    is_enabled=d["is_enabled"],
                    next_run_at=now + timedelta(hours=d["interval_hours"] * jitter_hrs)
                )
                self._jobs[job.id] = job

        self._save_config()

    def _save_config(self):
        """Persists schedule configuration to disk."""
        try:
            with self._lock:
                serialized = [j.model_dump(mode="json") for j in self._jobs.values()]
                with open(SCHEDULER_CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(serialized, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save scheduler config: {e}")

    def start(self):
        """Starts the scheduler daemon thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._scheduler_loop,
                daemon=True,
                name="PeriodicScheduler"
            )
            self._thread.start()
            logger.info("PeriodicScheduler daemon started.")

    def stop(self):
        """Stops the scheduler daemon."""
        with self._lock:
            self._running = False
            self._stop_event.set()

    def _scheduler_loop(self):
        """Background loop evaluating trigger times."""
        while self._running and not self._stop_event.is_set():
            try:
                now = datetime.now(timezone.utc)
                to_trigger = []

                with self._lock:
                    for job in self._jobs.values():
                        if not job.is_enabled:
                            continue
                        if job.next_run_at and now >= job.next_run_at:
                            to_trigger.append(job.id)

                for job_id in to_trigger:
                    self.run_job_now(job_id)

                self._maybe_run_maintenance()

            except Exception as e:
                logger.error(f"Error in scheduler evaluation loop: {e}", exc_info=True)

            self._stop_event.wait(timeout=self._check_interval)

    def _maybe_run_maintenance(self):
        """Periodically rotates the operation_logs table so the DB never grows unbounded."""
        now = datetime.now(timezone.utc)
        if (now - self._last_maintenance_at).total_seconds() < self._maintenance_interval:
            return
        self._last_maintenance_at = now
        try:
            from backend.database import SessionLocal, prune_operation_logs
            db = SessionLocal()
            try:
                pruned = prune_operation_logs(db)
                if pruned:
                    logger.info(f"Maintenance: pruned {pruned} operation-log row(s).")
            finally:
                db.close()
        except Exception as e:
            logger.debug(f"Operation-log maintenance skipped: {e}")

    def last_skip_reason(self) -> Optional[str]:
        """Reason the most recent run_job_now() declined to run (None when it ran or wasn't found)."""
        return self._last_skip_reason

    def run_job_now(self, job_id: str, force: bool = False) -> Optional[str]:
        """
        Triggers immediate execution of a scheduled job in the background.
        Automatic (scheduler-loop) runs respect opt-in feature flags; explicit user "Run now"
        requests pass force=True to bypass the *flag* gate. Consent-gated features (e.g.
        LinkedIn automation) must satisfy consent even on a forced run.
        """
        self._last_skip_reason = None
        job = self._jobs.get(job_id)
        if not job:
            return None

        # Respect opt-in feature flags: skip discovery channels the user has disabled.
        # Consent-required features additionally demand explicit acknowledgment, even when forced.
        _feature_map = {
            "google_jobs_scan": "google_jobs",
            "linkedin_sync": "linkedin_sync",
        }
        feature = _feature_map.get(job.task_type)
        if feature:
            from backend.database import CONSENT_REQUIRED_FEATURES
            _consent_required = feature in CONSENT_REQUIRED_FEATURES
            if (not force) or _consent_required:
                try:
                    from backend.database import SessionLocal, has_consent, is_feature_enabled
                    _db = SessionLocal()
                    try:
                        skip_reason = None
                        if not force and not is_feature_enabled(_db, feature):
                            skip_reason = f"feature '{feature}' is disabled"
                        elif _consent_required and not has_consent(_db, feature):
                            skip_reason = f"consent for '{feature}' was not given"
                        if skip_reason:
                            logger.info(f"Scheduled job [{job.name}] skipped: {skip_reason}.")
                            self._last_skip_reason = skip_reason
                            _now = datetime.now(timezone.utc)
                            job.last_status = "SKIPPED"
                            job.next_run_at = _now + timedelta(hours=job.interval_hours)
                            self._save_config()
                            return None
                    finally:
                        _db.close()
                except Exception as fe:
                    logger.debug(f"Feature-flag gate unavailable: {fe}")

        # Import runner functions lazily to avoid circular dependencies
        from backend.main import (
            _run_google_jobs_task_runner,
            _run_linkedin_sync_task_runner
        )

        runner_map = {
            "google_jobs_scan": ("Google Jobs Scheduled Scan", _run_google_jobs_task_runner),
            "linkedin_sync": ("LinkedIn Scheduled Sync", _run_linkedin_sync_task_runner)
        }

        if job.task_type not in runner_map:
            logger.warning(f"Unknown task type for schedule {job_id}: {job.task_type}")
            return None

        name_prefix, runner_fn = runner_map[job.task_type]
        task_id = task_engine.submit_task(
            task_type=job.task_type,
            task_name=f"{name_prefix} (Auto)",
            fn=runner_fn,
            total_steps=6 if job.task_type == "google_jobs_scan" else 3
        )

        now = datetime.now(timezone.utc)
        # Add slight jitter to next run to prevent predictable burst traffic
        jitter_minutes = random.uniform(-10, 10)
        next_run = now + timedelta(hours=job.interval_hours, minutes=jitter_minutes)

        job.last_run_at = now
        job.next_run_at = next_run
        job.last_status = "RUNNING"
        job.last_task_id = task_id
        self._save_config()

        logger.info(f"Triggered scheduled job [{job.name}] -> Task {task_id}. Next run at {next_run.isoformat()}.")
        return task_id

    def update_job(
        self,
        job_id: str,
        is_enabled: Optional[bool] = None,
        interval_hours: Optional[float] = None
    ) -> Optional[ScheduledJobConfig]:
        """Updates schedule status or interval."""
        job = self._jobs.get(job_id)
        if not job:
            return None

        if is_enabled is not None:
            job.is_enabled = is_enabled
            if is_enabled and (not job.next_run_at or job.next_run_at < datetime.now(timezone.utc)):
                job.next_run_at = datetime.now(timezone.utc) + timedelta(hours=job.interval_hours * 0.5)

        if interval_hours is not None and interval_hours > 0:
            job.interval_hours = interval_hours
            if job.is_enabled:
                job.next_run_at = datetime.now(timezone.utc) + timedelta(hours=interval_hours)

        self._save_config()
        return job

    def get_all_jobs(self) -> List[ScheduledJobConfig]:
        """Returns all configured scheduled jobs."""
        with self._lock:
            return list(self._jobs.values())


# Global singleton instance
scheduler = PeriodicScheduler()
