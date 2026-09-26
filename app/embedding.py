"""Text embedding via Ollama's ``/api/embed`` endpoint.

Embeddings populate ``records.embedding`` (the embedding of the *failure
description only*) and ``rag_chunks.embedding``. Every call is serialized through
:data:`app.ollama.OLLAMA_LOCK` — the Ollama server handles one request at a
time (NFR-2), so a burst of submissions must not overload it.

``EMBED_MODEL`` / ``EMBED_DIM`` are exposed from here so callers (repo, RAG) keep
a single source of truth. ``EMBED_DIM`` matches the model's output width and the
``vector(1024)`` columns in ``migrations/0001_init.sql``; it is a property of the
model, not a per-deployment setting.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.ollama import OLLAMA_LOCK, ollama_post

logger = logging.getLogger(__name__)

# The embedding model (from settings) and its fixed output width. EMBED_DIM is a
# property of the model and must match the vector(...) columns in the schema.
EMBED_MODEL: str = settings.ollama.embed_model
EMBED_DIM: int = 1024


class EmbeddingError(RuntimeError):
    """Raised when the Ollama ``/api/embed`` call fails."""


def embed(texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` in order via Ollama ``/api/embed``.

    Args:
        texts: One or more strings to embed; the result preserves the input
            order.

    Returns:
        A ``(n, EMBED_DIM)`` matrix — one embedding vector per input text.

    Raises:
        EmbeddingError: on any failure talking to Ollama.
    """
    with OLLAMA_LOCK:
        payload = {"model": EMBED_MODEL, "input": texts}
        try:
            body = ollama_post("/api/embed", payload)
        except Exception as exc:  # noqa: BLE001 - re-raised with domain type
            raise EmbeddingError(f"embedding failed: {exc}") from exc

    embeddings = body.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        logger.error(
            "Ollama /api/embed returned %d embeddings for %d inputs",
            len(embeddings) if isinstance(embeddings, list) else 0,
            len(texts),
        )
        raise EmbeddingError("Ollama /api/embed returned an unexpected shape")

    # Defensive: verify the width matches EMBED_DIM. Mismatched width indicates a
    # model/version problem the caller must not silently swallow.
    for vec in embeddings:
        if len(vec) != EMBED_DIM:
            raise EmbeddingError(
                f"embedding width {len(vec)} != expected {EMBED_DIM}"
            )

    return embeddings
