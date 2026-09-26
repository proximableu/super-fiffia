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


def _create_schema_migrations_table() -> None:
    """Ensure the ``schema_migrations`` tracking table exists."""
    conn = _checkout()
    try:
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
    finally:
        _release(conn)


def _applied_files() -> set[str]:
    """Return the set of migration filenames already recorded."""
    conn = _checkout()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations")
            return {row[0] for row in cur.fetchall()}
    finally:
        _release(conn)


def run_migrations() -> list[str]:
    """Apply every migration in ``migrations/`` that has not yet been applied.

    Migrations are applied in filename order, each inside its own transaction,
    and recorded in ``schema_migrations``. Running this again after everything is
    applied returns an empty list (idempotent).

    Returns the list of filenames that were newly applied.
    """
    get_pool()
    _create_schema_migrations_table()
    applied = _applied_files()

    applied_files: list[str] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue

        sql = _substitute(path.read_text(encoding="utf-8"), settings)
        conn = _checkout()
        try:
            conn.execute(sql)
            conn.commit()
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)",
                (path.name,),
            )
            conn.commit()
        finally:
            _release(conn)
        applied_files.append(path.name)

    return applied_files
