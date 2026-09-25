"""Tests for ``app/embedding.py`` — the Ollama embedding wrapper.

The HTTP layer is mocked by patching ``app.ollama._HTTP.post`` so no real
Ollama is needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from app import ollama
from app.embedding import EmbeddingError, embed, EMBED_DIM


def _make_resp(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = ""
    resp.json.return_value = body
    return resp


def test_embed_returns_matrix_of_correct_shape() -> None:
    texts = ["a", "b", "c"]
    vectors = [[0.1] * EMBED_DIM for _ in texts]
    ollama._HTTP.post = lambda url, json, timeout: _make_resp({"embeddings": vectors})

    out = embed(texts)
    assert len(out) == 3
    assert all(len(row) == EMBED_DIM for row in out)
    # one embedding per input, in the same order
    assert out[0] == vectors[0]


def test_embed_single_text_shape() -> None:
    ollama._HTTP.post = lambda url, json, timeout: _make_resp(
        {"embeddings": [[0.5] * EMBED_DIM]}
    )
    out = embed(["solo"])
    assert out == [[0.5] * EMBED_DIM]


def test_embed_raises_embedding_error_on_500() -> None:
    def five_hundred(*args, **kwargs):
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "boom"
        resp.json.return_value = {}
        return resp

    ollama._HTTP.post = five_hundred
    with pytest.raises(EmbeddingError, match="failed"):
        embed(["a"])


def test_embed_raises_on_transport_error() -> None:
    def raising(*args, **kwargs):
        raise httpx.ConnectTimeout("unreachable")

    ollama._HTTP.post = raising
    with pytest.raises(EmbeddingError, match="embedding failed"):
        embed(["a"])


def test_embed_raises_on_shape_mismatch() -> None:
    ollama._HTTP.post = lambda url, json, timeout: _make_resp(
        {"embeddings": [[0.1] * 3]}  # wrong width
    )
    with pytest.raises(EmbeddingError, match="width"):
        embed(["a"])


def test_embed_raises_when_count_mismatch() -> None:
    ollama._HTTP.post = lambda url, json, timeout: _make_resp({"embeddings": []})
    with pytest.raises(EmbeddingError, match="shape"):
        embed(["a"])
