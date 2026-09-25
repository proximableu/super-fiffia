"""Acceptance tests for hybrid retrieval over ``rag_chunks``.

``CONTRACT.md`` 8, ``AGENT.md`` T2.3. The fixture applies the migration and opens
a connection to the dedicated test database, so ``retrieve_rag`` exercises the
real ``rag_chunks`` schema: the ``vector(768)`` column, the GIN ``fts`` index and
the RRF ranking SQL.

No live Ollama is needed: ``app.retrieval.embed`` is faked so the vector leg
runs, and a second patch makes it raise :class:`EmbeddingError` so the lexical
fallback path is exercised.

``rag_chunks`` carries no metadata scope (``CONTRACT.md`` §8), so a query is
matched purely on content. The assertions therefore pin the *ranking* — the
chunk that actually mentions the query term must come back first and beat the
unrelated one — rather than exclusivity, which RRF's non-matching fallback rank
would otherwise break.
"""

from __future__ import annotations

import math
import re
from unittest.mock import patch

import psycopg
import pytest

from app.embedding import EmbeddingError, EMBED_DIM
from app.retrieval import retrieve_rag

# A 768-dimensional fake embedding so seeded chunks fit the ``embedding`` column
# without a live Ollama and so the corpus embeds consistently.
_EMBEDDING = [0.1] * EMBED_DIM

# A deterministic bag-of-words embedding over a small keyword vocabulary, so the
# fake embedder produces a query vector that is genuinely more similar to the
# chunks whose content shares vocabulary. Without it the vector leg would rank
# *all* seeded chunks equally (there is no scope to filter rag_chunks) and RRF
# would let a non-matching chunk score above a matching one — this keeps the
# semantic leg discriminative.
_VOCAB = ("pump", "pressure", "seal", "valve", "thermodynamics", "sensor", "calibrate")


def _bag_of_words_vector(text: str) -> list[float]:
    """Return a bag-of-words TF vector for ``text`` over a small keyword vocab."""
    vector = [0.0] * EMBED_DIM
    for token in re.findall(r"[a-zA-Z]+", text.lower()):
        if token in _VOCAB:
            vector[_VOCAB.index(token)] += 1.0
    return vector


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def fake_embed(texts):  # noqa: ANN001 - matches embed() signature
    """Return a valid ``(n, EMBED_DIM)`` matrix, one vector per input.

    Mirrors ``app.embedding.embed``'s contract (one embedding per input, same
    order) so the vector leg of retrieval runs without a live Ollama.
    """
    return [_bag_of_words_vector(t) for t in texts]


def boom(texts):  # noqa: ANN001 - matches embed() signature
    """Raise :class:`EmbeddingError` so retrieve_rag falls back to the lexical leg."""
    raise EmbeddingError("ollama down")


def _truncate(db_conn: psycopg.Connection) -> None:
    """TRUNCATE ``rag_chunks`` on a separate autocommit connection.

    ``conftest``'s autouse ``reset_db`` clears ``records`` but not ``rag_chunks``,
    so this test truncates it before seeding. The unique index on
    ``(source_file, content_hash)`` would otherwise collide with rows left by an
    earlier run of this suite.
    """
    with psycopg.connect(db_conn.info.dsn, autocommit=True) as c:
        c.execute("TRUNCATE rag_chunks")
    db_conn.rollback()


def _seed(conn: psycopg.Connection, chunk_text: str, source_file: str, section_header=None) -> None:
    """Insert a single distinct chunk into ``rag_chunks`` and commit.

    Each chunk gets a unique ``content_hash`` (see the module counter) so it never
    collides with the dedup unique index across repeated runs of this suite. The
    stored embedding is the same bag-of-words scheme used by the fake embedder, so
    the seeded corpus and the query embeds sit on the same scale.
    """
    conn.execute(
        "INSERT INTO rag_chunks "
        "(source_file, chunk_index, section_header, chunk_text, content_hash, embedding) "
        "VALUES (%s, %s, %s, %s, %s, %s::vector)",
        (
            source_file,
            0,
            section_header,
            chunk_text,
            _content_hash(source_file, chunk_text),
            "[" + ",".join(str(v) for v in _bag_of_words_vector(chunk_text)) + "]",
        ),
    )
    conn.commit()


# Monotonic counter so every seeded chunk gets a distinct ``content_hash``. The
# dedup unique index is on ``(source_file, content_hash)``; without per-chunk
# uniqueness a second _seed() call in the same test would raise UniqueViolation
# before retrieval even runs.
_HASH_SEQ = 0


def _content_hash(source_file: str, chunk_text: str) -> str:
    """A stable per-chunk digest for the ``(source_file, chunk_text)`` unique index."""
    import hashlib

    return hashlib.sha256(
        f"{source_file}\x00{chunk_text}".encode()
    ).hexdigest()


@pytest.fixture
def seeded(db_conn: psycopg.Connection) -> None:
    """Three chunks: two share the pump vocabulary, one does not.

    The two pump chunks rank above the sensor chunk for a "pump" query via both
    the semantic and lexical legs; the sensor chunk is only reachable through the
    RRF fallback and should never outrank a genuine match.
    """
    _truncate(db_conn)
    _seed(
        db_conn,
        chunk_text="The pump loses pressure when the seal fails.",
        source_file="pump_manual.md",
        section_header="# Pump",
    )
    _seed(
        db_conn,
        chunk_text="Replace the failing seal on the hydraulic pump valve.",
        source_file="pump_manual.md",
        section_header="# Seal",
    )
    _seed(
        db_conn,
        chunk_text="Calibrate the thermodynamics sensor before startup.",
        source_file="sensor_manual.md",
        section_header="# Sensor",
    )


# --------------------------------------------------------------------------- #
# retrieve_rag
# --------------------------------------------------------------------------- #


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_rag_returns_rag_hits(seeded):
    hits = retrieve_rag("pump loses pressure", top_k=10)

    assert hits
    assert all(h.source == "rag" for h in hits)
    # the matching pump chunk ranks first, with its field carriers populated
    assert hits[0].source_file == "pump_manual.md"
    assert hits[0].chunk_text == "The pump loses pressure when the seal fails."
    assert all(h.score > 0.0 for h in hits)


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_rag_ranks_matches_above_non_matches(seeded):
    # "pump" matches the two pump_manual chunks on both legs; they must rank
    # above the unrelated thermodynamics sensor chunk, which is reachable only
    # through RRF's fallback rank.
    hits = retrieve_rag("pump", top_k=10)

    assert hits[0].source_file == "pump_manual.md"
    pump_pos = min(i for i, h in enumerate(hits) if h.source_file == "pump_manual.md")
    sensor_pos = min(i for i, h in enumerate(hits) if h.source_file == "sensor_manual.md")
    assert pump_pos < sensor_pos


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_rag_section_header_carrier(seeded):
    hits = retrieve_rag("seal", top_k=10)
    assert any(h.section_header == "# Seal" for h in hits)


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_rag_respects_top_k_limit(db_conn, seeded):
    _seed(db_conn, "A second pump pressure note.", "pump_manual.md")
    _seed(db_conn, "A third pump pressure note.", "pump_manual.md")
    hits = retrieve_rag("pump", top_k=2)
    assert len(hits) == 2


@patch("app.retrieval.embed", boom)
def test_retrieve_rag_falls_back_to_lexical_on_embedding_failure(seeded):
    # the fallback still returns the lexically matching chunks ranked first
    hits = retrieve_rag("pump", top_k=10)

    assert hits
    assert all(h.source == "rag" for h in hits)
    assert hits[0].source_file == "pump_manual.md"
    assert hits[0].score > 0.0
