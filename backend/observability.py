"""
Observability and Logging Infrastructure for Job Alert Agent.
Provides structured JSON file logging with rotation, colored terminal logging,
request correlation tracking, LLM audit trails, and log tailing utilities.
"""

import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, List, Dict, Any

from backend.config import BASE_DIR

LOGS_DIR = BASE_DIR / "logs"
APP_LOG_PATH = LOGS_DIR / "app.log"
LLM_AUDIT_LOG_PATH = LOGS_DIR / "llm_audit.log"

# Standard noisy third-party loggers to silence/suppress
NOISY_LOGGERS = [
    "httpx",
    "httpcore",
    "urllib3",
    "uvicorn.access",
    "uvicorn.error",
    "asyncio",
    "playwright"
]


class JSONFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects for log aggregation and jq parsing.
    """
    def format(self, record: logging.LogRecord) -> str:
        log_obj: Dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "line": record.lineno,
        }
        
        # Include request_id / correlation_id if attached to record
        if hasattr(record, "request_id") and record.request_id:
            log_obj["request_id"] = record.request_id
            
        # Include extra structured fields if present
        if hasattr(record, "extra_data") and isinstance(record.extra_data, dict):
            log_obj.update(record.extra_data)
            
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
            
        return json.dumps(log_obj, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """
    Clean, human-readable terminal formatter with optional ANSI color highlights.
    """
    COLORS = {
        "DEBUG": "\033[36m",    # Cyan
        "INFO": "\033[32m",     # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",    # Red
        "CRITICAL": "\033[35m", # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, self.RESET)
        ts = self.formatTime(record, "%H:%M:%S")
        req_part = f" [{record.request_id[:8]}]" if hasattr(record, "request_id") and record.request_id else ""
        msg = record.getMessage()
        return f"{ts} {color}{record.levelname:<7}{self.RESET} [{record.name}]{req_part} {msg}"


def setup_observability(
    log_level: Optional[str] = None,
    log_dir: Optional[Path] = None,
    max_bytes: Optional[int] = None,
    backup_count: Optional[int] = None
) -> None:
    """
    Configures centralized, self-rotating logging for the entire application.
    Dual-sink:
      1. Terminal / stdout via ConsoleFormatter
      2. Rotating JSON log file (logs/app.log) via JSONFormatter
      3. Dedicated rotating JSON sink for the LLM audit trail (logs/llm_audit.log)
    Rotation is env-configurable so long-running deployments never overflow:
      LOG_LEVEL, LOG_MAX_BYTES (default 10 MiB), LOG_BACKUP_COUNT (default 5).
    """
    level_name = (log_level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    numeric_level = getattr(logging, level_name, logging.INFO)
    if max_bytes is None:
        max_bytes = int(os.environ.get("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
    if backup_count is None:
        backup_count = int(os.environ.get("LOG_BACKUP_COUNT", "5"))

    target_dir = log_dir or LOGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    app_log_file = target_dir / "app.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Avoid duplicate handlers on re-initialization
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    # 1. Console StreamHandler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(ConsoleFormatter())
    root_logger.addHandler(console_handler)

    # 2. Rotating File Handler (JSON format)
    try:
        file_handler = RotatingFileHandler(
            str(app_log_file),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8"
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(JSONFormatter())
        root_logger.addHandler(file_handler)
    except Exception as e:
        sys.stderr.write(f"Warning: Could not configure file logging at {app_log_file}: {e}\n")

    # 3. Dedicated rotating sink for the LLM audit trail (kept out of app.log)
    try:
        audit_logger = logging.getLogger("llm_audit")
        for h in list(audit_logger.handlers):
            audit_logger.removeHandler(h)
        audit_log_file = target_dir / "llm_audit.log"
        audit_handler = RotatingFileHandler(
            str(audit_log_file),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8"
        )
        audit_handler.setLevel(numeric_level)
        audit_handler.setFormatter(JSONFormatter())
        audit_logger.addHandler(audit_handler)
        audit_logger.propagate = False
    except Exception as e:
        sys.stderr.write(f"Warning: Could not configure LLM audit logging: {e}\n")

    # 4. Suppress verbose third-party loggers
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger("api").info(
        f"Observability initialized. Level={level_name}, LogFile={app_log_file}, "
        f"Rotation={max_bytes}B x {backup_count}"
    )


def log_llm_event(
    task_name: str,
    model: str,
    provider: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: float = 0.0,
    temperature: float = 0.0,
    is_cache_hit: bool = False,
    task_type: str = "general",
    error: Optional[str] = None
) -> None:
    """
    Logs structured audit telemetry for an LLM generation or memory cache resolution.
    """
    llm_logger = logging.getLogger("llm_audit")
    extra = {
        "extra_data": {
            "event": "llm_invocation",
            "task_name": task_name,
            "task_type": task_type,
            "model": model,
            "provider": provider,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "latency_ms": round(latency_ms, 2),
            "temperature": temperature,
            "is_cache_hit": is_cache_hit,
            "error": error
        }
    }
    msg = (
        f"LLM [{task_name}] provider={provider} model={model} "
        f"tokens={prompt_tokens}+{completion_tokens} lat={latency_ms:.1f}ms "
        f"temp={temperature} hit={is_cache_hit}"
    )
    if error:
        llm_logger.error(f"{msg} error={error}", extra=extra)
    else:
        llm_logger.info(msg, extra=extra)


def tail_log_file(lines: int = 100, log_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Reads and parses the last `lines` entries from the rotating JSON log file.
    Returns structured log entries. Falls back to raw lines if non-JSON.
    """
    target = log_path or APP_LOG_PATH
    if not target.exists():
        return []

    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            tail_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines

        parsed = []
        for line in tail_lines:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                parsed.append(json.loads(line_str))
            except json.JSONDecodeError:
                parsed.append({
                    "timestamp": "",
                    "level": "INFO",
                    "logger": "raw",
                    "message": line_str
                })
        return parsed
    except Exception as e:
        return [{"level": "ERROR", "message": f"Failed reading log file: {e}"}]
