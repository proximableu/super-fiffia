"""Smoke tests for the migration runner and schema (T0.2).

These verify that ``migrations/0001_init.sql`` produces the expected tables and
that the dedup contract (active-only partial unique index) is in place. They are
smoke tests, not a full schema regression suite.
"""

from __future__ import annotations

import pytest


def _columns(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            ORDER BY ordinal_position
            """
            ,
            (table,),
        )
        return {row[0] for row in cur.fetchall()}


def _index_def(conn, table: str, index: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = %s
              AND indexname = %s
            """
            ,
            (table, index),
        )
        row = cur.fetchone()
    assert row is not None, f"expected index {index} on {table}"
    return row[0]


@pytest.mark.parametrize("table", ["records", "records_audit", "rag_chunks"])
def test_expected_tables_exist(db_conn, table: str):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = %s",
            (table,),
        )
        assert cur.fetchone() is not None, f"expected table {table} to exist"


def test_records_columns(db_conn):
    expected = {
        "id",
        "category",
        "product",
        "article_number",
        "failure_description",
        "solution_description",
        "content_hash",
        "ncr",
        "bug_record_number",
        "source",
        "created_by",
        "status",
        "embedding",
        "fts",
        "embed_model",
        "embed_dim",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(_columns(db_conn, "records"))


def test_content_hash_is_char32(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'records'
              AND column_name = 'content_hash'
            """
        )
        row = cur.fetchone()
    assert row is not None
    data_type, char_length = row
    assert data_type == "character"
    assert char_length == 32


def test_records_audit_columns(db_conn):
    expected = {"id", "record_id", "action", "changed", "actor", "at"}
    assert expected.issubset(_columns(db_conn, "records_audit"))


def test_rag_chunks_columns(db_conn):
    expected = {
        "id",
        "source_file",
        "chunk_index",
        "section_header",
        "chunk_text",
        "content_hash",
        "embedding",
        "fts",
        "created_at",
    }
    assert expected.issubset(_columns(db_conn, "rag_chunks"))


def test_embedding_is_vector768(db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT format_type(atttypid, atttypmod)
            FROM pg_attribute
            WHERE attrelid = 'records'::regclass
              AND attname = 'embedding'
              AND NOT attnum < 0
            """
        )
        (att_type,) = cur.fetchone()
    assert att_type == "vector(768)"


def test_no_hard_delete_index(db_conn):
    """The active-only partial unique index enforces soft-delete dedup."""
    index_def = _index_def(db_conn, "records", "uq_records_content_hash")
    assert "UNIQUE" in index_def.upper()
    assert "WHERE" in index_def.upper()
    assert "active" in index_def


def test_rag_chunk_unique_index(db_conn):
    index_def = _index_def(db_conn, "rag_chunks", "uq_rag_chunk")
    assert "UNIQUE" in index_def.upper()
    assert "source_file" in index_def


def test_fts_and_hnsw_indexes_present(db_conn):
    records_indexes = {
        row[0]
        for row in db_conn.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'records'
            """
        ).fetchall()
    }
    assert "ix_records_fts" in records_indexes
    assert "ix_records_embedding_hnsw" in records_indexes
