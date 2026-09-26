"""Acceptance tests for RAG chunking and idempotent ingestion (T2.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

from app.rag import chunk, ingest
from tests.conftest import db_conn


def _count_rows(db_conn: Any, sql: str, args: tuple[Any, ...] = ()) -> int:
    """Return the number of rows ``sql`` matches on ``db_conn``."""
    try:
        with db_conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(sql, args)
            return len(cur.fetchall())
    finally:
        db_conn.rollback()


def _truncate(db_conn: Any) -> None:
    """TRUNCATE ``rag_chunks`` on a separate autocommit connection."""
    temp = psycopg.connect(
        db_conn.info.dsn, autocommit=True, row_factory=psycopg.rows.dict_row
    )
    try:
        with temp.cursor() as cur:
            cur.execute("TRUNCATE rag_chunks")
    finally:
        # The connection is owned by this helper (it was never checked out from
        # the pool), so it is closed rather than returned via ``_release``.
        temp.close()


def _write_doc(root: Path, name: str, content: str) -> None:
    """Write a document under ``root``."""
    (root / name).write_text(content, encoding="utf-8")


class _FakeEmbed:
    """Count calls and return a fixed-width vector per input text."""

    def __init__(self) -> None:
        self.calls = 0
        self.inputs: list[str] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.inputs.extend(texts)
        return [[0.0] * 768 for _ in texts]


def test_chunk_basic() -> None:
    """`chunk` splits a document into chunks and captures section headers."""
    text = "# First\nHello world.\n\nSecond paragraph here.\n## Nested\nInner text."
    chunks = chunk(text, chunk_chars=100, overlap=10)

    assert chunks
    for index, header, body in chunks:
        assert isinstance(index, int)
        assert isinstance(body, str) and body
        assert index >= 0

    headers = {header for _, header, _ in chunks}
    assert "Nested" in headers


def test_chunk_rejects_bad_params() -> None:
    """`chunk` validates chunk_chars and overlap."""
    import pytest

    with pytest.raises(ValueError):
        chunk("x", chunk_chars=0)
    with pytest.raises(ValueError):
        chunk("x", chunk_chars=10, overlap=10)


def test_ingest_writes_chunks_with_embeddings(
    db_conn: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """First ingest creates rows with non-None embeddings."""
    _truncate(db_conn)
    doc = "# Header\n" + "Sentence number one.\n\n" + "Sentence number two.\n"
    _write_doc(tmp_path, "doc.md", doc)

    fake = _FakeEmbed()
    monkeypatch.setattr("app.rag.embed", fake)

    result = ingest(str(tmp_path), chunk_chars=1000, overlap=50)

    assert result.files == 1
    assert result.chunks == result.upserted
    assert result.embedded == result.upserted
    assert result.upserted > 0
    assert fake.calls == 1

    total = _count_rows(db_conn, "SELECT 1 FROM rag_chunks")
    with_embeddings = _count_rows(
        db_conn, "SELECT 1 FROM rag_chunks WHERE embedding IS NOT NULL"
    )
    assert total == result.upserted
    assert with_embeddings == result.upserted
    _truncate(db_conn)


def test_reingest_is_idempotent(
    db_conn: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """Re-ingesting the same document adds no rows and no embedding calls."""
    _truncate(db_conn)
    doc = "# Header\n" + "Distinct sentence A.\n\n" + "Distinct sentence B.\n"
    _write_doc(tmp_path, "doc.md", doc)

    fake = _FakeEmbed()
    monkeypatch.setattr("app.rag.embed", fake)

    first = ingest(str(tmp_path), chunk_chars=1000, overlap=50)
    rows_after_first = _count_rows(db_conn, "SELECT 1 FROM rag_chunks")
    assert rows_after_first == first.upserted

    second = ingest(str(tmp_path), chunk_chars=1000, overlap=50)
    rows_after_second = _count_rows(db_conn, "SELECT 1 FROM rag_chunks")

    assert rows_after_second == rows_after_first
    assert second.upserted == 0
    assert second.embedded == 0
    assert first.embedded > 0

    _truncate(db_conn)


def test_dry_run_counts_without_writing(
    db_conn: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """`dry_run` reports counts but writes nothing."""
    _truncate(db_conn)
    _write_doc(tmp_path, "doc.md", "One.\n\nTwo.\n\nThree.")

    fake = _FakeEmbed()
    monkeypatch.setattr("app.rag.embed", fake)

    result = ingest(str(tmp_path), chunk_chars=500, overlap=20, dry_run=True)

    assert result.files == 1
    assert result.upserted == 0
    assert fake.calls == 0
    assert _count_rows(db_conn, "SELECT 1 FROM rag_chunks") == 0

    _truncate(db_conn)
