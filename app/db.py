"""Database connection pool and idempotent migration runner.

Wraps a **psycopg v3** connection pool and applies ``migrations/*.sql`` files in
filename order, tracking what has been applied in a ``schema_migrations`` table
(see ``CONTRACT.md`` §5). Before execution, every migration's ``{{ key.path }}``
placeholders are substituted from ``settings`` (currently only ``0002_stats_role``
needs this, but the runner is ready for it).

Connection pool
---------------
A single module-level :class:`~psycopg_pool.ConnectionPool` is created lazily from
``settings.db.dsn`` using ``settings.db.pool_min`` / ``settings.db.pool_max``.
If the ``psycopg[binary,pool]`` extra is missing, the import at the top of the
module fails loudly, so the app never runs without pooling.

Migration runner contract (``CONTRACT.md`` §5)
----------------------------------------------
``run_migrations()`` creates ``schema_migrations (filename TEXT PRIMARY KEY,
applied_at TIMESTAMPTZ)`` if missing, applies each ``migrations/*.sql`` file in
filename order inside its own transaction, records each applied file, and is safe
to run repeatedly (idempotent).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import psycopg
from app.config import Settings, settings

# Repository root -- the ``migrations/`` directory sits two levels up.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"

# ``{{ stats.role_name }}`` style placeholders.
_PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][\w.]*)\s*}}")

# ``ConnectionPool`` lives in the separate ``psycopg-pool`` distribution pulled in
# by the ``psycopg[binary,pool]`` extra. The import is intentionally unguarded:
# if the extra is missing, the pool is unavailable and the app must refuse to
# start rather than silently fall back to a single bare connection (no pooling).
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | psycopg.Connection | None = None

# Fixed key for the migration advisory lock (``pg_advisory_lock(bigint)`` is
# session-scoped). A memorable key keeps the constant readable; the value only
# needs to be unique to this app among all advisory-lock users.
_MIGRATION_LOCK_KEY = 0x5F56_5961_5F66  # "Fiffia_"

# How long to wait for the migration lock during the boot race before giving
# up (seconds). The first container migrates; the second parks here instead of
# erroring, so whichever boot finishes first always wins the run.
_MIGRATION_LOCK_TIMEOUT_S = 60.0


def _resolve_dotted(path: str, root: Any) -> str:
    """Resolve a dotted path (``stats.role_name``) against ``root``."""
    node: Any = root
    for part in path.split("."):
        node = getattr(node, part)
    return str(node)


def _substitute(sql: str, cfg: Settings) -> str:
    """Replace every ``{{ key.path }}`` placeholder with its settings value."""

    def _match(match: re.Match[str]) -> str:
        return _resolve_dotted(match.group(1), cfg)

    return _PLACEHOLDER_RE.sub(_match, sql)


def get_pool() -> ConnectionPool:
    """Return a lazily created connection pool.

    :class:`~psycopg_pool.ConnectionPool` is created on first call using
    ``settings.db.dsn`` and ``min_size`` / ``max_size``. A missing ``psycopg``
    extra raises at import time, so this never falls back to a bare connection.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            min_size=settings.db.pool_min,
            max_size=settings.db.pool_max,
            conninfo=settings.db.dsn,
        )
    return _pool


def _checkout() -> psycopg.Connection:
    """Hand out a pooled connection, forcing autocommit off on every checkout.

    A connection returned by ``ConnectionPool.getconn()`` can carry autocommit
    back on from a previous checkout (the pool does not reset it). psycopg's
    ``commit()`` is a no-op while autocommit is on, which would leave writes
    uncommitted and un-tracked across TRUNCATE boundaries between tests, so
    autocommit is forced off on every checkout.
    """
    _pool = get_pool()
    conn = _pool.getconn()
    if getattr(conn, "autocommit", False):
        conn.autocommit = False
    return conn


def _release(conn: psycopg.Connection) -> None:
    """Return a checked-out connection to the pool."""
    _pool.putconn(conn)


def close_pool() -> None:
    """Close the connection pool (primarily for tests / clean shutdown)."""
    global _pool
    if _pool is None:
        return
    _pool.close()
    _pool = None


def _open_dedicated_conn() -> psycopg.Connection:
    """Open a single bare connection outside the pool for lock-held work.

    ``run_migrations()`` must hold a session-scoped advisory lock for the whole
    of migration while every running process waits for the same lock. Holding a
    pooled connection for that span would risk a pool deadlock if ``pool_min``
    connections are already checked out, so the lock lives on a private,
    uncached connection that is opened when the lock is taken and closed when it
    is released.
    """

    return psycopg.connect(settings.db.dsn, connect_timeout=5)


def _acquire_migration_lock() -> psycopg.Connection:
    """Acquire the migration advisory lock, returning the holding connection.

    ``pg_advisory_lock(bigint)`` is session-scoped and blocks once held, which
    makes it unsuitable for a plain wait inside the boot race (a second container
    would hold a pooled connection while waiting and could deadlock a
    ``pool_min == 1`` pool). Instead we take the lock non-blockingly and poll
    ``pg_try_advisory_lock`` until it succeeds or the timeout elapses. On timeout
    we release the connection and surface a clear error rather than leaving a
    dangling lock or a half-run migration.
    """

    deadline = time.monotonic() + _MIGRATION_LOCK_TIMEOUT_S
    while True:
        conn = _open_dedicated_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s)",
                    (_MIGRATION_LOCK_KEY,),
                )
                if cur.fetchone()[0]:
                    return conn
        except Exception:
            conn.close()
            raise

        if time.monotonic() >= deadline:
            conn.close()
            raise RuntimeError(
                "timed out acquiring migration advisory lock "
                f"(after {_MIGRATION_LOCK_TIMEOUT_S:.0f}s) — is another runner "
                "holding it?"
            )
        conn.close()
        time.sleep(0.5)


def _release_migration_lock(conn: psycopg.Connection) -> None:
    """Release the migration advisory lock and close the holding connection.

    On a failed migration the transaction on ``conn`` is already aborted, so the
    ``pg_advisory_unlock`` call would raise ``InFailedSqlTransaction`` and mask
    the real error. Roll back first so the unlock runs on a live transaction.
    """

    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
    finally:
        conn.close()


def _create_schema_migrations_table(conn: psycopg.Connection) -> None:
    """Ensure the ``schema_migrations`` tracking table exists (on ``conn``)."""

    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()


def _applied_files(conn: psycopg.Connection) -> set[str]:
    """Return the set of migration filenames already recorded (on ``conn``)."""

    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def run_migrations() -> list[str]:
    """Apply every migration in ``migrations/`` that has not yet been applied.

    Migrations are applied in filename order, each inside its own transaction,
    and recorded in ``schema_migrations``. Running this again after everything is
    applied returns an empty list (idempotent).

    A single session-scoped ``pg_advisory_lock`` is held for the whole run on a
    dedicated connection, so concurrent boot migrations in separate containers
    serialise instead of racing the same files.
    """

    # Hold the advisory lock for the entire migration. All SQL below runs on
    # this connection; no additional pooled connection is checked out while the
    # lock is held, so a ``pool_min == 1`` pool can never deadlock.
    lock_conn = _acquire_migration_lock()
    try:
        _create_schema_migrations_table(lock_conn)
        applied = _applied_files(lock_conn)

        applied_files: list[str] = []
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue

            sql = _substitute(path.read_text(encoding="utf-8"), settings)
            with lock_conn.cursor() as cur:
                cur.execute(sql)
            lock_conn.commit()
            with lock_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (path.name,),
                )
            lock_conn.commit()
            applied_files.append(path.name)

        return applied_files
    finally:
        _release_migration_lock(lock_conn)
