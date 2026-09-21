"""
Tests for sustainable log rotation: rotating file handlers and operation-log DB rotation.
"""

import logging
import datetime
from logging.handlers import RotatingFileHandler

from backend.database import OperationLog, prune_operation_logs, utc_now
from backend.observability import setup_observability


def test_setup_observability_applies_rotation_config(tmp_path):
    setup_observability(log_level="INFO", log_dir=tmp_path, max_bytes=1234, backup_count=3)

    root_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
    ]
    assert root_handlers, "app.log rotating handler must be configured"
    assert root_handlers[0].maxBytes == 1234
    assert root_handlers[0].backupCount == 3

    audit_handlers = [
        h for h in logging.getLogger("llm_audit").handlers if isinstance(h, RotatingFileHandler)
    ]
    assert audit_handlers, "llm_audit rotating handler must be configured"
    assert audit_handlers[0].maxBytes == 1234


def test_prune_operation_logs_applies_retention_and_cap(db_session):
    now = utc_now()
    for _ in range(3):  # older than retention -> dropped
        db_session.add(OperationLog(
            operation_type="t", status="Completed", summary="old",
            created_at=now - datetime.timedelta(days=40)
        ))
    for i in range(5):  # recent -> capped to 4 (oldest of these pruned)
        db_session.add(OperationLog(
            operation_type="t", status="Completed", summary="new",
            created_at=now - datetime.timedelta(days=i)
        ))
    db_session.commit()

    deleted = prune_operation_logs(db_session, max_rows=4, retention_days=30)

    assert deleted == 4  # 3 expired + 1 over the cap
    assert db_session.query(OperationLog).count() == 4


def test_prune_operation_logs_noop_when_within_limits(db_session):
    db_session.add(OperationLog(
        operation_type="t", status="Completed", summary="fresh", created_at=utc_now()
    ))
    db_session.commit()

    assert prune_operation_logs(db_session, max_rows=100, retention_days=30) == 0
    assert db_session.query(OperationLog).count() == 1
