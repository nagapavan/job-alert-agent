import json
import pytest
from unittest.mock import patch, MagicMock
from pydantic import BaseModel, Field

from backend.ai_helper import (
    JobMatchResult,
    ParsedResumeSchema,
    ParsedWorkExperience,
    ParsedEducation,
    ConsolidatedApplicationPackageSchema,
    SingleJobMatchItem,
    BatchJobMatchResult,
    generate_structured,
    call_openai,
    call_gemini,
    call_ollama,
    call_anthropic
)

# -------------------------------------------------------------
# 1. Pydantic V2 Schema Validation Unit Tests
# -------------------------------------------------------------

def test_job_match_result_schema_validation():
    # Valid payload
    data = {
        "match_score": 88.5,
        "strengths": ["FastAPI expertise", "Distributed systems"],
        "gaps": ["Lacks Kubernetes"],
        "summary": "Strong backend candidate"
    }
    model = JobMatchResult.model_validate(data)
    assert model.match_score == 88.5
    assert len(model.strengths) == 2
    assert model.summary == "Strong backend candidate"

    # Score clamping (over 100 clamped to 100, under 0 clamped to 0)
    over_model = JobMatchResult.model_validate({"match_score": 150.0, "strengths": []})
    assert over_model.match_score == 100.0

    under_model = JobMatchResult.model_validate({"match_score": -20.0, "strengths": []})
    assert under_model.match_score == 0.0

    # Invalid score string is not silently replaced with a fabricated default
    invalid_model = JobMatchResult.model_validate({"match_score": "not_a_number", "strengths": []})
    assert invalid_model.match_score is None


def test_parsed_resume_schema_validation():
    data = {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": "+1 555 123 4567",
        "summary": "Senior Software Engineer with 8 years of Python experience",
        "skills": ["Python", "FastAPI", "PostgreSQL", "Docker"],
        "work_history": [
            {
                "title": "Lead Engineer",
                "company": "Tech Corp",
                "dates": "2020 - Present",
                "bullets": ["Architected microservices", "Reduced query latency by 40%"]
            }
        ],
        "education": [
            {
                "degree": "B.S. Computer Science",
                "school": "State University",
                "dates": "2012 - 2016"
            }
        ]
    }
    resume = ParsedResumeSchema.model_validate(data)
    assert resume.name == "Jane Doe"
    assert resume.email == "jane@example.com"
    assert len(resume.skills) == 4
    assert len(resume.work_history) == 1
    assert resume.work_history[0].company == "Tech Corp"
    assert len(resume.education) == 1
    assert resume.education[0].degree == "B.S. Computer Science"


def test_consolidated_package_schema_validation():
    data = {
        "match_score": 92.0,
        "strengths": ["Strong engineering depth"],
        "gaps": [],
        "summary": "Excellent fit",
        "tailored_points": "• Highlight distributed caching\n• Emphasize API design",
        "cover_letter": "Dear Hiring Team,\n\nI am thrilled to apply...",
        "cold_message": "Hi there, I saw the opening and would love to connect!"
    }
    pkg = ConsolidatedApplicationPackageSchema.model_validate(data)
    assert pkg.match_score == 92.0
    assert pkg.tailored_points is not None
    assert pkg.cover_letter is not None
    assert pkg.cold_message is not None


def test_batch_job_match_result_schema():
    data = {
        "results": [
            {
                "job_index": 0,
                "match_score": 85.0,
                "strengths": ["Python"],
                "gaps": ["K8s"],
                "summary": "Good match"
            },
            {
                "job_index": 1,
                "match_score": 45.0,
                "strengths": ["SQL"],
                "gaps": ["React"],
                "summary": "Moderate match"
            }
        ]
    }
    batch = BatchJobMatchResult.model_validate(data)
    assert len(batch.results) == 2
    assert batch.results[0].job_index == 0
    assert batch.results[0].match_score == 85.0
    assert batch.results[1].job_index == 1
    assert batch.results[1].match_score == 45.0


# -------------------------------------------------------------
# 2. Provider Protocol Formatting Integration Tests
# -------------------------------------------------------------

@patch("backend.ai_helper.requests.post")
def test_call_openai_with_json_schema(mock_post):
    mock_res = MagicMock()
    mock_res.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"match_score": 90.0, "strengths": ["Go"], "gaps": [], "summary": "Great match"})}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50}
    }
    mock_post.return_value = mock_res

    with patch("backend.config.OPENAI_API_KEY", "sk-test-key"):
        out = call_openai("System", "User", response_schema=JobMatchResult)
        assert mock_post.called
        payload = mock_post.call_args[1]["json"]
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["name"] == "JobMatchResult"
        assert "match_score" in payload["response_format"]["json_schema"]["schema"]["properties"]


@patch("backend.ai_helper.requests.post")
def test_call_gemini_with_response_schema(mock_post):
    mock_res = MagicMock()
    mock_res.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": json.dumps({"match_score": 80.0, "strengths": ["Rust"], "gaps": [], "summary": "Fit"})}]}}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 50}
    }
    mock_post.return_value = mock_res

    with patch("backend.config.GEMINI_API_KEY", "gemini-test-key"):
        out = call_gemini("System", "User", response_schema=JobMatchResult)
        assert mock_post.called
        called_url = mock_post.call_args[0][0]
        called_headers = mock_post.call_args[1]["headers"]
        assert "key=" not in called_url
        assert called_headers.get("x-goog-api-key") == "gemini-test-key"
        payload = mock_post.call_args[1]["json"]
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        assert "match_score" in payload["generationConfig"]["responseSchema"]["properties"]


@patch("backend.ai_helper.requests.post")
def test_call_ollama_with_json_schema(mock_post):
    mock_res = MagicMock()
    mock_res.json.return_value = {
        "message": {"content": json.dumps({"match_score": 75.0, "strengths": ["C++"], "gaps": [], "summary": "Solid"})},
        "prompt_eval_count": 80,
        "eval_count": 40
    }
    mock_post.return_value = mock_res

    out = call_ollama("System", "User", response_schema=JobMatchResult)
    assert mock_post.called
    payload = mock_post.call_args[1]["json"]
    assert isinstance(payload["format"], dict)
    assert "match_score" in payload["format"]["properties"]


@patch("backend.ai_helper.requests.post")
def test_call_anthropic_with_tool_schema(mock_post):
    mock_res = MagicMock()
    mock_res.json.return_value = {
        "content": [
            {
                "type": "tool_use",
                "name": "record_structured_output",
                "input": {"match_score": 85.0, "strengths": ["Python"], "gaps": [], "summary": "Matches role"}
            }
        ],
        "usage": {"input_tokens": 120, "output_tokens": 45}
    }
    mock_post.return_value = mock_res

    with patch("backend.config.ANTHROPIC_API_KEY", "ant-test-key"):
        out = call_anthropic("System", "User", response_schema=JobMatchResult)
        assert mock_post.called
        payload = mock_post.call_args[1]["json"]
        assert "tools" in payload
        assert payload["tool_choice"]["type"] == "tool"
        assert payload["tool_choice"]["name"] == "record_structured_output"
        parsed = json.loads(out)
        assert parsed["match_score"] == 85.0


# -------------------------------------------------------------
# 3. generate_structured & Resilient Fallback Tests
# -------------------------------------------------------------

@patch("backend.ai_helper.generate_text")
def test_generate_structured_direct_json(mock_gen):
    mock_gen.return_value = json.dumps({
        "match_score": 95.0,
        "strengths": ["Kubernetes", "Terraform"],
        "gaps": [],
        "summary": "Exceptional DevOps candidate"
    })

    result: JobMatchResult = generate_structured(
        schema=JobMatchResult,
        system_prompt="System",
        user_prompt="User",
        task_type="job_match_scoring"
    )

    assert isinstance(result, JobMatchResult)
    assert result.match_score == 95.0
    assert "Kubernetes" in result.strengths


@patch("backend.ai_helper.generate_text")
def test_generate_structured_with_markdown_fences_and_thinking_repair(mock_gen):
    # Tests that generate_structured recovers even if local model outputs markdown and thinking tags
    mock_gen.return_value = (
        "<think>Analyzing candidate experience against requirements...</think>\n"
        "Here is the evaluation:\n"
        "```json\n"
        '{\n  "match_score": 82.0,\n  "strengths": ["Asyncio", "FastAPI"],\n  "gaps": ["GraphQL"],\n  "summary": "Good fit"\n}\n'
        "```"
    )

    result: JobMatchResult = generate_structured(
        schema=JobMatchResult,
        system_prompt="System",
        user_prompt="User",
        task_type="job_match_scoring"
    )

    assert isinstance(result, JobMatchResult)
    assert result.match_score == 82.0
    assert result.strengths == ["Asyncio", "FastAPI"]
    assert result.gaps == ["GraphQL"]


# -------------------------------------------------------------
# 4. top_p / topP Forwarding
# -------------------------------------------------------------

def _openai_response():
    mock_res = MagicMock()
    mock_res.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"match_score": 90.0, "strengths": ["Go"], "gaps": [], "summary": "Great match"})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    return mock_res


@patch("backend.ai_helper.requests.post")
def test_call_openai_forwards_top_p(mock_post):
    mock_post.return_value = _openai_response()
    with patch("backend.config.OPENAI_API_KEY", "sk-test-key"):
        call_openai("System", "User", top_p=0.8)
    assert mock_post.call_args[1]["json"]["top_p"] == 0.8


@patch("backend.ai_helper.requests.post")
def test_call_openai_omits_top_p_when_none(mock_post):
    mock_post.return_value = _openai_response()
    with patch("backend.config.OPENAI_API_KEY", "sk-test-key"):
        call_openai("System", "User")
    assert "top_p" not in mock_post.call_args[1]["json"]


@patch("backend.ai_helper.requests.post")
def test_call_gemini_forwards_top_p_as_topP(mock_post):
    mock_res = MagicMock()
    mock_res.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": json.dumps({"match_score": 80.0, "strengths": [], "gaps": [], "summary": "Fit"})}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }
    mock_post.return_value = mock_res
    with patch("backend.config.GEMINI_API_KEY", "gemini-test-key"):
        call_gemini("System", "User", top_p=0.7)
    assert mock_post.call_args[1]["json"]["generationConfig"]["topP"] == 0.7


@patch("backend.ai_helper.requests.post")
def test_call_gemini_omits_topP_when_none(mock_post):
    mock_res = MagicMock()
    mock_res.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": json.dumps({"match_score": 80.0, "strengths": [], "gaps": [], "summary": "Fit"})}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }
    mock_post.return_value = mock_res
    with patch("backend.config.GEMINI_API_KEY", "gemini-test-key"):
        call_gemini("System", "User")
    assert "topP" not in mock_post.call_args[1]["json"]["generationConfig"]


@patch("backend.ai_helper.requests.post")
def test_call_ollama_forwards_top_p(mock_post):
    mock_res = MagicMock()
    mock_res.json.return_value = {
        "message": {"content": json.dumps({"match_score": 75.0, "strengths": [], "gaps": [], "summary": "Solid"})},
        "prompt_eval_count": 10,
        "eval_count": 5,
    }
    mock_post.return_value = mock_res
    call_ollama("System", "User", top_p=0.6)
    assert mock_post.call_args[1]["json"]["options"]["top_p"] == 0.6
