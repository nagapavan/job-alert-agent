"""Tests for the pluggable embedding backend chain (Phase 1).

The suite is hermetic: the local ONNX backend never downloads during tests
(``allow_download`` is forced off by conftest), so the chain resolves to the
deterministic hash backend unless a test injects a fake.
"""
import math

import pytest

from conftest import test_client as client

from backend import embeddings
from backend.embeddings import (
    EMBEDDING_DIM,
    ExternalEmbeddingBackend,
    HashEmbeddingBackend,
    OnnxEmbeddingBackend,
    embed_text,
    embed_texts,
    resolve_backends,
)


def _norm(vec):
    return math.sqrt(sum(x * x for x in vec))


class TestHashBackend:
    def test_deterministic_and_normalized(self):
        a = HashEmbeddingBackend().embed(["Python FastAPI Kubernetes"])[0]
        b = HashEmbeddingBackend().embed(["Python FastAPI Kubernetes"])[0]
        assert a == b
        assert len(a) == EMBEDDING_DIM
        assert _norm(a) == pytest.approx(1.0, abs=1e-6)

    def test_empty_text_is_zero_vector(self):
        vec = HashEmbeddingBackend().embed([""])[0]
        assert vec == [0.0] * EMBEDDING_DIM

    def test_different_text_differs(self):
        a = HashEmbeddingBackend().embed(["machine learning"])[0]
        b = HashEmbeddingBackend().embed(["payroll benefits"])[0]
        assert a != b


class TestChainResolution:
    def test_backend_order_per_setting(self):
        s = lambda backend: {"backend": backend, "model": "x", "allow_download": False,
                             "server_url": "", "server_model": ""}
        names = lambda backend: [b.name for b in resolve_backends(s(backend))]
        assert names("hash") == ["hash"]
        assert names("external") == ["external", "hash"]
        assert names("ollama") == ["external", "hash"]
        assert names("standalone") == ["standalone", "external", "hash"]
        assert names("auto") == ["standalone", "external", "hash"]

    def test_falls_through_when_standalone_unavailable(self, monkeypatch):
        # standalone unavailable (not cached, no download) and external unreachable -> hash.
        monkeypatch.setattr(OnnxEmbeddingBackend, "available", lambda self: False)
        monkeypatch.setattr(
            ExternalEmbeddingBackend, "embed",
            lambda self, texts: (_ for _ in ()).throw(RuntimeError("down")),
        )
        settings = {"backend": "auto", "model": "x", "allow_download": False,
                    "server_url": "", "server_model": ""}
        vec = embed_text("Python", settings=settings)
        assert len(vec) == EMBEDDING_DIM
        assert _norm(vec) == pytest.approx(1.0, abs=1e-6)

    def test_prefers_first_available_backend(self, monkeypatch):
        calls = {}

        class _FakeBackend:
            name = "standalone"

            def available(self):
                return True

            def embed(self, texts):
                calls["used"] = True
                return [[0.5, 0.5] for _ in texts]

        monkeypatch.setattr(embeddings, "resolve_backends", lambda settings=None: [_FakeBackend()])
        out = embed_texts(["hello"])
        assert calls.get("used") is True
        assert out == [[0.5, 0.5]]

    def test_empty_batch_returns_empty(self):
        assert embed_texts([]) == []

    def test_empty_text_returns_zero_vector(self):
        assert embed_text("") == [0.0] * EMBEDDING_DIM
        assert embed_text("   ") == [0.0] * EMBEDDING_DIM


class TestExternalEndpointResolution:
    def test_bare_host_gets_openai_path(self):
        assert embeddings._embeddings_endpoint("http://localhost:11434") == (
            "http://localhost:11434/v1/embeddings"
        )

    def test_v1_suffix(self):
        assert embeddings._embeddings_endpoint("http://localhost:1234/v1") == (
            "http://localhost:1234/v1/embeddings"
        )

    def test_explicit_embeddings_path_preserved(self):
        assert embeddings._embeddings_endpoint("http://host/embeddings") == "http://host/embeddings"

    def test_empty_defaults_to_ollama(self):
        assert embeddings._embeddings_endpoint("").endswith("/v1/embeddings")


class TestDimContract:
    """All backends must emit the same 384-d contract (SafeVector(384))."""

    @pytest.mark.parametrize("backend", [HashEmbeddingBackend()])
    def test_hash_dim(self, backend):
        assert backend.dim == EMBEDDING_DIM
        assert len(backend.embed(["x"])[0]) == EMBEDDING_DIM

    def test_ollama_resizes_arbitrary_dim(self):
        backend = ExternalEmbeddingBackend(dim=EMBEDDING_DIM)
        short = backend._to_dim([1.0, 2.0, 3.0])
        assert len(short) == EMBEDDING_DIM
        long = backend._to_dim([1.0] * 1000)
        assert len(long) == EMBEDDING_DIM

    def test_onnx_default_dim(self, tmp_path):
        backend = OnnxEmbeddingBackend("Xenova/bge-small-en-v1.5", tmp_path)
        assert backend.dim == EMBEDDING_DIM

    def test_onnx_unavailable_without_cache_or_download(self, tmp_path):
        backend = OnnxEmbeddingBackend("Xenova/bge-small-en-v1.5", tmp_path, allow_download=False)
        # Nothing cached and downloads disallowed -> unavailable (offline-safe).
        assert backend.available() is False


class TestSettingsPersistence:
    def test_save_and_read_roundtrip(self, monkeypatch, tmp_path):
        from backend import config

        monkeypatch.setattr(config, "EMBEDDING_SETTINGS_FILE", tmp_path / "e.json")
        monkeypatch.setattr(config, "_ENV_EMBEDDING_BACKEND", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_MODEL", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_ALLOW_DOWNLOAD", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_SERVER_URL", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_SERVER_MODEL", None)
        config.save_embedding_settings(
            backend="standalone", model="BAAI/bge-small-en-v1.5",
            allow_download=True, server_url="http://localhost:1234/v1", server_model="nomic",
        )
        s = config.get_embedding_settings()
        assert s["backend"] == "standalone"
        assert s["model"] == "BAAI/bge-small-en-v1.5"
        assert s["allow_download"] is True
        assert s["server_url"] == "http://localhost:1234/v1"
        assert s["server_model"] == "nomic"

    def test_env_overrides_persisted(self, monkeypatch, tmp_path):
        from backend import config

        monkeypatch.setattr(config, "EMBEDDING_SETTINGS_FILE", tmp_path / "e.json")
        monkeypatch.setattr(config, "_ENV_EMBEDDING_MODEL", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_ALLOW_DOWNLOAD", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_SERVER_URL", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_SERVER_MODEL", None)
        config.save_embedding_settings(backend="hash", model="stored-model")
        monkeypatch.setattr(config, "_ENV_EMBEDDING_BACKEND", "external")
        monkeypatch.setattr(config, "_ENV_EMBEDDING_MODEL", "env-model")
        s = config.get_embedding_settings()
        assert s["backend"] == "external"
        assert s["model"] == "env-model"

    def test_invalid_backend_rejected(self, monkeypatch, tmp_path):
        from backend import config

        monkeypatch.setattr(config, "EMBEDDING_SETTINGS_FILE", tmp_path / "e.json")
        monkeypatch.setattr(config, "_ENV_EMBEDDING_BACKEND", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_MODEL", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_ALLOW_DOWNLOAD", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_SERVER_URL", None)
        monkeypatch.setattr(config, "_ENV_EMBEDDING_SERVER_MODEL", None)
        config.save_embedding_settings(backend="nonsense")
        assert config.get_embedding_settings()["backend"] == "auto"


class TestEmbeddingSettingsApi:
    def test_get_returns_effective_settings(self):
        res = client.get("/api/settings/embeddings")
        assert res.status_code == 200
        body = res.json()
        assert body["dim"] == EMBEDDING_DIM
        assert body["backend"] in ("auto", "standalone", "external", "ollama", "hash")
        assert body["active_backend"] in ("standalone", "external", "hash")

    def test_put_persists_selection(self):
        res = client.put(
            "/api/settings/embeddings",
            json={
                "backend": "hash",
                "model": "Xenova/bge-small-en-v1.5",
                "server_url": "http://localhost:1234/v1",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["backend"] == "hash"
        assert body["active_backend"] == "hash"
        assert body["server_url"] == "http://localhost:1234/v1"

    def test_put_rejects_unknown_backend(self):
        res = client.put("/api/settings/embeddings", json={"backend": "bogus"})
        assert res.status_code == 200
        # Invalid values are ignored; the persisted value stays valid.
        assert res.json()["backend"] in ("auto", "standalone", "external", "ollama", "hash")
