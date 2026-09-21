"""
Background Task Engine for Long-Running Scrapes, Synchronizations, and Batch Operations.
Provides non-blocking task execution, cooperative cancellation tokens,
step-wise progress tracking, and SSE real-time event broadcasting.
"""

import time
import uuid
import logging
import asyncio
import threading
from enum import Enum
from datetime import datetime, timezone
from typing import Callable, Any, Optional, Dict, List
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("task_engine")


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskProgress(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: str
    task_type: str
    task_name: str
    status: TaskStatus = TaskStatus.QUEUED
    progress_percentage: float = 0.0
    current_step: int = 0
    total_steps: int = 1
    step_label: str = "Initializing task..."
    items_discovered: int = 0
    errors_count: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    elapsed_seconds: float = 0.0
    error_message: Optional[str] = None
    result_data: Optional[Dict[str, Any]] = None


class CancellationToken:
    """Cooperative cancellation token passed into long-running tasks."""

    def __init__(self):
        self._is_cancelled = False
        self._lock = threading.RLock()

    def cancel(self):
        with self._lock:
            self._is_cancelled = True

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._is_cancelled

    def check(self):
        """Raises InterruptedError if cancelled."""
        if self.is_cancelled:
            raise InterruptedError("Task was cancelled by user.")


class BackgroundTask:
    def __init__(
        self,
        task_id: str,
        task_type: str,
        task_name: str,
        fn: Callable,
        args: tuple,
        kwargs: dict,
    ):
        self.task_id = task_id
        self.task_type = task_type
        self.task_name = task_name
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.token = CancellationToken()
        self.progress = TaskProgress(
            task_id=task_id,
            task_type=task_type,
            task_name=task_name,
            status=TaskStatus.QUEUED,
        )
        self.thread: Optional[threading.Thread] = None


class BackgroundTaskEngine:
    """
    Runs tasks on a dedicated worker pool with step callbacks and SSE event broadcasting.
    """

    def __init__(self, max_concurrent_tasks: int = 4):
        self._tasks: Dict[str, BackgroundTask] = {}
        self._lock = threading.RLock()
        self._max_workers = max_concurrent_tasks
        self._event_subscribers: List[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.sem_check = threading.Semaphore(self._max_workers)
        logger.info("BackgroundTaskEngine initialized.")

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def submit_task(
        self,
        task_type: str,
        task_name: str,
        fn: Callable,
        *args,
        total_steps: int = 1,
        **kwargs,
    ) -> str:
        """
        Submits a long-running callable to execute in the background.
        Returns a unique task_id immediately.
        """
        task_id = f"tsk_{uuid.uuid4().hex[:8]}"
        with self._lock:
            task = BackgroundTask(
                task_id=task_id,
                task_type=task_type,
                task_name=task_name,
                fn=fn,
                args=args,
                kwargs=kwargs,
            )
            task.progress.total_steps = max(1, total_steps)
            self._tasks[task_id] = task

        thread = threading.Thread(
            target=self._run_task_wrapper,
            args=(task,),
            daemon=True,
            name=f"TaskWorker-{task_id}",
        )
        task.thread = thread
        thread.start()
        logger.info(
            f"Submitted background task [{task_name}] (ID: {task_id}, Type: {task_type})."
        )
        return task_id

    def _run_task_wrapper(self, task: BackgroundTask):
        """Worker thread entry point."""
        t0 = time.time()
        task.progress.status = TaskStatus.RUNNING
        task.progress.started_at = datetime.now(timezone.utc)
        self._broadcast_event(task.progress)

        def progress_callback(
            step: int,
            label: str,
            items_found: int = 0,
            errors: int = 0,
            total_steps: Optional[int] = None,
        ):
            if total_steps:
                task.progress.total_steps = total_steps
            task.progress.current_step = step
            task.progress.step_label = label
            task.progress.items_discovered = items_found
            task.progress.errors_count = errors
            task.progress.elapsed_seconds = round(time.time() - t0, 1)
            pct = (step / max(1, task.progress.total_steps)) * 100.0
            task.progress.progress_percentage = min(100.0, max(0.0, round(pct, 1)))
            self._broadcast_event(task.progress)

        lock_acquired = self.sem_check.acquire()
        try:
            # Pass cancellation token and progress callback into target function
            task.kwargs["cancel_token"] = task.token
            task.kwargs["progress_cb"] = progress_callback

            result = task.fn(*task.args, **task.kwargs)

            task.progress.status = TaskStatus.COMPLETED
            task.progress.progress_percentage = 100.0
            task.progress.completed_at = datetime.now(timezone.utc)
            task.progress.elapsed_seconds = round(time.time() - t0, 1)
            task.progress.step_label = (
                f"Completed successfully in {task.progress.elapsed_seconds}s"
            )
            if isinstance(result, dict):
                task.progress.result_data = result
                if "jobs_found" in result:
                    task.progress.items_discovered = result["jobs_found"]
            logger.info(
                f"Task [{task.task_name}] ({task.task_id}) completed in {task.progress.elapsed_seconds}s."
            )
        except InterruptedError:
            task.progress.status = TaskStatus.CANCELLED
            task.progress.completed_at = datetime.now(timezone.utc)
            task.progress.elapsed_seconds = round(time.time() - t0, 1)
            task.progress.step_label = "Task cancelled by user."
            logger.warning(f"Task [{task.task_name}] ({task.task_id}) cancelled.")
        except Exception as e:
            task.progress.status = TaskStatus.FAILED
            task.progress.completed_at = datetime.now(timezone.utc)
            task.progress.elapsed_seconds = round(time.time() - t0, 1)
            task.progress.error_message = str(e)
            task.progress.step_label = f"Failed: {str(e)[:80]}"
            logger.error(
                f"Task [{task.task_name}] ({task.task_id}) failed: {e}", exc_info=True
            )
        finally:
            self._broadcast_event(task.progress)
            if lock_acquired:
                self.sem_check.release()

    def cancel_task(self, task_id: str) -> bool:
        """Requests cooperative cancellation of a running task."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.progress.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                task.token.cancel()
                task.progress.step_label = "Cancelling..."
                self._broadcast_event(task.progress)
                return True
            return False

    def get_task(self, task_id: str) -> Optional[TaskProgress]:
        with self._lock:
            t = self._tasks.get(task_id)
            return t.progress if t else None

    def get_all_tasks(self, limit: int = 50) -> List[TaskProgress]:
        with self._lock:
            tasks_list = [t.progress for t in self._tasks.values()]
            # Sort by started_at desc
            return sorted(
                tasks_list,
                key=lambda x: x.started_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )[:limit]

    def get_active_tasks(self) -> List[TaskProgress]:
        with self._lock:
            return [
                t.progress
                for t in self._tasks.values()
                if t.progress.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
            ]

    def clear_completed_tasks(self) -> int:
        """Removes finished, failed, or cancelled tasks from memory."""
        with self._lock:
            to_del = [
                tid
                for tid, t in self._tasks.items()
                if t.progress.status
                in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
            ]
            for tid in to_del:
                del self._tasks[tid]
            return len(to_del)

    # ---------------------------------------------------------
    # Server-Sent Events (SSE) Pub/Sub
    # ---------------------------------------------------------
    def register_subscriber(self, q: asyncio.Queue):
        with self._lock:
            self._event_subscribers.append(q)

    def unregister_subscriber(self, q: asyncio.Queue):
        with self._lock:
            if q in self._event_subscribers:
                self._event_subscribers.remove(q)

    def _broadcast_event(self, progress: TaskProgress):
        payload = progress.model_dump_json()
        with self._lock:
            subs = list(self._event_subscribers)

        for q in subs:
            try:
                if self._loop and self._loop.is_running():
                    self._loop.call_soon_threadsafe(q.put_nowait, payload)
                else:
                    q.put_nowait(payload)
            except Exception:
                pass


# Global singleton instance
task_engine = BackgroundTaskEngine()
