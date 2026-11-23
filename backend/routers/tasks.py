"""Background-task introspection, the SSE progress stream, and the automation scheduler.

The task *dispatch* routes stay in ``main.py``: each one binds a runner function defined there
(``_run_*_task_runner``), so moving them would only move the import cycle. This module owns the
read/cancel/stream side, which depends on nothing but the shared engine singletons.
"""

import asyncio
import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.llm_queue import llm_queue
from backend.scheduler import ScheduledJobConfig, scheduler
from backend.task_engine import TaskProgress, task_engine

router = APIRouter(tags=["tasks"])


@router.get("/api/tasks", response_model=List[TaskProgress])
def list_all_background_tasks(limit: int = 50):
    """Returns list of recent and active background tasks."""
    return task_engine.get_all_tasks(limit=limit)


@router.get("/api/tasks/active", response_model=List[TaskProgress])
def list_active_background_tasks():
    """Returns all currently running or queued background tasks."""
    return task_engine.get_active_tasks()


@router.get("/api/tasks/events")
async def task_events_sse(request: Request):
    """
    Server-Sent Events (SSE) stream for real-time background task progress updates.
    """
    # Bind the running loop so worker threads broadcast thread-safely.
    loop = asyncio.get_running_loop()
    task_engine.set_event_loop(loop)
    q = asyncio.Queue()
    task_engine.register_subscriber(q)

    async def event_generator():
        try:
            # Yield initial state of active tasks
            for task_prog in task_engine.get_active_tasks():
                yield f"data: {task_prog.model_dump_json()}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Wait for next event or heartbeat
                    event_data = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {event_data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            task_engine.unregister_subscriber(q)
            task_engine.clear_event_loop(loop)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/tasks/{task_id}", response_model=TaskProgress)
def get_task_status(task_id: str):
    """Retrieves status and progress of a specific background task."""
    progress = task_engine.get_task(task_id)
    if not progress:
        raise HTTPException(status_code=404, detail="Task not found")
    return progress


@router.post("/api/tasks/{task_id}/cancel")
def cancel_background_task(task_id: str):
    """Cooperatively cancels a running or queued background task."""
    success = task_engine.cancel_task(task_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Task could not be cancelled or has already completed.",
        )
    return {"status": "ok", "message": f"Cancellation requested for task {task_id}."}


@router.delete("/api/tasks/completed")
def clear_completed_tasks_endpoint():
    """Clears completed and cancelled background tasks from memory."""
    cleared = task_engine.clear_completed_tasks()
    return {"status": "ok", "cleared_count": cleared}


@router.get("/api/system/unified-status")
def get_unified_system_status():
    """
    Returns unified status across Background Scraping Tasks, LLM Inference Queue, and Token Cache.
    """
    llm_status = llm_queue.get_status()
    active_tasks = task_engine.get_active_tasks()
    all_tasks = task_engine.get_all_tasks(limit=10)

    return {
        "active_background_tasks_count": len(active_tasks),
        "active_background_tasks": active_tasks,
        "recent_background_tasks": all_tasks,
        "llm_queue": llm_status,
        "server_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# -------------------------------------------------------------
# Automation Scheduler Endpoints
# -------------------------------------------------------------


class UpdateScheduleRequest(BaseModel):
    is_enabled: Optional[bool] = None
    interval_hours: Optional[float] = None


@router.get("/api/scheduler/jobs", response_model=List[ScheduledJobConfig])
def list_scheduled_jobs():
    """Returns all configured periodic automation jobs and their next scheduled run times."""
    return scheduler.get_all_jobs()


@router.patch("/api/scheduler/jobs/{job_id}", response_model=ScheduledJobConfig)
def update_scheduled_job(job_id: str, req: UpdateScheduleRequest):
    """Updates a periodic job schedule (e.g. enable/disable, change interval)."""
    updated = scheduler.update_job(
        job_id, is_enabled=req.is_enabled, interval_hours=req.interval_hours
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Scheduled job not found")
    return updated


@router.post("/api/scheduler/jobs/{job_id}/run")
def trigger_scheduled_job_now(job_id: str):
    """Triggers immediate on-demand background execution of a scheduled job."""
    task_id = scheduler.run_job_now(job_id, force=True)
    if not task_id:
        reason = scheduler.last_skip_reason()
        if reason:
            # Policy skip (e.g. consent not given) — the job exists but is intentionally blocked.
            raise HTTPException(
                status_code=403,
                detail=f"Cannot run '{job_id}': {reason}.",
            )
        raise HTTPException(
            status_code=404, detail="Scheduled job not found or cannot be triggered."
        )
    return {
        "status": "ok",
        "job_id": job_id,
        "task_id": task_id,
        "message": f"Triggered scheduled job {job_id} as background task {task_id}.",
    }
