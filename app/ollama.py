"""Shared, serialized Ollama HTTP client.

Every Ollama call (LLM ``chat`` and ``embed``) is serialized through the single
module-level ``OLLAMA_LOCK``. The Ollama server handles **one request at a time**
(NFR-2), so the lock keeps a burst of concurrent WebUI/API requests from
overloading it — callers queue instead.

Transport contract (``httpx.post``):

    * returns a :class:`httpx.Response`;
    * raises ``httpx.HTTPError`` on a transport/timeout failure;
    * exposes ``.status_code`` and ``.json()`` for a 2xx body.

Anything that is not a 2xx response — or any transport error — is re-raised as
:class:`OllamaError` so callers keep a single, typed failure surface.
"""

from __future__ import annotations

import logging
import sys
import threading

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# --- shared serialization ------------------------------------------------- #
# One lock for both embed and LLM calls. The Ollama server processes a single
# request at a time, so all HTTP calls funnel through this one lock.
OLLAMA_LOCK: threading.Lock = threading.Lock()

# A single client is cheap to reuse and lets httpx keep connection pooling alive.
_HTTP = httpx.Client()


class OllamaError(RuntimeError):
    """Raised when an Ollama HTTP call fails (transport error or non-2xx)."""


class LLMError(RuntimeError):
    """Raised when a structured LLM response cannot be produced/decoded."""


def ollama_post(path: str, payload: dict) -> dict:
    """POST ``{base_url}{path}`` with ``payload`` and return the decoded body.

    The call is serialized under :data:`OLLAMA_LOCK` and must raise
    :class:`OllamaError` on any failure so upstream layers never see a raw
    ``httpx`` error.

    Args:
        path: Endpoint path, e.g. ``"/api/chat"`` or ``"/api/embed"``.
        payload: JSON body to send.

    Returns:
        The JSON body as a ``dict``.

    Raises:
        OllamaError: on a transport error or a non-2xx status code.
    """
    url = f"{settings.ollama.base_url}{path}"
    try:
        resp = _HTTP.post(url, json=payload, timeout=300)
    except httpx.HTTPError as exc:
        logger.error("Ollama request to %s failed: %s", url, exc)
        raise OllamaError(f"Ollama request to {path} failed: {exc}") from exc

    if not (200 <= resp.status_code < 300):
        logger.error(
            "Ollama %s returned %s: %s",
            path,
            resp.status_code,
            resp.text[:500],
        )
        raise OllamaError(
            f"Ollama request to {path} failed with status {resp.status_code}"
        )

    return resp.json()


def chat(messages: list[dict]) -> str:
    """Plain completion: ``POST /api/chat`` without structured output.

    Args:
        messages: OpenAI-style ``[{"role", "content"}, ...]`` message list.

    Returns:
        The assistant's response text.

    Raises:
        OllamaError: on any failure talking to Ollama.
        LLMError: if the model returns no usable content string.
    """
    payload = {
        "model": settings.ollama.llm_model,
        "messages": messages,
        "stream": False,
    }
    body = ollama_post("/api/chat", payload)
    content = body.get("message", {}).get("content")
    if not isinstance(content, str) or not content:
        logger.error(
            "Ollama response missing/empty content: %s",
            content,
        )
        raise LLMError("Ollama response had no usable content")
    return content


def chat_structured(messages: list[dict], schema: dict) -> str:
    """Structured completion: ``POST /api/chat`` with ``format=<json schema>``.

    Returns the **raw JSON string** the model emitted — the caller parses and
    validates it.

    Args:
        messages: OpenAI-style ``[{"role", "content"}, ...]`` message list.
        schema: JSON schema for the structured output, sent verbatim as the
            ``format`` field.

    Returns:
        The raw JSON string from ``response.message.content``.

    Raises:
        OllamaError: on any failure talking to Ollama.
        LLMError: if the model returns no usable content string.
    """
    payload = {
        "model": settings.ollama.llm_model,
        "messages": messages,
        "format": schema,
        "stream": False,
    }
    body = ollama_post("/api/chat", payload)
    content = body.get("message", {}).get("content")
    if not isinstance(content, str) or not content:
        logger.error(
            "Ollama structured response missing/empty content: %s",
            content,
        )
        raise LLMError("Ollama structured response had no usable content")
    return content


# Surface unexpected errors clearly in the CLI (e.g. ``scripts/``), where a raw
# traceback is confusing. Web layers translate these into envelopes instead.
if __name__ == "__main__":  # pragma: no cover
    try:
        # Exercise the client end-to-end against a running Ollama, if present.
        print(chat([{"role": "user", "content": "ping"}]))
    except (OllamaError, LLMError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
