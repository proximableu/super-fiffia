"""Tests for ``app/ollama.py`` — the shared, serialized Ollama client.

The HTTP layer is mocked: ``app.ollama._HTTP`` (the module-level httpx client)
has its ``.post`` replaced with a fake, so no real Ollama is needed.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import httpx
import pytest

from app import ollama
from app.ollama import (
    LLMError,
    OllamaError,
    chat,
    chat_structured,
    ollama_post,
)
from app.embedding import embed as embed_fn


def _make_resp(status_code: int = 200, body: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = body if body is not None else {}
    return resp


# --------------------------------------------------------------------------- #
# ollama_post
# --------------------------------------------------------------------------- #
def test_ollama_post_returns_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, json: dict, timeout: float) -> MagicMock:
        assert url.endswith("/api/chat")
        assert json["stream"] is False
        return _make_resp(200, {"message": {"content": "hi"}})

    monkeypatch.setattr(ollama._HTTP, "post", fake_post)
    body = ollama_post("/api/chat", {"stream": False})
    assert body == {"message": {"content": "hi"}}


def test_ollama_post_raises_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama._HTTP, "post", lambda *a, **k: _make_resp(500, text="boom"))
    with pytest.raises(OllamaError, match="status 500"):
        ollama_post("/api/chat", {})


def test_ollama_post_wraps_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_post(*args, **kwargs):
        raise httpx.ConnectTimeout("unreachable")

    monkeypatch.setattr(ollama._HTTP, "post", raising_post)
    with pytest.raises(OllamaError, match="failed"):
        ollama_post("/api/chat", {})


# --------------------------------------------------------------------------- #
# chat / chat_structured
# --------------------------------------------------------------------------- #
def test_chat_returns_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ollama._HTTP,
        "post",
        lambda *a, **k: _make_resp(200, {"message": {"content": "the answer"}}),
    )
    out = chat([{"role": "user", "content": "q"}])
    assert out == "the answer"


def test_chat_sends_model_and_stream_false(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_post(url: str, json: dict, timeout: float) -> MagicMock:
        seen.update(json)
        return _make_resp(200, {"message": {"content": "x"}})

    monkeypatch.setattr(ollama._HTTP, "post", fake_post)
    chat([{"role": "user", "content": "q"}])
    assert seen["stream"] is False
    assert seen["model"] == ollama.settings.ollama.llm_model


def test_chat_structured_returns_raw_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ollama._HTTP,
        "post",
        lambda *a, **k: _make_resp(200, {"message": {"content": '{"thought": "x"}'}}),
    )
    raw = chat_structured([{"role": "user", "content": "q"}], {"type": "object"})
    assert raw == '{"thought": "x"}'


def test_chat_structured_sends_json_schema_format(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = {"type": "object", "properties": {}}
    seen: dict = {}

    def fake_post(url: str, json: dict, timeout: float) -> MagicMock:
        seen.update(json)
        return _make_resp(200, {"message": {"content": "{}"}})

    monkeypatch.setattr(ollama._HTTP, "post", fake_post)
    chat_structured([{"role": "user", "content": "q"}], schema)
    assert seen["format"] == schema
    assert seen["stream"] is False


def test_chat_structured_empty_content_raises_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama._HTTP, "post", lambda *a, **k: _make_resp(200, {"message": {}}))
    with pytest.raises(LLMError, match="no usable content"):
        chat_structured([{"role": "user", "content": "q"}], {"type": "object"})


# --------------------------------------------------------------------------- #
# serialization (NFR-2) — two concurrent embed() calls never overlap
# --------------------------------------------------------------------------- #
def test_lock_serializes_concurrent_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    peak = 0
    guard = threading.Lock()

    def fake_post(url: str, json: dict, timeout: float) -> MagicMock:
        nonlocal active, peak
        # Observe overlap: if the lock were broken, two calls would be 'active'
        # simultaneously while they sleep. Under the lock only one is active.
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        n = len(json["input"])
        return _make_resp(200, {"embeddings": [[0.1] * 768 for _ in range(n)]})

    monkeypatch.setattr(ollama._HTTP, "post", fake_post)

    def worker() -> None:
        embed_fn(["a", "b"])

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak == 1, f"Ollama calls overlapped (peak concurrency {peak}); expected 1"
