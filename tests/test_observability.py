"""
Unit and Integration Tests for Observability, Structured Logging, and Request Tracing.
"""

import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.observability import (
    JSONFormatter,
    ConsoleFormatter,
    setup_observability,
    log_llm_event,
    tail_log_file
)

client = TestClient(app)


def test_sanitize_log_details_redacts_and_stays_valid_json():
    """OperationLog details must redact secrets/emails while remaining parseable JSON."""
    from backend.main import sanitize_log_details

    raw = sanitize_log_details({
        "email": "candidate@example.com",
        "api_key": "sk-secret-123",
        "nested": {"refresh_token": "tok-abc"},
        "companies": ["Stripe", "OpenAI"],
    })
    assert raw is not None
    parsed = json.loads(raw)  # must not raise
    assert parsed["email"] == "[REDACTED_EMAIL]"
    assert parsed["api_key"] == "[REDACTED]"
    assert parsed["nested"]["refresh_token"] == "[REDACTED]"
    assert parsed["companies"] == ["Stripe", "OpenAI"]

    # Empty input -> None
    assert sanitize_log_details(None) is None
    assert sanitize_log_details("") is None


class TestObservabilityFormatters:
    def test_json_formatter_valid_output(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="User initiated application scan",
            args=(),
            exc_info=None
        )
        record.request_id = "req_123456"
        record.extra_data = {"jobs_found": 5, "source": "Greenhouse"}

        formatted = formatter.format(record)
        data = json.loads(formatted)

        assert data["level"] == "INFO"
        assert data["logger"] == "test_logger"
        assert data["message"] == "User initiated application scan"
        assert data["request_id"] == "req_123456"
        assert data["jobs_found"] == 5
        assert data["source"] == "Greenhouse"
        assert "timestamp" in data

    def test_console_formatter_output(self):
        formatter = ConsoleFormatter()
        record = logging.LogRecord(
            name="api",
            level=logging.WARNING,
            pathname="main.py",
            lineno=10,
            msg="Rate limit near threshold",
            args=(),
            exc_info=None
        )
        record.request_id = "req_abcdef123456"
        formatted = formatter.format(record)

        assert "WARNING" in formatted
        assert "[api]" in formatted
        assert "[req_abcd]" in formatted
        assert "Rate limit near threshold" in formatted


class TestObservabilitySetup:
    def test_setup_observability_creates_log_file_and_handlers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            setup_observability(log_level="DEBUG", log_dir=tmp_path)

            root = logging.getLogger()
            assert root.level == logging.DEBUG
            assert len(root.handlers) >= 2

            test_logger = logging.getLogger("test_obs")
            test_logger.info("Test observability setup log line")

            # Verify file exists and has content
            log_file = tmp_path / "app.log"
            assert log_file.exists()

            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
                assert len(lines) >= 1
                parsed = json.loads(lines[-1])
                assert parsed["message"] == "Test observability setup log line"

    def test_tail_log_file_parsing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_file = Path(tmp_dir) / "app.log"
            with open(log_file, "w", encoding="utf-8") as f:
                for i in range(10):
                    f.write(json.dumps({
                        "timestamp": f"2026-09-19T21:00:{i:02d}",
                        "level": "INFO" if i % 2 == 0 else "ERROR",
                        "logger": "api",
                        "message": f"Log message {i}"
                    }) + "\n")

            tail = tail_log_file(lines=5, log_path=log_file)
            assert len(tail) == 5
            assert tail[-1]["message"] == "Log message 9"


class TestLLMAuditLogging:
    def test_log_llm_event_success(self):
        with patch("logging.Logger.info") as mock_info:
            log_llm_event(
                task_name="CoverLetter-Acme",
                model="qwen2.5:7b",
                provider="lm_studio",
                prompt_tokens=450,
                completion_tokens=220,
                latency_ms=1240.5,
                temperature=0.8,
                is_cache_hit=False,
                task_type="cover_letter"
            )
            mock_info.assert_called_once()
            args, kwargs = mock_info.call_args
            assert "CoverLetter-Acme" in args[0]
            assert "extra_data" in kwargs.get("extra", {})
            assert kwargs["extra"]["extra_data"]["total_tokens"] == 670

    def test_log_llm_event_error(self):
        with patch("logging.Logger.error") as mock_error:
            log_llm_event(
                task_name="JobMatch-Uber",
                model="llama3:8b",
                provider="ollama",
                latency_ms=50.0,
                task_type="job_match",
                error="Connection refused on :11434"
            )
            mock_error.assert_called_once()
            args, kwargs = mock_error.call_args
            assert "Connection refused" in args[0]
            assert kwargs["extra"]["extra_data"]["error"] == "Connection refused on :11434"


class TestHTTPMiddlewareAndEndpoint:
    def test_request_observability_middleware_adds_x_request_id(self):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert "X-Request-ID" in resp.headers
        assert resp.headers["X-Request-ID"].startswith("req_")

    def test_api_logs_endpoint(self):
        resp = client.get("/api/logs?lines=20")
        assert resp.status_code == 200
        data = resp.json()
        assert "count" in data
        assert "logs" in data
        assert isinstance(data["logs"], list)

    def test_api_logs_filter_by_level(self):
        resp = client.get("/api/logs?lines=50&level=INFO")
        assert resp.status_code == 200
        data = resp.json()
        for entry in data["logs"]:
            if "level" in entry:
                assert entry["level"] == "INFO"
