"""Read-only operational surface: persisted operation history, the live log tail, and the
learning-readiness report.

These routes were extracted from ``main.py`` as the first router split because they depend only on
the session, the ORM model and the log tailer — no app-level state.
"""

import datetime
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from backend.database import OperationLog, get_db
from backend.observability import tail_log_file

router = APIRouter(tags=["operations"])


class OperationLogOut(BaseModel):
    id: int
    operation_type: str
    status: str
    summary: str
    details_json: Optional[str] = None
    jobs_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


@router.get("/api/operations/logs", response_model=List[OperationLogOut])
def get_operation_logs(limit: int = 50, db: Session = Depends(get_db)):
    """Retrieves persistent audit history of all sync, scrape, and application operations."""
    return (
        db.query(OperationLog)
        .order_by(OperationLog.created_at.desc())
        .limit(limit)
        .all()
    )


@router.delete("/api/operations/logs")
def clear_operation_logs(db: Session = Depends(get_db)):
    """Clears past operation history logs."""
    deleted = db.query(OperationLog).delete()
    db.commit()
    return {"message": f"Successfully cleared {deleted} operation log record(s)."}


@router.get("/api/logs")
def get_application_logs(
    lines: int = Query(default=100, ge=1, le=1000),
    level: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
):
    """
    Observability Log Streamer.
    Tails the active rotating structured JSON log file (logs/app.log).
    Supports optional log level filtering (INFO, WARNING, ERROR) and substring search.
    """
    raw_logs = tail_log_file(lines=lines * 2 if (level or search) else lines)
    filtered = raw_logs
    if level:
        lvl_upper = level.upper()
        filtered = [l for l in filtered if l.get("level", "").upper() == lvl_upper]
    if search:
        s_lower = search.lower()
        filtered = [l for l in filtered if s_lower in json.dumps(l).lower()]

    return {"count": len(filtered[:lines]), "logs": filtered[:lines]}
