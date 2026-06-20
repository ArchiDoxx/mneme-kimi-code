"""Tests for the tiered embedding backends (hash | local | api).

``_encode_texts`` dispatches on ``config.vector.embedding_backend`` and always
falls back to the dependency-free hash backend so semantic search never errors,
even when sentence-transformers or the API endpoint are unavailable.
"""

from __future__ import annotations

import json

import numpy as np

import mneme.db.vector as vec


def _cfg(**vector_overrides: object) -> dict:
    vector = {
        "embedding_backend": "hash",
        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
        "embedding_dim": 384,
    }
    vector.update(vector_overrides)
    return {"vector": vector}


def test_hash_backend_is_deterministic_and_normalized(monkeypatch):
    monkeypatch.setattr(vec, "load_config", lambda: _cfg(embedding_backend="hash"))

    a = vec._encode_texts(["hello", "world"])
    b = vec._encode_texts(["hello", "world"])

    assert a.shape == (2, 384)
    assert np.allclose(a, b)  # deterministic
    assert np.allclose(np.linalg.norm(a, axis=1), 1.0, atol=1e-5)  # unit vectors


def test_hash_backend_respects_configured_dim(monkeypatch):
    monkeypatch.setattr(vec, "load_config", lambda: _cfg(embedding_dim=128))
    assert vec._encode_texts(["x"]).shape == (1, 128)


def test_api_backend_parses_and_normalizes(monkeypatch):
    monkeypatch.setattr(
        vec,
        "load_config",
        lambda: _cfg(
            embedding_backend="api",
            embedding_api_base="https://fake/v1",
            embedding_api_model="mxbai",
        ),
    )

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"data": [{"embedding": [3.0, 4.0]}]}).encode("utf-8")

    monkeypatch.setattr(vec.urllib.request, "urlopen", lambda *a, **k: _FakeResp())

    out = vec._encode_texts(["x"])
    assert out.shape == (1, 2)
    # [3, 4] normalized -> [0.6, 0.8]
    assert np.allclose(out[0], [0.6, 0.8], atol=1e-6)


def test_api_backend_falls_back_to_hash_on_error(monkeypatch):
    monkeypatch.setattr(
        vec,
        "load_config",
        lambda: _cfg(
            embedding_backend="api",
            embedding_api_base="https://fake/v1",
            embedding_api_model="mxbai",
            embedding_dim=384,
        ),
    )

    def _boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(vec.urllib.request, "urlopen", _boom)
    assert vec._encode_texts(["x"]).shape == (1, 384)  # hash fallback


def test_api_backend_missing_config_falls_back(monkeypatch):
    # backend=api but no base/model configured -> hash fallback, no crash.
    monkeypatch.setattr(vec, "load_config", lambda: _cfg(embedding_backend="api"))
    assert vec._encode_texts(["x"]).shape == (1, 384)


def test_local_backend_falls_back_when_sentence_transformers_missing(monkeypatch):
    monkeypatch.setattr(vec, "load_config", lambda: _cfg(embedding_backend="local"))
    cache = vec._EmbeddingCache()
    monkeypatch.setattr(cache, "_has_sentence_transformers", False)
    monkeypatch.setattr(cache, "_model", None)
    assert vec._encode_texts(["x"]).shape == (1, 384)  # hash fallback
