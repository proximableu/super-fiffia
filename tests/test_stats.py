"""Acceptance for T6.1 — the ``fs_stats_reader`` read-only role and the
statistics SQL (``sql/stats.sql``, ``F&S_REQUIREMENTS.md`` §8).

The read-only role must run every grouped query in ``sql/stats.sql`` and be
unable to write to ``records`` or read ``records_audit`` / ``rag_chunks``.
Each test opens its own committed connection so it never interferes with the
``reset_db`` truncation used by the rest of the suite.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from app.config import settings

TEST_DSN = os.environ.get("TEST_DATABASE_DSN")

# ``fs_stats_reader`` credentials from ``settings.stats`` (set by the migration
# runner). For the test database, inject them in place of ``fiffia``.
if TEST_DSN:
    _sample = TEST_DSN.split("@")[-1]
    _STATS_URL = f"postgresql://{settings.stats.role_name}:change_me@{_sample}"
else:
    _STATS_URL = (
        f"postgresql://{settings.stats.role_name}:"
        f"{settings.stats.role_password}@{settings.db.dsn.split('@')[1]}"
    )


class _Superuser:
    """A fresh superuser connection, committed on exit, isolated per test."""

    def __enter__(self) -> psycopg.Connection:
        self._conn = psycopg.connect(settings.db.dsn)
        return self._conn

    def __exit__(self, *exc: object) -> None:
        if exc[0] is None:
            self._conn.commit()
        self._conn.close()


def _run_as_reader(sql: str) -> list[tuple]:
    """Execute ``sql`` through a fresh ``fs_stats_reader`` connection."""
    with psycopg.connect(_STATS_URL) as c:
        return c.execute(sql).fetchall()


def _reader_denies(sql: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _run_as_reader(sql)


def _seed() -> None:
    """Seed active/archived rows so the stats queries have known data.

    Runs on its own committed superuser connection so the rows are visible to a
    separate ``fs_stats_reader`` connection. Truncates first because the test
    database is shared with the rest of the suite and is not reset between our
    tests, which keeps the counts deterministic.
    """
    with _Superuser() as conn:
        conn.execute("TRUNCATE records_audit, records")
        conn.execute(
            """
            INSERT INTO records
                (id, category, product, article_number, failure_description,
                 solution_description, content_hash, status, embed_model, embed_dim,
                 created_at)
            VALUES
                (gen_random_uuid(), 'Elektrik', 'Motor', 'ART-100', 'Motor startar inte',
                 'Bytte kontakter', 'hashA', 'active', 'snowflake-arctic-embed2:568m', 1024,
                 now() - interval '2 months'),
                (gen_random_uuid(), 'Elektrik', 'Motor', NULL, 'Overbelastning',
                 'Nedbring last', 'hashB', 'active', 'snowflake-arctic-embed2:568m', 1024,
                 now() - interval '1 month'),
                (gen_random_uuid(), 'Hydraulik', 'Pump', NULL, 'Tryckfall', 'Byt packning',
                 'hashC', 'archived', 'snowflake-arctic-embed2:568m', 1024, now());
            """
        )


def test_stats_role_exists(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", ("fs_stats_reader",))
        assert cur.fetchone() is not None, "fs_stats_reader role must exist"


def test_stats_role_cannot_write():
    _reader_denies(
        "INSERT INTO records (category, product, status) VALUES ('x', 'y', 'active')"
    )


def test_stats_role_cannot_read_audit():
    _reader_denies("SELECT 1 FROM records_audit")


def test_stats_queries_run():
    """The four queries in sql/stats.sql execute and return the expected counts."""
    _seed()
    # Each query returns exactly one row carrying its aggregate COUNT(*) value.
    # (query, expected count). Archived rows are excluded; ART-100 is the only
    # active article_number.
    queries: list[tuple[str, int]] = [
        (
            (
                "SELECT category, product, COUNT(*) FROM records WHERE status='active' "
                "GROUP BY category, product ORDER BY COUNT(*) DESC;"
            ),
            2,  # two active Motor rows (one category/product group)
        ),
        (
            "SELECT COUNT(*) FROM records WHERE status='active';",
            2,  # two active rows seeded
        ),
        (
            "SELECT COUNT(DISTINCT content_hash) FROM records WHERE status='active';",
            2,  # two distinct hashes
        ),
        (
            (
                "SELECT article_number, COUNT(*) FROM records WHERE status='active' "
                "AND article_number IS NOT NULL GROUP BY article_number ORDER BY 2 DESC LIMIT 20;"
            ),
            1,  # one active article_number (ART-100)
        ),
    ]

    for sql, expected_n in queries:
        count = _run_as_reader(sql)[0][-1]
        assert count == expected_n, f"expected count {expected_n} for: {sql}"
