import time
import queue
import logging
import threading
import uuid
from enum import IntEnum
from typing import Callable, Any, Optional, Dict, List
from concurrent.futures import Future

logger = logging.getLogger("llm_queue")

class LLMPriority(IntEnum):
    """
    Priority tiers for LLM task scheduling:
    - INTERACTIVE (1): High priority (Chat, live modal queries, immediate user interactions)
    - ON_DEMAND (2): Normal priority (Single-job assisted apply, single resume parse/tailoring)
    - BACKGROUND (3): Low priority (Bulk tailoring, background feed scans, batch enrichment)
    """
    INTERACTIVE = 1
    ON_DEMAND = 2
    BACKGROUND = 3

class LLMTask:
    def __init__(
        self,
        fn: Callable,
        args: tuple,
        kwargs: dict,
        priority: LLMPriority = LLMPriority.ON_DEMAND,
        task_name: str = ""
    ):
        self.task_id = str(uuid.uuid4())[:8]
        self.priority = int(priority)
        self.timestamp = time.time()
        self.task_name = task_name or fn.__name__
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.future = Future()

    def __lt__(self, other: "LLMTask") -> bool:
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.timestamp < other.timestamp

class LLMQueueManager:
    """
    In-memory dynamic-concurrency Priority Queue Manager for LLM inference.
    - Local Models (LM Studio, Unsloth, Ollama): Runs with Concurrency = 1 + pacing delay (0.2s)
      to protect local GPU VRAM and prevent KV-cache thrashing.
    - Cloud Models (OpenAI, Gemini, Claude, DeepSeek): Dynamically scales up to MAX_CONCURRENCY (default: 8)
      with zero pacing delay for high-throughput parallel execution.
    """
    def __init__(
        self, 
        max_workers: int = 1, 
        pacing_seconds: float = 0.2, 
        mode: str = "local"
    ):
        self._queue = queue.PriorityQueue()
        self._lock = threading.Lock()
        self._max_workers = max(1, int(max_workers))
        self._pacing_seconds = pacing_seconds
        self._mode = mode
        self._active_tasks: Dict[str, Dict[str, Any]] = {}
        self._total_processed: int = 0
        self._total_duration_ms: float = 0.0
        self._running = True
        self._workers: List[threading.Thread] = []

        self._ensure_workers()
        logger.info(f"LLM Priority Queue Manager initialized (Workers: {self._max_workers}, Mode: {self._mode}).")

    def _ensure_workers(self):
        """Spawns worker threads up to _max_workers."""
        with self._lock:
            while len(self._workers) < self._max_workers:
                worker_id = len(self._workers)
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(worker_id,),
                    daemon=True,
                    name=f"LLM-Queue-Worker-{worker_id}"
                )
                self._workers.append(t)
                t.start()

    def set_concurrency(
        self, 
        max_workers: int, 
        pacing_seconds: Optional[float] = None, 
        mode: Optional[str] = None
    ):
        """
        Dynamically adjusts worker concurrency pool size and pacing on the fly.
        """
        with self._lock:
            new_workers = max(1, int(max_workers))
            old_workers = self._max_workers
            self._max_workers = new_workers
            if pacing_seconds is not None:
                self._pacing_seconds = max(0.0, float(pacing_seconds))
            if mode is not None:
                self._mode = mode

        if new_workers > old_workers:
            logger.info(f"Scaling LLM queue concurrency UP: {old_workers} -> {new_workers} workers (Mode: {self._mode}).")
            self._ensure_workers()
        elif new_workers < old_workers:
            logger.info(f"Scaling LLM queue concurrency DOWN: {old_workers} -> {new_workers} workers (Mode: {self._mode}).")
            # Idle excess workers will naturally exit in _worker_loop

    def sync_provider_concurrency(self, is_local: bool):
        """
        Synchronizes queue worker pool based on provider locality (local vs cloud).
        """
        from backend.config import (
            LLM_LOCAL_MAX_CONCURRENCY,
            LLM_CLOUD_MAX_CONCURRENCY,
            LLM_LOCAL_PACING_SECONDS,
            LLM_CLOUD_PACING_SECONDS
        )
        if is_local:
            self.set_concurrency(
                max_workers=LLM_LOCAL_MAX_CONCURRENCY,
                pacing_seconds=LLM_LOCAL_PACING_SECONDS,
                mode="local"
            )
        else:
            self.set_concurrency(
                max_workers=LLM_CLOUD_MAX_CONCURRENCY,
                pacing_seconds=LLM_CLOUD_PACING_SECONDS,
                mode="cloud"
            )

    def submit(
        self,
        fn: Callable,
        *args,
        priority: LLMPriority = LLMPriority.ON_DEMAND,
        timeout: float = 180.0,
        task_name: str = "",
        **kwargs
    ) -> Any:
        """
        Submits an inference task to the priority queue and blocks until complete or timed out.
        """
        task = LLMTask(fn, args, kwargs, priority=priority, task_name=task_name)
        self._queue.put(task)
        logger.debug(f"Queued LLM task [{task.task_name}] (ID: {task.task_id}, Priority: {priority.name}, Queue Depth: {self._queue.qsize()})")

        try:
            return task.future.result(timeout=timeout)
        except Exception as e:
            logger.error(f"Task [{task.task_name}] ({task.task_id}) failed or timed out: {e}")
            raise

    def _worker_loop(self, worker_id: int):
        """Worker loop that pulls prioritized tasks."""
        while self._running:
            with self._lock:
                if worker_id >= self._max_workers:
                    # Clean retirement of excess worker thread when scaled down
                    break

            try:
                task: LLMTask = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if task.future.cancelled():
                self._queue.task_done()
                continue

            start_t = time.time()
            with self._lock:
                self._active_tasks[task.task_id] = {
                    "task_name": task.task_name,
                    "started_at": start_t,
                    "priority": task.priority,
                    "worker_id": worker_id
                }

            try:
                logger.debug(f"Worker {worker_id} executing LLM task [{task.task_name}] (ID: {task.task_id}, Priority: {task.priority})")
                res = task.fn(*task.args, **task.kwargs)
                task.future.set_result(res)
            except Exception as ex:
                logger.warning(f"Error executing LLM task [{task.task_name}] on worker {worker_id}: {ex}")
                task.future.set_exception(ex)
            finally:
                duration_ms = (time.time() - start_t) * 1000.0
                with self._lock:
                    self._total_processed += 1
                    self._total_duration_ms += duration_ms
                    self._active_tasks.pop(task.task_id, None)

                self._queue.task_done()

                # Pacing delay between sequential requests on local models
                if self._pacing_seconds > 0:
                    time.sleep(self._pacing_seconds)

    def get_status(self) -> Dict[str, Any]:
        """Returns current queue metrics, active workers, concurrency limit, and backpressure indicators."""
        with self._lock:
            q_depth = self._queue.qsize()
            avg_lat = (self._total_duration_ms / self._total_processed) if self._total_processed > 0 else 0.0
            
            # Active tasks metadata
            active_list = list(self._active_tasks.values())
            active_count = len(active_list)
            first_task_name = active_list[0]["task_name"] if active_count > 0 else None
            max_elapsed = max([(time.time() - t["started_at"]) for t in active_list], default=0.0)

            # High backpressure threshold is 5 tasks per available worker
            is_high_pressure = q_depth >= (self._max_workers * 4)

            # Est wait time
            est_wait = round((q_depth / max(1, self._max_workers)) * (avg_lat / 1000.0 or 2.5), 1)

            return {
                "queue_depth": q_depth,
                "max_concurrency": self._max_workers,
                "active_workers": active_count,
                "mode": self._mode,
                "active_task": first_task_name,
                "active_tasks_count": active_count,
                "active_task_elapsed_seconds": round(max_elapsed, 1),
                "total_processed": self._total_processed,
                "avg_latency_ms": round(avg_lat, 1),
                "is_backpressure_high": is_high_pressure,
                "estimated_wait_seconds": est_wait
            }

    def clear_background_tasks(self) -> int:
        """Purges any pending Priority 3 (BACKGROUND) tasks from the queue."""
        cleared = 0
        temp_items = []
        while not self._queue.empty():
            try:
                item: LLMTask = self._queue.get_nowait()
                if item.priority == LLMPriority.BACKGROUND:
                    item.future.cancel()
                    cleared += 1
                else:
                    temp_items.append(item)
            except queue.Empty:
                break

        for item in temp_items:
            self._queue.put(item)
        return cleared

# Global singleton queue manager
llm_queue = LLMQueueManager(max_workers=1, pacing_seconds=0.2, mode="local")
