"""Acceptance tests for hybrid retrieval over ``rag_chunks``.

``CONTRACT.md`` 8, ``AGENT.md`` T2.3. The fixture applies the migration and opens
a connection to the dedicated test database, so ``retrieve_rag`` exercises the
real ``rag_chunks`` schema: the ``vector(1024)`` column, the GIN ``fts`` index and
the RRF ranking SQL.

No live Ollama is needed: ``app.retrieval.embed`` is faked so the vector leg
runs, and a second patch makes it raise :class:`EmbeddingError` so the lexical
fallback path is exercised.

``rag_chunks`` may carry an optional category/product scope (added in migration
``0004_rag_chunk_scope.sql``), applied by :func:`retrieve_rag` as an optional
``WHERE`` on both the vector and lexical legs — the same structured filtering
:func:`retrieve_fs` applies to records. When the caller passes an empty scope the
query is matched purely on content (unchanged behavior); when it passes a scope,
only chunks tagged for that category or product return. The assertions therefore
pin both the *ranking* and the scoping.
"""

from __future__ import annotations

import math
import re
from unittest.mock import patch

import psycopg
import pytest

from app.embedding import EmbeddingError, EMBED_DIM
from app.retrieval import retrieve_rag

# A 1024-dimensional fake embedding so seeded chunks fit the ``embedding`` column
# without a live Ollama and so the corpus embeds consistently.
_EMBEDDING = [0.1] * EMBED_DIM

# A deterministic bag-of-words embedding over a small keyword vocabulary, so the
# fake embedder produces a query vector that is genuinely more similar to the
# chunks whose content shares vocabulary. Without it the vector leg would rank
# *all* seeded chunks equally and RRF would let a non-matching chunk score above
# a matching one — this keeps the semantic leg discriminative.
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


def _seed(
    conn: psycopg.Connection,
    chunk_text: str,
    source_file: str,
    section_header: str | None = None,
    category: str | None = None,
    product: str | None = None,
) -> None:
    """Insert a single distinct chunk into ``rag_chunks`` and commit.

    Each chunk gets a unique ``content_hash`` (see the module counter) so it never
    collides with the dedup unique index across repeated runs of this suite. The
    stored embedding is the same bag-of-words scheme used by the fake embedder, so
    the seeded corpus and the query embeds sit on the same scale. The optional
    ``category``/``product`` tag the chunk so retrieval scope tests exercise the
    new scoped columns.
    """
    conn.execute(
        "INSERT INTO rag_chunks "
        "(source_file, chunk_index, section_header, chunk_text, content_hash, "
        "embedding, category, product) "
        "VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s)",
        (
            source_file,
            0,
            section_header,
            chunk_text,
            _content_hash(source_file, chunk_text),
            "[" + ",".join(str(v) for v in _bag_of_words_vector(chunk_text)) + "]",
            category,
            product,
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
        category="pump",
        product="pump",
    )
    _seed(
        db_conn,
        chunk_text="Calibrate the thermodynamics sensor before startup.",
        source_file="sensor_manual.md",
        section_header="# Sensor",
        category="sensor",
        product="sensor",
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


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_rag_scope_filters_by_product(seeded):
    """A scope for ``product='pump'`` returns only pump chunks (matches the head
    question's product); the sensor chunk is excluded."""
    from app.records_repo import Scope

    hits = retrieve_rag("pump", top_k=10, scope=Scope(category=None, product="pump"))
    assert all(h.source == "rag" for h in hits)
    assert all(h.source_file == "pump_manual.md" for h in hits)
    assert all(h.product == "pump" for h in hits)


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_rag_scope_by_category_or_product(seeded):
    """OR semantics: a scope with a non-null product matches chunks tagged with
    that product (``category`` NULL)."""
    from app.records_repo import Scope

    hits = retrieve_rag("seal", top_k=10, scope=Scope(category=None, product="pump"))
    # the # Seal pump chunk lexically matches "seal" and is in-scope
    assert any(h.source_file == "pump_manual.md" for h in hits)
    # the sensor chunk is out of scope for product='pump'
    assert all(h.source_file != "sensor_manual.md" for h in hits)


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_rag_empty_scope_returns_everything(seeded):
    """An empty scope applies no category/product filter — unchanged behavior;
    both pump and sensor chunks are reachable."""
    from app.records_repo import Scope

    hits = retrieve_rag("pump", top_k=10, scope=Scope())
    sources = {h.source_file for h in hits}
    assert "pump_manual.md" in sources
    assert "sensor_manual.md" in sources


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_rag_empty_scope_preserves_untagged_chunks(db_conn):
    """Untagged chunks remain reachable when the caller does NOT scope (empty
    scope). This pins the bug-report concern: a chunk ingested before any scope
    was ever supported must not silently disappear from results. Use a fresh
    table (``db_conn``) so the untagged row does not collide with tagged
    fixture rows."""
    from app.records_repo import Scope

    _truncate(db_conn)
    _seed(
        db_conn,
        "Calibrate the thermodynamics sensor before startup.",
        "sensor_manual.md",
        "# Sensor",
        category=None,  # explicitly untagged
        product=None,
    )
    _seed(
        db_conn,
        "The pump loses pressure when the seal fails.",
        "pump_manual.md",
        "# Pump",
        category="pump",
        product="pump",
    )
    # Empty scope: both the untagged and tagged chunks are returned.
    hits = retrieve_rag("pump", top_k=10, scope=Scope())
    sources = {h.source_file for h in hits}
    assert "sensor_manual.md" in sources
    assert "pump_manual.md" in sources

