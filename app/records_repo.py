"""DB access for records: insert (with dedup), get, list, update, archive,
restore, and the append-only audit writer.

This module is the only place that talks to the ``records`` / ``records_audit``
tables. Every function takes a psycopg v3 connection and binds every parameter —
SQL string concatenation of user input never happens (CONTRACT.md §6).

The dedup unique index ``uq_records_content_hash`` (on ``status='active'`` rows)
is the authority. Because a duplicate is detected by the database rather than by
a check-then-insert race, an ``IntegrityError`` is re-raised as
:class:`DuplicateError` carrying the existing record's id and created-at time.

The Pydantic models (:class:`Scope`, :class:`RecordIn`, :class:`RecordOut`)
live here so they are the single source of truth that the higher layers
(``records_service`` / ``retrieval`` / ``api``) import from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional, Sequence, Tuple
from uuid import UUID

import json

import psycopg
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Pydantic schemas (single source of truth — imported by records_service,
# retrieval and api)
# --------------------------------------------------------------------------- #

Status = str  # "active" | "archived"
Source = str  # "manual" | "import" | "api"


class Scope(BaseModel):
    category: Optional[str] = None
    product: Optional[str] = None
    article_number: Optional[str] = None


class RecordIn(BaseModel):
    category: str
    product: str
    article_number: Optional[str] = None
    failure_description: str = Field(min_length=1)
    solution_description: str = Field(min_length=1)
    ncr: Optional[str] = None
    bug_record_number: Optional[str] = None
    source: Source = "manual"


class RecordOut(BaseModel):
    id: UUID
    category: str
    product: str
    article_number: Optional[str]
    failure_description: str
    solution_description: str
    ncr: Optional[str]
    bug_record_number: Optional[str]
    content_hash: str
    source: Source
    status: Status
    embed_model: Optional[str]
    embed_dim: Optional[int]
    created_at: datetime
    updated_at: datetime


class Hit(BaseModel):
    """Unified search hit. ``source`` discriminates records from RAG chunks."""

    id: UUID
    source: Literal["records", "rag"]
    score: float
    # records fields (None for rag hits)
    category: Optional[str] = None
    product: Optional[str] = None
    article_number: Optional[str] = None
    failure_description: Optional[str] = None
    solution_description: Optional[str] = None
    ncr: Optional[str] = None
    bug_record_number: Optional[str] = None
    # rag fields (None for record hits)
    source_file: Optional[str] = None
    section_header: Optional[str] = None
    chunk_text: Optional[str] = None


class ChatTurn(BaseModel):
    """A single message in the in-memory conversation history."""

    role: Literal["user", "assistant"]
    content: str


class AgentAction(BaseModel):
    """The typed, structured step the agent takes on one loop iteration.

    Emitted as JSON by the model's structured output and parsed here. Exactly
    one field carries the action payload: ``query`` for the two search actions,
    ``clarification`` for ``ask_clarification`` and ``answer`` for
    ``final_answer``.
    """

    thought: str = Field(description="Reasoning about context quality and the next step.")
    action: Literal["search_records", "search_rag", "ask_clarification", "final_answer"]
    query: Optional[str] = Field(
        default=None, description="Reformulated query for a search action."
    )
    filters: Optional[Scope] = Field(
        default=None, description="Optional metadata filters for search_records."
    )
    clarification: Optional[str] = Field(
        default=None, description="Question to the user (ask_clarification)."
    )
    answer: Optional[str] = Field(
        default=None, description="Final answer (final_answer)."
    )


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class DuplicateError(RuntimeError):
    """A record with the same ``content_hash`` already exists and is active."""

    def __init__(self, existing_id: UUID, created_at: datetime) -> None:
        super().__init__(f"duplicate record {existing_id}")
        self.existing_id = existing_id
        self.created_at = created_at


class NotFoundError(RuntimeError):
    """A record id did not resolve to any row."""

    def __init__(self, record_id: UUID) -> None:
        super().__init__(f"record {record_id} not found")
        self.record_id = record_id


# --------------------------------------------------------------------------- #
# Row <-> model helpers
# --------------------------------------------------------------------------- #

# columns of ``records`` that map 1:1 onto :class:`RecordOut` (id last because
# it is converted from a string to a uuid)
_RECORD_COLUMNS = (
    "id",
    "category",
    "product",
    "article_number",
    "failure_description",
    "solution_description",
    "ncr",
    "bug_record_number",
    "content_hash",
    "source",
    "status",
    "embed_model",
    "embed_dim",
    "created_at",
    "updated_at",
)


def _as_vector(values: Sequence[float]) -> str:
    """Render a Python float list as a pgvector literal ``"[0.1,-0.2]"``.

    psycopg does not adapt a bare list to a ``vector`` column, so the value is
    bound as a string and cast ``::vector`` in SQL (see CONTRACT.md §8).
    """
    return "[" + ",".join(str(v) for v in values) + "]"


def _row_to_record(row: dict[str, Any]) -> RecordOut:
    """Map a ``records`` SELECT row onto a :class:`RecordOut`."""
    return RecordOut(
        id=UUID(str(row["id"])),
        category=row["category"],
        product=row["product"],
        article_number=row["article_number"],
        failure_description=row["failure_description"],
        solution_description=row["solution_description"],
        ncr=row["ncr"],
        bug_record_number=row["bug_record_number"],
        content_hash=row["content_hash"],
        source=row["source"],
        status=row["status"],
        embed_model=row["embed_model"],
        embed_dim=row["embed_dim"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# fields of :class:`RecordIn` that map onto ``records`` columns
_INPUT_FIELDS: tuple[str, ...] = (
    "category",
    "product",
    "article_number",
    "failure_description",
    "solution_description",
    "ncr",
    "bug_record_number",
    "source",
)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def audit(
    conn: psycopg.Connection,
    record_id: UUID,
    action: str,
    changed: Optional[dict] = None,
    actor: Optional[str] = None,
) -> None:
    """Append one row to ``records_audit`` (append-only log).

    :param action:  one of ``create`` / ``update`` / ``archive`` / ``restore``.
    :param changed: optional ``{field: {old, new}}`` map for ``update`` rows.
    """
    record_id_str = str(record_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO records_audit (record_id, action, changed, actor)
            VALUES (%s, %s, %s, %s)
            """,
            (record_id_str, action, json.dumps(changed) if changed is not None else None, actor),
        )
    # psycopg v3's cursor context manager commits on exit, so the audit row
    # survives even when the caller (insert/update/archive) has already committed
    # its own work — see CONTRACT.md §9.


def insert(
    conn: psycopg.Connection,
    record: RecordIn,
    content_hash: str,
    embedding: list[float],
    embed_model: str,
    embed_dim: int,
    actor: str,
) -> RecordOut:
    """Insert a record and return it as :class:`RecordOut`.

    The unique partial index makes a duplicate a database error; it is caught
    and re-raised as :class:`DuplicateError` carrying the existing record's id.
    On success an audit row with ``action='create'`` is appended.
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        try:
            cur.execute(
                """
                INSERT INTO records (
                    category, product, article_number,
                    failure_description, solution_description,
                    content_hash, ncr, bug_record_number, source, status,
                    embedding, embed_model, embed_dim, created_by
                ) VALUES (
                    %(category)s, %(product)s, %(article_number)s,
                    %(failure_description)s, %(solution_description)s,
                    %(content_hash)s, %(ncr)s, %(bug_record_number)s, %(source)s, 'active',
                    %(embedding)s::vector, %(embed_model)s, %(embed_dim)s, %(created_by)s
                )
                RETURNING id, category, product, article_number,
                          failure_description, solution_description, ncr,
                          bug_record_number, content_hash, source, status,
                          embed_model, embed_dim, created_at, updated_at
                """,
                {
                    "category": record.category,
                    "product": record.product,
                    "article_number": record.article_number,
                    "failure_description": record.failure_description,
                    "solution_description": record.solution_description,
                    "content_hash": content_hash,
                    "ncr": record.ncr,
                    "bug_record_number": record.bug_record_number,
                    "source": record.source,
                    "embedding": _as_vector(embedding),
                    "embed_model": embed_model,
                    "embed_dim": embed_dim,
                    "created_by": actor,
                },
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT ... RETURNING returned no row")
        except psycopg.errors.IntegrityError as exc:
            # The unique index fired — roll back the aborted transaction and
            # surface the existing record so the caller can return a 409.
            conn.rollback()
            existing = _find_active_by_hash(conn, content_hash)
            if existing is None:
                raise RuntimeError(
                    "duplicate hash violation but no active record found"
                ) from exc
            raise DuplicateError(existing["id"], existing["created_at"]) from None

    audit(conn, row["id"], "create", {}, actor)
    return _row_to_record(row)


def get(conn: psycopg.Connection, record_id: UUID) -> Optional[RecordOut]:
    """Return a record by id, or ``None`` if it does not exist."""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"SELECT {', '.join(_RECORD_COLUMNS)} FROM records WHERE id=%s",
            (str(record_id),),
        )
        row = cur.fetchone()
    return _row_to_record(row) if row else None


def list_records(
    conn: psycopg.Connection,
    scope: Optional[Scope],
    status: Status,
    q: Optional[str],
    limit: int,
    offset: int,
) -> Tuple[list[RecordOut], int]:
    """Return records matching the filters plus the total match count.

    With ``q`` rows are ordered by lexical similarity over the FTS index;
    otherwise by ``created_at`` DESC. ``scope`` (category/product/article_number)
    narrows the window; ``status`` selects active/archived rows.
    """
    scope = scope or Scope()
    where, params = _build_where(scope, status, q)
    ordering = (
        "ts_rank(fts, plainto_tsquery('simple', %(q)s)) DESC" if q else "created_at DESC"
    )

    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM records WHERE {where}",
            params,
        )
        total = cur.fetchone()["n"]

        cur.execute(
            f"SELECT {', '.join(_RECORD_COLUMNS)} FROM records "
            f"WHERE {where} ORDER BY {ordering} LIMIT %(limit)s OFFSET %(offset)s",
            {**params, "limit": limit, "offset": offset},
        )
        rows = cur.fetchall()
    return [_row_to_record(r) for r in rows], int(total)


def update(
    conn: psycopg.Connection,
    record_id: UUID,
    record: RecordIn,
    content_hash: str,
    embedding: Optional[list[float]],
    actor: str,
) -> RecordOut:
    """Update a record's fields and return the new row.

    Only columns whose value actually changed are recorded in the audit row's
    ``changed`` map. If ``embedding`` is provided the embedding column is
    recomputed; a resulting ``content_hash`` that collides with another active
    record raises :class:`DuplicateError`. A missing id raises
    :class:`NotFoundError`.
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"SELECT {', '.join(_RECORD_COLUMNS)} FROM records WHERE id=%s",
            (str(record_id),),
        )
        current = cur.fetchone()
        if current is None:
            raise NotFoundError(record_id)

        changed = _compute_changed(current, record)
        set_clauses, params = _build_update_set(record, content_hash, embedding)
        params["id"] = str(record_id)

        try:
            cur.execute(
                f"UPDATE records SET {', '.join(set_clauses)} "
                f"RETURNING {', '.join(_RECORD_COLUMNS)}",
                params,
            )
            updated = cur.fetchone()
            if updated is None:
                raise NotFoundError(record_id)
        except psycopg.errors.IntegrityError as exc:
            conn.rollback()
            existing = _find_active_by_hash(conn, content_hash, exclude_id=record_id)
            if existing is None:
                raise RuntimeError(
                    "duplicate hash collision but no active record found"
                ) from exc
            raise DuplicateError(existing["id"], existing["created_at"]) from None

    audit(conn, record_id, "update", changed, actor)
    return _row_to_record(updated)


def archive(conn: psycopg.Connection, record_id: UUID, actor: str) -> RecordOut:
    """Soft-delete: set ``status='archived'`` and return the new row."""
    return _set_status(conn, record_id, "archived", "archive", actor)


def restore(conn: psycopg.Connection, record_id: UUID, actor: str) -> RecordOut:
    """Restore: set ``status='active'`` and return the new row.

    Re-checks the unique hash: if restoring makes the hash collide with another
    active record the update raises :class:`DuplicateError`.
    """
    return _set_status(conn, record_id, "active", "restore", actor)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _build_where(
    scope: Scope, status: Status, q: Optional[str]
) -> Tuple[str, dict]:
    """Assemble the ``WHERE`` clause and bound parameters for a list query."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    # ``status`` restricts to active/archived rows; any other value (the WebUI's
    # "all") omits the filter.
    if status in ("active", "archived"):
        clauses.append("status = %(status)s")
        params["status"] = status
    if scope.category:
        clauses.append("category = %(category)s")
        params["category"] = scope.category
    if scope.product:
        clauses.append("product = %(product)s")
        params["product"] = scope.product
    if scope.article_number:
        clauses.append("article_number = %(article_number)s")
        params["article_number"] = scope.article_number
    if q:
        clauses.append("fts @@ plainto_tsquery('simple', %(q)s)")
        params["q"] = q
    return " AND ".join(clauses), params


def _compute_changed(current: dict, record: RecordIn) -> dict:
    """Return ``{field: {old, new}}`` for the input fields that actually changed."""
    changed: dict[str, dict] = {}
    for field_name in _INPUT_FIELDS:
        old = current[field_name]
        new = getattr(record, field_name)
        if old != new:
            changed[field_name] = {"old": old, "new": new}
    return changed


def _build_update_set(
    record: RecordIn, content_hash: str, embedding: Optional[list[float]]
) -> Tuple[list[str], dict]:
    """Build the ``SET`` clause and parameters for an UPDATE."""
    set_clauses: list[str] = []
    params: dict[str, Any] = {}
    for field_name in _INPUT_FIELDS:
        set_clauses.append(f"{field_name} = %({field_name})s")
        params[field_name] = getattr(record, field_name)
    set_clauses.append("content_hash = %(content_hash)s")
    params["content_hash"] = content_hash
    if embedding is not None:
        set_clauses.append("embedding = %(embedding)s::vector")
        params["embedding"] = _as_vector(embedding)
    return set_clauses, params


def _set_status(
    conn: psycopg.Connection,
    record_id: UUID,
    status: Status,
    action: str,
    actor: str,
) -> RecordOut:
    """Common path for archive/restore: set ``status`` and write an audit row.

    The status change runs inside a ``try`` so that a ``content_hash``
    collision — e.g. restoring a record whose hash now clashes with another
    active row — is caught and re-raised as :class:`DuplicateError`.
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"SELECT {', '.join(_RECORD_COLUMNS)} FROM records WHERE id=%s",
            (str(record_id),),
        )
        current = cur.fetchone()
        if current is None:
            raise NotFoundError(record_id)

        content_hash = current["content_hash"]
        try:
            cur.execute(
                f"UPDATE records SET status=%(status)s "
                f"WHERE id=%(id)s RETURNING {', '.join(_RECORD_COLUMNS)}",
                {"status": status, "id": str(record_id)},
            )
            row = cur.fetchone()
        except psycopg.errors.IntegrityError as exc:
            conn.rollback()
            existing = _find_active_by_hash(
                conn, content_hash, exclude_id=record_id
            )
            if existing is None:
                raise RuntimeError(
                    "duplicate hash collision but no active record found"
                ) from exc
            raise DuplicateError(existing["id"], existing["created_at"]) from None
    audit(conn, record_id, action, {}, actor)
    return _row_to_record(row)


def _find_active_by_hash(
    conn: psycopg.Connection,
    content_hash: str,
    exclude_id: Optional[UUID] = None,
) -> Optional[dict]:
    """Return the ``{id, created_at}`` of the active record with ``content_hash``.

    ``exclude_id`` skips a specific row — the record being inserted/updated or
    reactivated by its own restore, whose hash legitimately reappears in the
    unique index and must not be mistaken for a distinct duplicate.
    """
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        if exclude_id is None:
            # Match a sentinel that no real UUID can equal, so the
            # ``id <> %(exclude_id)`` clause stays uniformly named and we avoid
            # mixing positional / named placeholders on the base query.
            exclude_id = UUID(int=0)
        cur.execute(
            "SELECT id, created_at FROM records "
            "WHERE content_hash=%(content_hash)s AND status='active' "
            "AND id <> %(exclude_id)s LIMIT 1",
            {"content_hash": content_hash, "exclude_id": str(exclude_id)},
        )
        return cur.fetchone()
