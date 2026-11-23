"""LLM provider/routing/queue/token status and the BYOK + embedding settings.

Everything that answers "what is the model layer doing right now" and "which credentials and
embedding backend is it using". The settings writes persist through ``backend.config`` (Fernet-encrypted
for keys), so no app-level state is involved here either.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.ai_helper import TASK_PROFILES, TaskComplexity, detect_active_llm_provider, get_model_for_tier
from backend.config import (
    DEFAULT_LLM_PROVIDER,
    ENABLE_DYNAMIC_MODEL_ROUTING,
    get_llm_credentials,
    is_cloud_fallback_allowed,
    save_llm_settings,
)
from backend.database import get_db
from backend.llm_queue import llm_queue

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


@router.get("/api/llm/status")
def get_llm_status():
    """Returns detailed status and metadata of the currently active LLM provider."""
    return detect_active_llm_provider()


@router.get("/api/llm/routing-status")
def get_llm_routing_status():
    """Returns dynamic model routing configuration, tier assignments, and task complexity profiles."""

    active_provider = detect_active_llm_provider()
    provider_id = active_provider.get("provider", DEFAULT_LLM_PROVIDER).lower()

    return {
        "dynamic_routing_enabled": ENABLE_DYNAMIC_MODEL_ROUTING,
        "active_provider": active_provider,
        "resolved_provider_tiers": {
            "fast": get_model_for_tier(provider_id, TaskComplexity.FAST),
            "standard": get_model_for_tier(provider_id, TaskComplexity.STANDARD),
            "deep_reasoning": get_model_for_tier(
                provider_id, TaskComplexity.DEEP_REASONING
            ),
        },
        "task_profiles": {
            k: {
                "complexity": v["complexity"].value
                if hasattr(v["complexity"], "value")
                else str(v["complexity"]),
                "temperature": v["temperature"],
                "max_tokens": v["max_tokens"],
                "description": v["description"],
                "top_p": v["top_p"],
            }
            for k, v in TASK_PROFILES.items()
        },
    }


@router.get("/api/llm/queue-status")
def get_llm_queue_status():
    """Returns live metrics, queue depth, active task, and backpressure alerts for the local LLM worker queue."""
    return llm_queue.get_status()


@router.get("/api/llm/token-usage")
def get_llm_token_usage():
    """Returns total prompt tokens, completion tokens, inference counts, and cost/savings analytics in O(1) time."""
    from backend.ai_helper import token_tracker

    return token_tracker.get_stats()


@router.post("/api/llm/token-usage/sync")
def sync_llm_token_usage(db: Session = Depends(get_db)):
    """Incrementally aggregates delta token usage from newer operation logs."""
    from backend.ai_helper import token_tracker

    token_tracker.sync_incremental_db_tokens(db=db)
    return token_tracker.get_stats()


@router.post("/api/llm/token-usage/recalculate")
def recalculate_llm_token_usage(db: Session = Depends(get_db)):
    """Reconstructs lifetime token counters from all historical database records."""
    from backend.ai_helper import token_tracker

    return token_tracker.recalculate_from_database(db=db)


@router.delete("/api/llm/token-usage")
def reset_llm_token_usage():
    """Resets the session token usage counter."""
    from backend.ai_helper import token_tracker

    token_tracker.reset()
    return {"message": "Token usage counter successfully reset."}


# -------------------------------------------------------------
# BYOK LLM Settings (persisted, encrypted; env vars override)
# -------------------------------------------------------------
class LlmSettingsIn(BaseModel):
    openai_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    allow_cloud_fallback: Optional[bool] = None


class LlmSettingsOut(BaseModel):
    openai_configured: bool
    gemini_configured: bool
    anthropic_configured: bool
    allow_cloud_fallback: bool


def _masked_llm_settings() -> LlmSettingsOut:
    creds = get_llm_credentials()
    return LlmSettingsOut(
        openai_configured=bool(creds["openai"]),
        gemini_configured=bool(creds["gemini"]),
        anthropic_configured=bool(creds["anthropic"]),
        allow_cloud_fallback=is_cloud_fallback_allowed(),
    )


@router.get("/api/settings/llm", response_model=LlmSettingsOut)
def get_llm_settings():
    """Returns masked BYOK credential status and the cloud-fallback toggle (never raw keys)."""
    return _masked_llm_settings()


@router.put("/api/settings/llm", response_model=LlmSettingsOut)
def update_llm_settings(payload: LlmSettingsIn):
    """
    Persists BYOK credentials (encrypted at rest) and the cloud-fallback toggle.
    Pass an empty string to clear a key; omit a field (null) to leave it unchanged.
    """
    save_llm_settings(
        openai_api_key=payload.openai_api_key,
        gemini_api_key=payload.gemini_api_key,
        anthropic_api_key=payload.anthropic_api_key,
        allow_cloud_fallback=payload.allow_cloud_fallback,
    )
    # Re-evaluate the active provider so LLM queue concurrency/pacing follows the new config.
    try:
        detect_active_llm_provider()
    except Exception:
        pass
    return _masked_llm_settings()


# -------------------------------------------------------------
# Embedding Backend Settings (local-first, 384-d contract)
# - Selecting a model is a machine-level choice; switching the model invalidates
#   existing vectors, so the frontend prompts the user to reindex.
# -------------------------------------------------------------
class EmbeddingSettingsIn(BaseModel):
    backend: Optional[str] = None
    model: Optional[str] = None
    allow_download: Optional[bool] = None
    server_url: Optional[str] = None
    server_model: Optional[str] = None


class EmbeddingSettingsOut(BaseModel):
    backend: str
    model: str
    allow_download: bool
    server_url: str
    server_model: str
    active_backend: str
    model_cached: bool
    dim: int


def _embedding_settings_out() -> "EmbeddingSettingsOut":
    from backend import embeddings
    from backend.config import EMBEDDING_DIM, get_embedding_settings

    settings = get_embedding_settings()
    return EmbeddingSettingsOut(
        backend=settings["backend"],
        model=settings["model"],
        allow_download=settings["allow_download"],
        server_url=settings["server_url"],
        server_model=settings["server_model"],
        active_backend=embeddings.active_backend_name(settings),
        model_cached=embeddings.local_model_cached(settings),
        dim=EMBEDDING_DIM,
    )


@router.get("/api/settings/embeddings", response_model=EmbeddingSettingsOut)
def get_embedding_settings_endpoint():
    """Returns the effective embedding backend/model and whether the model is cached."""
    return _embedding_settings_out()


@router.put("/api/settings/embeddings", response_model=EmbeddingSettingsOut)
def update_embedding_settings_endpoint(payload: EmbeddingSettingsIn):
    """Persists embedding backend/model selection (env vars still override)."""
    from backend.config import save_embedding_settings

    save_embedding_settings(
        backend=payload.backend,
        model=payload.model,
        allow_download=payload.allow_download,
        server_url=payload.server_url,
        server_model=payload.server_model,
    )
    return _embedding_settings_out()


@router.post("/api/settings/embeddings/download", response_model=EmbeddingSettingsOut)
def download_embedding_model():
    """
    Downloads/warms the local ONNX model by embedding a probe text. Enables downloads for
    the call so the model is fetched, then reports cache status.
    """
    from backend import embeddings
    from backend.config import get_embedding_settings

    settings = dict(get_embedding_settings())
    try:
        backend = embeddings.OnnxEmbeddingBackend(
            model_id=settings["model"],
            cache_dir=embeddings.get_embedding_settings_cache_dir(),
            allow_download=True,
        )
        backend.embed(["warmup"])
    except Exception as e:  # noqa: BLE001 - surfaced to the caller as a failed download
        logger.warning(f"Embedding model download failed: {e}")
        raise HTTPException(
            status_code=502, detail=f"Embedding model download failed: {e}"
        ) from e
    return _embedding_settings_out()


@router.post("/api/llm/queue/clear-background")
def clear_background_tasks():
    """Clears all background tasks from the LLM queue."""
    llm_queue.clear_background_tasks()
    return {"message": "Background tasks cleared successfully."}
