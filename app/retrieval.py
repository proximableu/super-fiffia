"""Structured-first hybrid retrieval over ``records`` (``CONTRACT.md`` §8, ``AGENT.md`` T2.1).

``retrieve_fs`` returns ranked hits for a failure query against the *active*
records by combining two legs with reciprocal rank fusion:

* a **vector** leg — HNSW cosine proximity to the query embedding, and
* a **lexical** leg — GIN full-text match over ``fts``.

Scoping is *structured-first*: the caller's :class:`~app.records_repo.Scope`
(category + product + optional article_number) is applied as a structured
``WHERE`` on **both** legs. Any scope matching no active rows yields ``[]``.

The query embedding is the embedding of the query *text* (the corpus embeds the
``failure_description``, so a record ranks highest when the query is
semantically close to it). If the query embedding call fails, retrieval falls
back to the lexical leg alone and logs a warning — a search must never fail
solely because the embedding server is down.
"""

from __future__ import annotations

import logging

import psycopg

from app.config import settings
from app.db import _checkout, _release
from app.embedding import EmbeddingError, embed
from app.ollama import OLLAMA_LOCK
from app.records_repo import Hit, Scope, _as_vector

logger = logging.getLogger(__name__)

# Records RRF query (``CONTRACT.md`` §8). Both legs scope by ``status='active'``
# and the caller's scope via optional ``%(category)s`` / ``%(product)s`` /
# ``%(article)s`` filters, then combine their per-leg ranks with reciprocal rank
# fusion (``rrf_k``) over the shared ``records`` id.
_RECORDS_RRF_SQL = """
WITH vec AS (
    SELECT id, failure_description, solution_description,
           category, product, article_number, ncr, bug_record_number,
           ROW_NUMBER() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rank
    FROM records
    WHERE status = 'active'
      AND (%(category)s::text IS NULL OR category       = %(category)s::text)
      AND (%(product)s::text  IS NULL OR product        = %(product)s::text)
      AND (%(article)s::text  IS NULL OR article_number = %(article)s::text)
    ORDER BY embedding <=> %(qvec)s::vector
    LIMIT %(pool)s
),
txt AS (
    SELECT id, failure_description, solution_description,
           category, product, article_number, ncr, bug_record_number,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank(fts, plainto_tsquery('simple', %(q)s)) DESC
           ) AS rank
    FROM records
    WHERE status = 'active'
      AND fts @@ plainto_tsquery('simple', %(q)s)
      AND (%(category)s::text IS NULL OR category       = %(category)s::text)
      AND (%(product)s::text  IS NULL OR product        = %(product)s::text)
      AND (%(article)s::text  IS NULL OR article_number = %(article)s::text)
    LIMIT %(pool)s
)
SELECT
    COALESCE(v.id, t.id)                                     AS id,
    COALESCE(v.failure_description,  t.failure_description)  AS failure_description,
    COALESCE(v.solution_description, t.solution_description) AS solution_description,
    COALESCE(v.category, t.category)                         AS category,
    COALESCE(v.product, t.product)                           AS product,
    COALESCE(v.article_number, t.article_number)             AS article_number,
    COALESCE(v.ncr, t.ncr)                                   AS ncr,
    COALESCE(v.bug_record_number, t.bug_record_number)       AS bug_record_number,
    (COALESCE(1.0/(%(rrf_k)s + v.rank), 0.0)
     + COALESCE(1.0/(%(rrf_k)s + t.rank), 0.0)) AS rrf_score
FROM vec v
FULL OUTER JOIN txt t ON v.id = t.id
ORDER BY rrf_score DESC
LIMIT %(topk)s;
"""

# Lexical-only leg, used when the query embedding cannot be produced. The vector
# leg is dropped; rows are ranked by their lexical-leg rank alone.
_RECORDS_LEXICAL_SQL = """
SELECT id, failure_description, solution_description,
       category, product, article_number, ncr, bug_record_number,
       1.0/(%(rrf_k)s + t.rank) AS rrf_score
FROM (
    SELECT id, failure_description, solution_description,
           category, product, article_number, ncr, bug_record_number,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank(fts, plainto_tsquery('simple', %(q)s)) DESC
           ) AS rank
    FROM records
    WHERE status = 'active'
      AND fts @@ plainto_tsquery('simple', %(q)s)
      AND (%(category)s::text IS NULL OR category       = %(category)s::text)
      AND (%(product)s::text  IS NULL OR product        = %(product)s::text)
      AND (%(article)s::text  IS NULL OR article_number = %(article)s::text)
    LIMIT %(pool)s
) AS t
ORDER BY rrf_score DESC
LIMIT %(topk)s;
"""


def _score_row(row: dict) -> Hit:
    """Map a single records RRF SELECT row onto a records :class:`Hit`."""
    return Hit(
        id=row["id"],
        source="records",
        score=row["rrf_score"],
        category=row["category"],
        product=row["product"],
        article_number=row["article_number"],
        failure_description=row["failure_description"],
        solution_description=row["solution_description"],
        ncr=row["ncr"],
        bug_record_number=row["bug_record_number"],
    )


def _fetch_records(
    qvec: list[float], scope: Scope, query: str, topk: int, rrf_k: int
) -> list[Hit]:
    """Run the records RRF query and map its rows onto :class:`Hit`."""
    conn = _checkout()
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                _RECORDS_RRF_SQL,
                {
                    "qvec": _as_vector(qvec),
                    "category": scope.category,
                    "product": scope.product,
                    "article": scope.article_number,
                    "q": query,
                    "pool": settings.retrieval.pool,
                    "rrf_k": rrf_k,
                    "topk": topk,
                },
            )
            return [_score_row(r) for r in cur.fetchall()]
    finally:
        # The bare-connection pool stays in a transaction after a bare
        # ``conn.execute()``; rolling back releases the row/lock state so the
        # next query never deadlocks behind an idle-in-transaction holder.
        conn.rollback()
        _release(conn)


def _fetch_records_lexical(scope: Scope, query: str, topk: int) -> list[Hit]:
    """Lexical-only leg: GIN FTS match over ``records``, ranked by lexical RRF."""
    conn = _checkout()
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                _RECORDS_LEXICAL_SQL,
                {
                    "category": scope.category,
                    "product": scope.product,
                    "article": scope.article_number,
                    "q": query,
                    "pool": settings.retrieval.pool,
                    "rrf_k": settings.retrieval.rrf_k,
                    "topk": topk,
                },
            )
            return [_score_row(r) for r in cur.fetchall()]
    finally:
        conn.rollback()
        _release(conn)


# RAG RRF query (`CONTRACT.md` §8): identical in shape to the records RRF query
# but over `rag_chunks`. Rows are ranked by reciprocal rank fusion of the vector
# and lexical legs over the shared `rag_chunks` id.
#
# Both legs scope by the caller's category/product (optional): the
# same `%(category)s` / `%(product)s` filters used by `retrieve_fs`. An empty
# scope (both params NULL) matches every chunk, so an untagged ingest is never
# silently dropped when the caller isn't scoping; a non-empty scope matches only
# chunks tagged with the provided category or product, so scoping narrows the pool
# to the product being discussed.
_RAG_RRF_SQL = """
WITH vec AS (
    SELECT id, chunk_text, section_header, source_file, category, product,
           ROW_NUMBER() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rank
    FROM rag_chunks
    WHERE (
        -- Empty scope (both params NULL): match everything, so untagged data
        -- is never silently dropped when the caller isn't scoping.
        (%(category)s::text IS NULL AND %(product)s::text IS NULL)
        -- Non-empty scope: a chunk is in scope only when it carries a tag
        -- that matches a provided field. An untagged chunk (category/product
        -- NULL) yields NULL on every comparison, so it is excluded here —
        -- which is the whole point of scoping: pull only this product's docs.
        OR (
            (%(category)s::text IS NOT NULL AND category = %(category)s::text)
            OR (%(product)s::text IS NOT NULL AND product = %(product)s::text)
        )
    )
    ORDER BY embedding <=> %(qvec)s::vector
    LIMIT %(pool)s
),
txt AS (
    SELECT id, chunk_text, section_header, source_file, category, product,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank(fts, plainto_tsquery('simple', %(q)s)) DESC
           ) AS rank
    FROM rag_chunks
    WHERE fts @@ plainto_tsquery('simple', %(q)s)
      AND (
          -- Empty scope (both params NULL): match everything, so untagged data
          -- is never silently dropped when the caller isn't scoping.
          (%(category)s::text IS NULL AND %(product)s::text IS NULL)
          -- Non-empty scope: a chunk is in scope only when it carries a tag
          -- that matches a provided field. An untagged chunk (category/product
          -- NULL) yields NULL on every comparison, so it is excluded here —
          -- which is the whole point of scoping: pull only this product's docs.
          OR (
              (%(category)s::text IS NOT NULL AND category = %(category)s::text)
              OR (%(product)s::text IS NOT NULL AND product = %(product)s::text)
          )
      )
    LIMIT %(pool)s
)
SELECT
    COALESCE(v.id, t.id)                    AS id,
    COALESCE(v.chunk_text, t.chunk_text)    AS chunk_text,
    COALESCE(v.section_header, t.section_header) AS section_header,
    COALESCE(v.source_file, t.source_file)  AS source_file,
    COALESCE(v.category, t.category)        AS category,
    COALESCE(v.product, t.product)          AS product,
    (COALESCE(1.0/(%(rrf_k)s + v.rank), 0.0)
     + COALESCE(1.0/(%(rrf_k)s + t.rank), 0.0)) AS rrf_score
FROM vec v
FULL OUTER JOIN txt t ON v.id = t.id
ORDER BY rrf_score DESC
LIMIT %(topk)s;
"""

# Lexical-only leg over `rag_chunks`, used when the query embedding cannot be
# produced. The vector leg is dropped; rows are ranked by their lexical leg and
# scoped exactly like the RRF query above.
_RAG_LEXICAL_SQL = """
SELECT id, chunk_text, section_header, source_file, category, product,
       1.0/(%(rrf_k)s + t.rank) AS rrf_score
FROM (
    SELECT id, chunk_text, section_header, source_file, category, product,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank(fts, plainto_tsquery('simple', %(q)s)) DESC
           ) AS rank
    FROM rag_chunks
    WHERE fts @@ plainto_tsquery('simple', %(q)s)
      AND (
          -- Empty scope (both params NULL): match everything, so untagged data
          -- is never silently dropped when the caller isn't scoping.
          (%(category)s::text IS NULL AND %(product)s::text IS NULL)
          -- Non-empty scope: a chunk is in scope only when it carries a tag
          -- that matches a provided field. An untagged chunk (category/product
          -- NULL) yields NULL on every comparison, so it is excluded here —
          -- which is the whole point of scoping: pull only this product's docs.
          OR (
              (%(category)s::text IS NOT NULL AND category = %(category)s::text)
              OR (%(product)s::text IS NOT NULL AND product = %(product)s::text)
          )
      )
    LIMIT %(pool)s
) AS t
ORDER BY rrf_score DESC
LIMIT %(topk)s;
"""


def _score_rag_row(row: dict) -> Hit:
    """Map a single RAG RRF SELECT row onto a rag :class:`Hit`."""
    return Hit(
        id=row["id"],
        source="rag",
        score=row["rrf_score"],
        source_file=row["source_file"],
        section_header=row["section_header"],
        chunk_text=row["chunk_text"],
        category=row["category"],
        product=row["product"],
    )


def _fetch_rag(
    qvec: list[float],
    scope: Scope,
    query: str,
    topk: int,
    rrf_k: int,
) -> list[Hit]:
    """Run the RAG RRF query and map its rows onto :class:`Hit`."""
    conn = _checkout()
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                _RAG_RRF_SQL,
                {
                    "qvec": _as_vector(qvec),
                    "category": scope.category,
                    "product": scope.product,
                    "q": query,
                    "pool": settings.retrieval.pool,
                    "rrf_k": rrf_k,
                    "topk": topk,
                },
            )
            return [_score_rag_row(r) for r in cur.fetchall()]
    finally:
        conn.rollback()
        _release(conn)


def _fetch_rag_lexical(scope: Scope, query: str, topk: int) -> list[Hit]:
    """Lexical-only leg over `rag_chunks`, ranked by lexical RRF."""
    conn = _checkout()
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                _RAG_LEXICAL_SQL,
                {
                    "category": scope.category,
                    "product": scope.product,
                    "q": query,
                    "pool": settings.retrieval.pool,
                    "rrf_k": settings.retrieval.rrf_k,
                    "topk": topk,
                },
            )
            return [_score_rag_row(r) for r in cur.fetchall()]
    finally:
        conn.rollback()
        _release(conn)


def retrieve_rag(
    query: str,
    top_k: int | None = None,
    *,
    scope: Scope | None = None,
) -> list[Hit]:
    """Hybrid retrieval over `rag_chunks`.

    Combines a vector leg (HNSW cosine proximity to the query embedding) and a
    lexical leg (GIN full-text match over `fts`) with reciprocal rank fusion,
    then caps the results at ``top_k`` (default
    ``settings.retrieval.top_k_rag``).

    A caller's :class:`~app.records_repo.Scope` (category + product) is applied as
    a structured ``WHERE`` (optional) on both legs, mirroring :func:`retrieve_fs`.
    An empty scope (both fields unset) matches every chunk — so an untagged chunk
    ingested before scoping existed is never silently dropped; a non-empty scope
    matches only chunks tagged with the provided category or product, narrowing RAG
    to the product being discussed.

    If the query embedding fails, the lexical leg runs alone and a warning is
    logged — the search must not fail because the embedding server is down.

    Args:
        query: The natural-language query; embedded as its own text.
        top_k: Result cap; defaults to ``settings.retrieval.top_k_rag``.
        scope: Category/product filters. ``None`` or an all-``None`` scope applies
            no filter.

    Returns:
        Ranked :class:`Hit` rows, each with ``source='rag'``.
    """
    if top_k is None:
        top_k = settings.retrieval.top_k_rag
    scope = scope or Scope()

    try:
        with OLLAMA_LOCK:
            qvec = embed([query])[0]
    except EmbeddingError as exc:
        logger.warning(
            "embedding failed for RAG query; falling back to lexical-only "
            "retrieval: %s",
            exc,
        )
        return _fetch_rag_lexical(scope, query, top_k)

    return _fetch_rag(qvec, scope, query, top_k, settings.retrieval.rrf_k)


def retrieve_fs(scope: Scope, query: str, top_k: int | None = None) -> list[Hit]:
    """Structured-first hybrid retrieval over active records.

    The scope is applied as a structured ``WHERE`` (category + product + optional
    article_number) on both the vector and lexical legs; only ``status='active'``
    rows are considered. Hits are ranked with reciprocal rank fusion of the two
    legs and capped at ``top_k`` (default ``settings.retrieval.top_k_records``).
    A scope that matches no active rows yields ``[]``.

    If the query embedding fails, the lexical leg runs alone and a warning is
    logged — the search must not fail because the embedding server is down.

    Args:
        scope: Category/product/article_number filters.
        query: The failure query; embedded as its own text.
        top_k: Result cap; defaults to ``settings.retrieval.top_k_records``.

    Returns:
        Ranked :class:`Hit` rows, each with ``source='records'``.
    """
    if top_k is None:
        top_k = settings.retrieval.top_k_records

    try:
        with OLLAMA_LOCK:
            qvec = embed([query])[0]
    except EmbeddingError as exc:
        logger.warning(
            "embedding failed for search query; falling back to lexical-only "
            "retrieval: %s",
            exc,
        )
        return _fetch_records_lexical(scope, query, top_k)

    return _fetch_records(qvec, scope, query, top_k, settings.retrieval.rrf_k)
