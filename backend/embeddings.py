"""Pluggable embedding backends (local-first, fixed 384-d contract).

Preference order mirrors the repo's local-first philosophy:

1. **standalone** — in-process ONNX model (``bge-small-en-v1.5`` by default). No external
   server; runs on CPU through ``onnxruntime`` + ``tokenizers``.
2. **external**   — any OpenAI-compatible embeddings endpoint (``/v1/embeddings``):
   Ollama, LM Studio, Unsloth, vLLM, llama.cpp, or a fully custom URL.
3. **hash**       — deterministic character/word hash fallback. No semantics; it only
   keeps the app functional fully offline, so it is always the last resort.

Every backend emits L2-normalized vectors of ``EMBEDDING_DIM`` (384) dimensions so the
existing ``SafeVector(384)`` columns and cosine-similarity code remain valid when the
backend, server, or model changes. Changing the model does not change the dimension, but
existing rows must be re-embedded to stay comparable.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import List, Optional, Protocol, Sequence, runtime_checkable

from backend.config import (
    EMBEDDING_DIM,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    get_embedding_settings,
)

logger = logging.getLogger("embeddings")

# The ONNX runtime expects these two files inside the HuggingFace repo.
_ONNX_MODEL_FILE = "onnx/model.onnx"
_ONNX_TOKENIZER_FILE = "tokenizer.json"
_MAX_SEQ_LENGTH = 256
_TRUNCATE_CHARS = 4000


def _l2_normalize(vector: Sequence[float]) -> List[float]:
    norm = sum(x * x for x in vector) ** 0.5
    if norm > 0:
        return [x / norm for x in vector]
    return list(vector)


@runtime_checkable
class EmbeddingBackend(Protocol):
    """Minimal contract every embedding backend satisfies."""

    name: str
    dim: int

    def available(self) -> bool:
        """Cheap capability check; MUST NOT download or make blocking network calls."""

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Return one L2-normalized vector per input text."""


# ---------------------------------------------------------------------------
# Deterministic fallback (no model, no network)
# ---------------------------------------------------------------------------


class HashEmbeddingBackend:
    """Deterministic character/word hash vectorizer; the offline last resort."""

    name = "hash"

    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim

    def available(self) -> bool:
        return True

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._one(t or "") for t in texts]

    def _one(self, text: str) -> List[float]:
        if not text or not text.strip():
            return [0.0] * self.dim
        vector = [0.0] * self.dim
        for word in text[:_TRUNCATE_CHARS].lower().split():
            h = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            weight = 1.0 + (len(word) / 10.0)
            vector[idx] += weight
        return _l2_normalize(vector)


# ---------------------------------------------------------------------------
# External embedding server (OpenAI-compatible)
# ---------------------------------------------------------------------------

# Normalizes a base URL to its OpenAI-compatible embeddings path.
# Ollama exposes /v1/embeddings; LM Studio/Unsloth/vLLM/llama.cpp expose
# <base>/embeddings. This covers all of them without hardcoding a specific product.
_DEFAULT_OLLAMA_URL = (OLLAMA_BASE_URL or "http://localhost:11434").rstrip("/")


def _embeddings_endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = _DEFAULT_OLLAMA_URL
    if base.endswith("/embeddings"):
        return base
    if base.endswith("/v1"):
        return f"{base}/embeddings"
    # A bare host (e.g. http://localhost:11434) -> OpenAI-compatible path.
    return f"{base}/v1/embeddings"


class ExternalEmbeddingBackend:
    """Any OpenAI-compatible embeddings server (Ollama/LM Studio/Unsloth/custom)."""

    name = "external"

    def __init__(
        self,
        server_url: str = "",
        model: str = "",
        dim: int = EMBEDDING_DIM,
        timeout: int = 10,
    ):
        self.server_url = (server_url or "").strip()
        self.model = (model or "").strip() or OLLAMA_MODEL
        self.dim = dim
        self.timeout = timeout

    def available(self) -> bool:
        # Do not probe the network here; a down server simply fails through to the
        # next backend in the chain (preserving the previous silent-fallback behaviour).
        return True

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        import requests

        url = _embeddings_endpoint(self.server_url)
        results: List[List[float]] = []
        for text in texts:
            payload = {"model": self.model, "input": (text or "")[:_TRUNCATE_CHARS]}
            res = requests.post(url, json=payload, timeout=self.timeout)
            if res.status_code != 200:
                raise RuntimeError(f"Embedding server HTTP {res.status_code}")
            body = res.json()
            # OpenAI shape: {"data": [{"embedding": [...]}]}; Ollama also accepts "embedding".
            embedding = []
            data = body.get("data")
            if isinstance(data, list) and data:
                embedding = data[0].get("embedding") or []
            if not embedding:
                embedding = body.get("embedding") or []
            if not embedding:
                raise RuntimeError("Embedding server returned an empty embedding")
            results.append(self._to_dim(embedding))
        return results

    def _to_dim(self, embedding: List[float]) -> List[float]:
        if len(embedding) > self.dim:
            vec = embedding[: self.dim]
        elif len(embedding) < self.dim:
            vec = list(embedding) + [0.0] * (self.dim - len(embedding))
        else:
            vec = list(embedding)
        return _l2_normalize(vec)


# ---------------------------------------------------------------------------
# Local in-process ONNX model
# ---------------------------------------------------------------------------


def _onnx_deps_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401

        return True
    except Exception:
        return False


class OnnxEmbeddingBackend:
    """In-process ONNX sentence-embedding backend (CPU).

    The model is downloaded lazily on first ``embed`` only when ``allow_download`` is
    set; otherwise the backend is simply unavailable and the chain falls through.
    """

    name = "standalone"

    def __init__(
        self,
        model_id: str,
        cache_dir: Path,
        dim: int = EMBEDDING_DIM,
        allow_download: bool = False,
    ):
        self.model_id = model_id
        self.cache_dir = Path(cache_dir)
        self.dim = dim
        self.allow_download = allow_download
        self._session = None
        self._tokenizer = None
        self._input_names: set = set()
        self._lock = threading.Lock()

    # -- availability -----------------------------------------------------
    def _cached(self, filename: str) -> bool:
        try:
            from huggingface_hub import try_to_load_from_cache

            path = try_to_load_from_cache(
                self.model_id, filename, cache_dir=str(self.cache_dir)
            )
            return isinstance(path, str)
        except Exception:
            return False

    def available(self) -> bool:
        if not _onnx_deps_available():
            return False
        if self._session is not None:
            return True
        if self.allow_download:
            return True
        return self._cached(_ONNX_MODEL_FILE) and self._cached(_ONNX_TOKENIZER_FILE)

    # -- loading ----------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer

            local_only = not self.allow_download
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            model_path = hf_hub_download(
                self.model_id,
                _ONNX_MODEL_FILE,
                cache_dir=str(self.cache_dir),
                local_files_only=local_only,
            )
            tokenizer_path = hf_hub_download(
                self.model_id,
                _ONNX_TOKENIZER_FILE,
                cache_dir=str(self.cache_dir),
                local_files_only=local_only,
            )
            tokenizer = Tokenizer.from_file(tokenizer_path)
            tokenizer.enable_padding()
            tokenizer.enable_truncation(max_length=_MAX_SEQ_LENGTH)
            self._tokenizer = tokenizer
            self._session = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
            self._input_names = {i.name for i in self._session.get_inputs()}
            logger.info("Loaded local ONNX embedding model %s", self.model_id)

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        import numpy as np

        self._ensure_loaded()
        encodings = self._tokenizer.encode_batch([(t or "")[:_TRUNCATE_CHARS] for t in texts])
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feed = {"input_ids": input_ids, "attention_mask": attention_mask}
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros_like(input_ids)
        outputs = self._session.run(None, feed)
        hidden = outputs[0]
        # BGE models use CLS pooling (first token) followed by L2 normalization.
        cls_vectors = hidden[:, 0, :]
        return [_l2_normalize(row.tolist()) for row in cls_vectors]


# ---------------------------------------------------------------------------
# Chain resolution & public entrypoints
# ---------------------------------------------------------------------------

_EMBED_LOCK = threading.Lock()


def resolve_backends(settings: Optional[dict] = None) -> List[EmbeddingBackend]:
    """Return the ordered backend chain for the current (or supplied) settings."""
    s = settings or get_embedding_settings()
    backend = s.get("backend", "auto")
    model = s.get("model") or "Xenova/bge-small-en-v1.5"
    allow_download = bool(s.get("allow_download"))
    server_url = s.get("server_url") or ""
    server_model = s.get("server_model") or ""

    standalone = OnnxEmbeddingBackend(
        model_id=model,
        cache_dir=get_embedding_settings_cache_dir(),
        allow_download=allow_download,
    )
    external = ExternalEmbeddingBackend(server_url=server_url, model=server_model)
    hash_backend = HashEmbeddingBackend()

    if backend == "hash":
        return [hash_backend]
    if backend in ("external", "ollama"):
        # "ollama" is a back-compat alias for the external endpoint pinned to Ollama.
        if backend == "ollama" and not server_url:
            external = ExternalEmbeddingBackend(server_url=_DEFAULT_OLLAMA_URL, model=server_model)
        return [external, hash_backend]
    if backend == "standalone":
        return [standalone, external, hash_backend]
    # "auto": standalone wins only when already cached (or downloads are explicitly allowed).
    return [standalone, external, hash_backend]


def get_embedding_settings_cache_dir() -> Path:
    from backend.config import EMBEDDING_CACHE_DIR

    return EMBEDDING_CACHE_DIR


def embed_texts(texts: Sequence[str], settings: Optional[dict] = None) -> List[List[float]]:
    """Embed a batch of texts, trying each backend in the resolution chain in order."""
    texts = list(texts)
    if not texts:
        return []
    last_error: Optional[Exception] = None
    for backend in resolve_backends(settings):
        try:
            if not backend.available():
                continue
            return backend.embed(texts)
        except Exception as exc:  # noqa: BLE001 - chain deliberately swallows per-backend errors
            last_error = exc
            logger.debug("Embedding backend %s failed: %s", backend.name, exc)
            continue
    # HashEmbeddingBackend is always available, so this is defensive only.
    logger.warning("All embedding backends failed (%s); using hash fallback", last_error)
    return HashEmbeddingBackend().embed(texts)


def embed_text(text: str, settings: Optional[dict] = None) -> List[float]:
    """Embed a single string, returning a 384-d L2-normalized vector."""
    if not text or not text.strip():
        return [0.0] * EMBEDDING_DIM
    return embed_texts([text], settings=settings)[0]


def active_backend_name(settings: Optional[dict] = None) -> str:
    """Name of the first backend that would actually run for the current settings."""
    for backend in resolve_backends(settings):
        try:
            if backend.available():
                return backend.name
        except Exception:
            continue
    return "hash"


def local_model_cached(settings: Optional[dict] = None) -> bool:
    """True when the configured ONNX model is present in the local cache."""
    s = settings or get_embedding_settings()
    backend = OnnxEmbeddingBackend(
        model_id=s.get("model") or "Xenova/bge-small-en-v1.5",
        cache_dir=get_embedding_settings_cache_dir(),
        allow_download=False,
    )
    return backend.available()
