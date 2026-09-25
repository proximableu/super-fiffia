"""Acceptance tests for scoped structured-first hybrid retrieval.

``CONTRACT.md`` 8, ``AGENT.md`` T2.1. The fixture applies the migration and
opens a connection to the dedicated test database, so ``retrieve_fs`` exercises
the real ``records`` schema: the ``vector(768)`` columns, the GIN ``fts`` index,
the ``status='active'`` filter and the RRF ranking SQL.

No live Ollama is needed: ``app.retrieval.embed`` is faked so the vector leg
runs, and a second patch makes it raise :class:`EmbeddingError` so the lexical
fallback path is exercised.
"""

from __future__ import annotations

from unittest.mock import patch

import psycopg
import pytest

from app.embedding import EmbeddingError, EMBED_DIM, EMBED_MODEL
from app.records_repo import Scope
from app.retrieval import retrieve_fs

# A 768-dimensional fake embedding so seeded rows fit the ``embedding`` column
# without a live Ollama and so the corpus embeds consistently.
_EMBEDDING = [0.1] * EMBED_DIM

# Monotonic counter so every seeded row gets a distinct ``content_hash``. The
# dedup unique index is on ``content_hash WHERE status='active'``; without
# per-row uniqueness a second _seed() call in the same test would raise
# UniqueViolation before retrieval even runs.
_HASH_SEQ = 0


def fake_embed(texts):  # noqa: ANN001 - matches embed() signature
    """Return a valid ``(n, EMBED_DIM)`` matrix, one vector per input.

    Mirrors ``app.embedding.embed``'s contract (one embedding per input, same
    order) so the vector leg of retrieval runs without a live Ollama.
    """
    return [[0.5] * EMBED_DIM for _ in texts]


def boom(texts):  # noqa: ANN001 - matches embed() signature
    """Raise :class:`EmbeddingError` so retrieve_fs falls back to the lexical leg."""
    raise EmbeddingError("ollama down")


def _seed(conn: psycopg.Connection, scope: Scope) -> None:
    """Insert a single distinct active row under *scope* and commit.

    Each row gets a unique ``content_hash`` (see the module counter) so it never
    collides with the dedup unique index across repeated runs of this suite.
    """
    global _HASH_SEQ
    _HASH_SEQ += 1
    content_hash = f"{_HASH_SEQ:032x}"
    conn.execute(
        "INSERT INTO records "
        "(category, product, article_number, failure_description, "
        " solution_description, content_hash, source, status, embedding, "
        " embed_model, embed_dim, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s::vector, %s, %s, %s)",
        (
            scope.category,
            scope.product,
            scope.article_number,
            "Pump loses pressure",
            "Replace the failing pump seal",
            content_hash,
            "manual",
            "[" + ",".join(str(v) for v in _EMBEDDING) + "]",
            EMBED_MODEL,
            EMBED_DIM,
            "tester",
        ),
    )
    conn.commit()


@pytest.fixture
def seeded(db_conn: psycopg.Connection) -> Scope:
    """Hydraulics + a pneumatics row. Returns the hydraulics scope."""
    _seed(
        db_conn,
        Scope(category="hydraulics", product="valve_b", article_number="200-010"),
    )
    _seed(
        db_conn,
        Scope(category="pneumatics", product="cyl_c", article_number="300-020"),
    )
    return Scope(category="hydraulics", product="valve_b")


# --------------------------------------------------------------------------- #
# retrieve_fs
# --------------------------------------------------------------------------- #


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_fs_returns_active_rows_in_scope(seeded):
    hits = retrieve_fs(seeded, "pump loses pressure", top_k=10)

    assert hits
    assert all(h.source == "records" for h in hits)
    # only the hydraulics row is in scope
    assert {h.category for h in hits} == {"hydraulics"}
    assert all(h.product == "valve_b" for h in hits)
    # the expected field carries are populated
    assert hits[0].failure_description == "Pump loses pressure"


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_fs_ranks_by_semantic_and_lexical_similarity(seeded):
    # "pump" matches only the hydraulics row's content; it must come back ranked.
    hits = retrieve_fs(seeded, "pump", top_k=10)

    assert len(hits) == 1
    assert hits[0].failure_description == "Pump loses pressure"
    assert all(h.score > 0.0 for h in hits)


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_fs_empty_scope_returns_empty(db_conn):
    # a category with no seeded data at all -> no rows regardless of query
    hits = retrieve_fs(Scope(category="thermodynamics", product="pump"), "pump", top_k=10)
    assert hits == []


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_fs_excludes_archived_rows(db_conn):
    _seed(db_conn, Scope(category="hydraulics", product="valve_b"))
    hits = retrieve_fs(Scope(category="hydraulics", product="valve_b"), "pump", top_k=10)
    assert len(hits) == 1

    # archive the row: it must drop out of every subsequent retrieval
    db_conn.execute(
        "UPDATE records SET status='archived' "
        "WHERE category='hydraulics' AND product='valve_b'"
    )
    db_conn.commit()

    hits = retrieve_fs(Scope(category="hydraulics", product="valve_b"), "pump", top_k=10)
    assert hits == []


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_fs_article_number_scopes(seeded):
    hits = retrieve_fs(
        Scope(category="hydraulics", product="valve_b", article_number="200-010"),
        "pump",
        top_k=10,
    )
    assert hits and hits[0].article_number == "200-010"

    # wrong article number for that category -> no rows
    hits = retrieve_fs(
        Scope(category="hydraulics", product="valve_b", article_number="999-999"),
        "pump",
        top_k=10,
    )
    assert hits == []


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_fs_uses_top_k_limit(db_conn):
    # seed a handful of rows that all match a broad term
    for art in ("200-010", "200-011", "200-012", "200-013"):
        _seed(
            db_conn,
            Scope(category="hydraulics", product="valve_b", article_number=art),
        )
    hits = retrieve_fs(Scope(category="hydraulics", product="valve_b"), "pump", top_k=2)
    assert len(hits) == 2


@patch("app.retrieval.embed", boom)
def test_retrieve_fs_falls_back_to_lexical_on_embedding_failure(seeded):
    # the fallback still returns the lexically matching active row
    hits = retrieve_fs(seeded, "pump", top_k=10)

    assert hits
    assert all(h.source == "records" for h in hits)
    assert hits[0].failure_description == "Pump loses pressure"


@patch("app.retrieval.embed", fake_embed)
def test_retrieve_fs_scope_by_category_only(seeded):
    hits = retrieve_fs(Scope(category="hydraulics"), "pump", top_k=10)
    assert hits and hits[0].category == "hydraulics"
