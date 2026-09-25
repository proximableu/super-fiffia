"""Database connection pool and idempotent migration runner.

Wraps a **psycopg v3** connection pool and applies ``migrations/*.sql`` files in
filename order, tracking what has been applied in a ``schema_migrations`` table
(see ``CONTRACT.md`` §5). Before execution, every migration's ``{{ key.path }}``
placeholders are substituted from ``settings`` (currently only ``0002_stats_role``
needs this, but the runner is ready for it).

Connection pool
---------------
A single module-level connection pool is created lazily from
``settings.db.dsn`` using ``settings.db.pool_min`` / ``settings.db.pool_max``.
If ``psycopg.pool`` is unavailable (e.g. a wheel built without the pool module)
the runner falls back to a single bare connection; ``_checkout()`` / ``_release()``
treat both transparently.

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

try:  # psycopg v3 ships a connection pool; fall back to plain connections.
    from psycopg.pool import ConnectionPool
except Exception:  # pragma: no cover - exercised only when the wheel is broken
    ConnectionPool = None  # type: ignore[assignment]

_pool: ConnectionPool | psycopg.Connection | None = None


def _is_pool() -> bool:
    """Whether the active backend is a real connection pool."""
    return ConnectionPool is not None


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


def _connect() -> psycopg.Connection:
    """Open a single connection using the configured DSN."""
    return psycopg.connect(settings.db.dsn)


def get_pool() -> ConnectionPool | psycopg.Connection:
    """Return a lazily created connection pool.

    Falls back to a single bare connection when ``psycopg.pool`` is not
    available (e.g. a wheel built without the pool module); the rest of the
    runner treats both transparently via ``_checkout()`` / ``_release()``.
    """
    global _pool
    if _pool is None:
        if _is_pool():
            _pool = ConnectionPool(
                min_connections=settings.db.pool_min,
                max_connections=settings.db.pool_max,
                conninfo=settings.db.dsn,
            )
        else:  # pragma: no cover - only when the pool module is missing
            _pool = _connect()
    return _pool


def _checkout() -> psycopg.Connection:
    """Hand out a connection to use, from the pool or the bare fallback.

    A connection from ``ConnectionPool.getconn()`` can carry autocommit back on
    from a previous checkout (the pool does not reset it). psycopg's
    ``commit()`` is a no-op while autocommit is on, which would leave writes
    uncommitted and un-tracked across TRUNCATE boundaries between tests, so
    autocommit is forced off on every checkout.

    The pool is created lazily here (via :func:`get_pool`) so callers that never
    call :func:`run_migrations` — such as :func:`records_service.submit` — still
    get a live connection instead of ``None``.
    """
    get_pool()
    if _is_pool():
        conn = _pool.getconn()
        if getattr(conn, "autocommit", False):
            conn.autocommit = False
        return conn
    return _pool


def _release(conn: psycopg.Connection) -> None:
    """Return a checked-out connection to the pool, if applicable."""
    if _is_pool() and _pool is not None:
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
