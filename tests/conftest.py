"""Pytest fixtures for database tests.

``db_conn`` is a connection to a dedicated **test** database. The test DSN comes
from ``TEST_DATABASE_DSN`` if set, otherwise from ``settings.db.dsn``; either way
it is independent of any running application. The module applies
``run_migrations()`` once against that database so the smoke tests exercise the
real migration path, and the ``reset_db`` autouse fixture truncates the
``records`` / ``records_audit`` tables before each test so every test starts
from an empty, isolated schema.
"""

from __future__ import annotations

import os
from dataclasses import replace

import psycopg
import pytest

from app import db
from app.config import settings

DATABASE_DSN = os.environ.get("TEST_DATABASE_DSN")


@pytest.fixture(scope="module")
def db_conn() -> psycopg.Connection:
    """Yield a test-database connection after a clean migration.

    The fixture opens its own connection (it does not rely on the app pool), so
    the tests are isolated from whatever DSN the application is configured for.
    The ``records`` tables are truncated (not dropped) so each run starts empty:
    the migration runner is idempotent and only recreates tables that are missing,
    so dropping them between runs would leave the schema empty.
    """
    dsn = DATABASE_DSN or settings.db.dsn
    if dsn != settings.db.dsn:
        # Point the app at the test database so run_migrations writes there.
        settings.db = replace(settings.db, dsn=dsn)
    db.run_migrations()

    conn = psycopg.connect(dsn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True, scope="function")
def reset_db(db_conn: psycopg.Connection) -> None:
    """Truncate ``records`` / ``records_audit`` before each test.

    Each test commits rows into the shared test database; without truncating
    before each one the dedup unique index sees rows left behind by earlier
    tests. ``db_conn`` owns the connection for the whole module.

    Truncating happens through a *separate* autocommit connection so it is never
    blocked by an open transaction on ``db_conn``. Each test also leaves that
    transaction open when it calls bare ``conn.execute()`` (a shared lock that
    would otherwise block the next test's ``TRUNCATE``), so the module
    connection is rolled back after every test to release it.
    """
    with psycopg.connect(settings.db.dsn) as c:
        c.execute("TRUNCATE records_audit, records")
        c.commit()
    try:
        yield
    finally:
        db_conn.rollback()
