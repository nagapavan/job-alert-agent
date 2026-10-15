"""Tests for the generic quality Judge gate, the LLM audit trace/content capture, and the
centralized prompts module.

Covers:
- judge_output: pass-through, bounded re-generation, non-fatal failure, empty-text guard.
- observability.log_llm_event: trace_id attached and privacy-gated content capture.
- prompts module: interview questions cap, match-scoring template, copilot builder.
"""
import logging

import pytest
from unittest.mock import patch

from backend.ai_helper import JudgeVerdict, judge_output
from backend import observability
from backend.prompts import (
    JOB_MATCH_SCORING_SYSTEM,
    JUDGE_SYSTEM,
    career_copilot_system,
    interview_questions_system,
)


# ---------------------------------------------------------------- judge_output

@pytest.fixture
def capture_llm_audit():
    """Attach a capturing handler to the llm_audit logger and yield the captured list."""
    captured = []
    logger = logging.getLogger("llm_audit")
    logger.setLevel(logging.DEBUG)

    class _Handler(logging.Handler):
        def emit(self, record):
            captured.append(record)

    handler = _Handler()
    old_propagate = logger.propagate
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield captured
    finally:
        logger.removeHandler(handler)
        logger.propagate = old_propagate


def test_judge_output_passes_returns_original():
    with patch("backend.ai_helper.generate_structured") as mock_structured:
        mock_structured.return_value = JudgeVerdict(score=5.0, pass_=True, critique="Great")
        result = judge_output("This is a solid answer.", context="some context")
    assert result["text"] == "This is a solid answer."
    assert result["regenerated"] is False
    assert result["regenerations"] == 0
    assert result["verdict"].pass_ is True


def test_judge_output_fails_then_regenerates_bounded():
    with patch("backend.ai_helper.generate_structured") as mock_structured:
        mock_structured.side_effect = [
            JudgeVerdict(score=2.0, pass_=False, critique="Hallucinated a metric"),
            JudgeVerdict(score=5.0, pass_=True, critique="Now accurate"),
        ]
        result = judge_output(
            "draft answer",
            context="context",
            regenerator=lambda critique: "revised answer addressing " + critique,
            max_regenerations=1,
        )
    assert result["text"] == "revised answer addressing Hallucinated a metric"
    assert result["regenerated"] is True
    assert result["regenerations"] == 1
    assert result["verdict"].pass_ is True


def test_judge_output_fails_no_regenerator_keeps_original():
    with patch("backend.ai_helper.generate_structured") as mock_structured:
        mock_structured.return_value = JudgeVerdict(score=1.0, pass_=False, critique="bad")
        result = judge_output("original", context="c")
    assert result["text"] == "original"
    assert result["regenerated"] is False
    assert result["verdict"].pass_ is False


def test_judge_output_exception_is_non_fatal():
    with patch("backend.ai_helper.generate_structured", side_effect=Exception("offline")):
        result = judge_output("survive", context="c")
    assert result["text"] == "survive"
    assert result["verdict"] is None
    assert result["regenerated"] is False


def test_judge_output_empty_text_short_circuits():
    with patch("backend.ai_helper.generate_structured") as mock_structured:
        result = judge_output("   ", context="c")
        mock_structured.assert_not_called()
    assert result["text"] == "   "
    assert result["verdict"] is None


def test_judge_output_stops_when_regenerator_returns_same_text():
    with patch("backend.ai_helper.generate_structured") as mock_structured:
        mock_structured.return_value = JudgeVerdict(score=2.0, pass_=False, critique="fix")
        result = judge_output(
            "same", context="c", regenerator=lambda critique: "same", max_regenerations=5
        )
    # No infinite loop; text unchanged because regenerator returned identical text.
    assert result["text"] == "same"
    assert result["regenerations"] == 0


# ---------------------------------------------------------------- audit content capture

def test_log_llm_event_capture_off_no_content(monkeypatch, capture_llm_audit):
    """When content capture is off (default), no prompt/response content is persisted."""
    monkeypatch.setattr(observability, "LLM_AUDIT_CAPTURE_CONTENT", False)
    observability.log_llm_event(
        task_name="t", model="m", provider="p",
        prompt_content="secret resume data", response_content="reply",
        trace_id="trace-1",
    )
    assert capture_llm_audit
    rec = capture_llm_audit[-1]
    extra = rec.extra_data
    assert "prompt_truncated" not in extra
    assert "response_truncated" not in extra
    assert extra["trace_id"] == "trace-1"


def test_log_llm_event_capture_on_truncates(monkeypatch, capture_llm_audit):
    """When capture is on, the prompt/response are stored, truncated to the configured cap."""
    monkeypatch.setattr(observability, "LLM_AUDIT_CAPTURE_CONTENT", True)
    monkeypatch.setattr(observability, "LLM_AUDIT_MAX_CONTENT_CHARS", 20)
    observability.log_llm_event(
        task_name="t", model="m", provider="p",
        prompt_content="a" * 100, response_content="b" * 100,
    )
    rec = capture_llm_audit[-1]
    extra = rec.extra_data
    assert "prompt_truncated" in extra
    assert len(extra["prompt_truncated"]) <= 30  # 20 chars + possible ellipsis
    assert "response_truncated" in extra


def test_truncate_text_short_and_empty():
    assert observability.truncate_text("") == ""
    assert observability.truncate_text("short") == "short"
    long = "x" * 500
    assert len(observability.truncate_text(long, 10)) <= 15


# ---------------------------------------------------------------- prompts module

def test_interview_questions_system_caps_count():
    assert "Generate at most 3 questions" in interview_questions_system(3)
    assert "Generate at most 15 questions" in interview_questions_system(999)
    # 0 is falsy -> falls back to the default of 6 (preserves original `or 6` semantics).
    assert "Generate at most 6 questions" in interview_questions_system(0)


def test_job_match_scoring_system_is_constant():
    assert "match_score MUST be an integer or float between 0 and 100" in JOB_MATCH_SCORING_SYSTEM
    assert JOB_MATCH_SCORING_SYSTEM == JOB_MATCH_SCORING_SYSTEM


def test_career_copilot_system_includes_fields():
    prompt = career_copilot_system(
        candidate_name="Alice",
        candidate_experience="8 years",
        skills_summary="Python, Go",
        total_jobs=10,
        to_apply_count=4,
        shortlisted_count=3,
        applied_count=2,
        interview_count=1,
        top_matches_summary="Backend @ Co (90%)",
        focused_job_snippet="- Focused Job: Staff Engineer",
    )
    assert "- Name: Alice" in prompt
    assert "- Experience: 8 years" in prompt
    assert "- Total Tracked: 10 roles" in prompt
    assert "Focused Job: Staff Engineer" in prompt
    assert "STAR method" in prompt


def test_judge_system_prompt_mentions_scoring():
    assert "0-5" in JUDGE_SYSTEM
    assert "groundedness" in JUDGE_SYSTEM
