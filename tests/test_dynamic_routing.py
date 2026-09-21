import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.ai_helper import (
    TaskComplexity,
    TASK_PROFILES,
    get_model_for_tier,
    resolve_task_routing,
    generate_text,
    _execute_generate_text
)

client = TestClient(app)

# -------------------------------------------------------------
# Unit Tests: Task Complexity Resolution & Tuning
# -------------------------------------------------------------

def test_task_complexity_resolution():
    # Fast tier tasks
    prov, model, temp, tokens, complexity, _ = resolve_task_routing(task_type="skill_extraction")
    assert complexity == TaskComplexity.FAST
    assert temp == 0.0
    assert tokens == 400

    prov, model, temp, tokens, complexity, _ = resolve_task_routing(task_type="title_cleanup")
    assert complexity == TaskComplexity.FAST
    assert temp == 0.0
    assert tokens == 200

    # Standard tier tasks
    prov, model, temp, tokens, complexity, _ = resolve_task_routing(task_type="job_match_scoring")
    assert complexity == TaskComplexity.STANDARD
    assert temp == 0.2
    assert tokens == 1000

    # Deep reasoning tier tasks
    prov, model, temp, tokens, complexity, _ = resolve_task_routing(task_type="cover_letter")
    assert complexity == TaskComplexity.DEEP_REASONING
    assert temp == 0.8
    assert tokens == 2500

    prov, model, temp, tokens, complexity, _ = resolve_task_routing(task_type="star_interview_prep")
    assert complexity == TaskComplexity.DEEP_REASONING
    assert temp == 0.5
    assert tokens == 2500

def test_user_override_hyperparameters():
    # Explicit user overrides should take precedence over default task profiles
    prov, model, temp, tokens, complexity, _ = resolve_task_routing(
        task_type="cover_letter",
        user_temp=0.8,
        user_max_tokens=500
    )
    assert complexity == TaskComplexity.DEEP_REASONING
    assert temp == 0.8
    assert tokens == 500

# -------------------------------------------------------------
# Unit Tests: Provider-Specific Tier Model Mapping
# -------------------------------------------------------------

def test_dynamic_tier_model_selection():
    # Ollama
    assert get_model_for_tier("ollama", TaskComplexity.FAST) == "qwen2.5:3b"
    assert get_model_for_tier("ollama", TaskComplexity.STANDARD) == "llama3:8b"
    assert get_model_for_tier("ollama", TaskComplexity.DEEP_REASONING) == "llama3.3:70b"

    # OpenAI
    assert get_model_for_tier("openai", TaskComplexity.FAST) == "gpt-4o-mini"
    assert get_model_for_tier("openai", TaskComplexity.DEEP_REASONING) == "gpt-4o"

    # Gemini
    assert get_model_for_tier("gemini", TaskComplexity.FAST) == "gemini-1.5-flash"
    assert get_model_for_tier("gemini", TaskComplexity.DEEP_REASONING) == "gemini-1.5-pro"

    # Anthropic
    assert get_model_for_tier("anthropic", TaskComplexity.FAST) == "claude-3-5-haiku-20241022"
    assert get_model_for_tier("anthropic", TaskComplexity.DEEP_REASONING) == "claude-3-5-sonnet-20240620"

def test_provider_alias_normalization_and_lmstudio_tiers():
    """'LM Studio' (with a space) must normalize to the lmstudio branch, not fall through to 'default'."""
    from backend.config import LM_STUDIO_MODEL
    with patch("backend.ai_helper.LM_STUDIO_FAST_MODEL", ""), \
         patch("backend.ai_helper.LM_STUDIO_STANDARD_MODEL", ""), \
         patch("backend.ai_helper.LM_STUDIO_DEEP_MODEL", ""):
        # No per-tier models configured -> single LM_STUDIO_MODEL, never "default"
        assert get_model_for_tier("LM Studio", TaskComplexity.STANDARD) == LM_STUDIO_MODEL
        assert get_model_for_tier("lm studio", TaskComplexity.DEEP_REASONING) == LM_STUDIO_MODEL
        # With a fast-tier model configured, the fast tier resolves to it (JIT model switching)
        with patch("backend.ai_helper.LM_STUDIO_FAST_MODEL", "qwen2.5-7b-instruct"):
            assert get_model_for_tier("LM Studio", TaskComplexity.FAST) == "qwen2.5-7b-instruct"

# -------------------------------------------------------------
# Integration Tests: End-to-End Dynamic Routing Dispatch
# -------------------------------------------------------------

def test_generate_text_dynamic_routing():
    mock_handler = MagicMock(return_value="Dynamic routed response")
    
    with patch.dict("backend.ai_helper.PROVIDER_REGISTRY", {
        "ollama": {
            "id": "ollama",
            "name": "Ollama",
            "handler": mock_handler,
            "is_local": True,
            "priority": 1,
            "get_tier_model": lambda c: get_model_for_tier("ollama", c)
        }
    }):
        # Call with fast task
        res = generate_text(
            system_prompt="sys",
            user_prompt="user",
            provider="ollama",
            task_type="skill_extraction"
        )
        assert res == "Dynamic routed response"
        mock_handler.assert_called_with(
            "sys",
            "user",
            False,
            temperature=0.0,
            max_tokens=400,
            top_p=0.85,
            model="qwen2.5:3b"
        )

        # Call with deep task
        res_deep = generate_text(
            system_prompt="sys",
            user_prompt="user",
            provider="ollama",
            task_type="cover_letter"
        )
        assert res_deep == "Dynamic routed response"
        mock_handler.assert_called_with(
            "sys",
            "user",
            False,
            temperature=0.8,
            max_tokens=2500,
            top_p=0.95,
            model="llama3.3:70b"
        )

# -------------------------------------------------------------
# API Tests: Dynamic Routing Status Endpoint
# -------------------------------------------------------------

def test_api_llm_routing_status_endpoint():
    res = client.get("/api/llm/routing-status")
    assert res.status_code == 200
    data = res.json()
    assert data["dynamic_routing_enabled"] is True
    assert "tier_models" in data
    assert data["tier_models"]["fast_tier"] == "qwen2.5:3b"
    assert "resolved_provider_tiers" in data
    assert "task_profiles" in data
    assert "cover_letter" in data["task_profiles"]
    assert data["task_profiles"]["cover_letter"]["complexity"] == "deep_reasoning"
