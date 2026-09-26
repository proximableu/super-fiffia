"""Records submission pipeline — the single path both the WebUI and the REST
API go through (``CONTRACT.md`` §9, ``AGENT.md`` T1.5).

The pipeline is deliberately tiny because it is the one place a mistake is
expensive:

    submit(payload) → validate taxonomy → hash → embed → insert
    update_record(record_id, payload) → validate → hash → (re-embed) → update

``bulk(items)`` runs one independent submit per item so a single bad record
(taxonomy, a colliding hash, a DB hiccup) isolates itself in that item's
transaction instead of aborting the whole batch (``CONTRACT.md`` §10).

Every failure is typed so callers can render it directly:
:exc:`InvalidTaxonomyError` → 422, :exc:`DuplicateError` → 409,
:exc:`FileNotFoundError` → 404.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.db import _checkout, _release
from app.embedding import EMBED_DIM, EMBED_MODEL, embed
from app.hashing import content_hash
from app.records_repo import (
    DuplicateError,
    RecordIn,
    RecordOut,
    _find_active_by_hash,
    archive,
    get,
    insert,
    restore,
    update,
)
from app.taxonomy import is_valid

logger = logging.getLogger(__name__)


class InvalidTaxonomyError(RuntimeError):
    """The submitted ``(category, product, article_number)`` is not valid.

    Carries the human message the caller renders into a ``422`` body.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DuplicateInfo:
    """Value object mirroring ``CONTRACT.md`` §6 ``DuplicateInfo``."""

    def __init__(self, existing_id: UUID, created_at) -> None:
        self.existing_id = existing_id
        self.created_at = created_at


# --------------------------------------------------------------------------- #
# Submission
# --------------------------------------------------------------------------- #


def _validate(record: RecordIn) -> None:
    """Raise :exc:`InvalidTaxonomyError` if the taxonomy fields do not line up."""
    if not is_valid(record.category, record.product, record.article_number):
        suffix = f" / {record.article_number}" if record.article_number else ""
        raise InvalidTaxonomyError(
            f"invalid taxonomy: {record.category} / {record.product}{suffix} "
            "is not in the configured taxonomy"
        )


def submit(payload: RecordIn, actor: str = "web") -> RecordOut:
    """Validate → hash → embed → insert, inside one transaction.

    Returns the stored :class:`RecordOut`. A duplicate (an active row already
    carrying the same ``content_hash``) raises :exc:`DuplicateError` with the
    existing record's id; the API maps it to ``409``.

    :param payload: the incoming record.
    :param actor:    the user / client id recorded on the row and audit log.
    :raises InvalidTaxonomyError: on an out-of-vocabulary taxonomy triple.
    :raises DuplicateError: on a hash collision with an active record.
    :raises RuntimeError: on DB failures (transaction rolled back first).
    """
    _validate(payload)
    digest = content_hash(
        payload.failure_description, payload.solution_description
    )
    embedding = embed([payload.failure_description])[0]

    conn = _checkout()
    try:
        # A unique index makes the insert race-safe; the audit row is written by
        # ``insert`` on success, so nothing is logged when a duplicate is found.
        result = insert(
            conn,
            payload,
            content_hash=digest,
            embedding=embedding,
            embed_model=EMBED_MODEL,
            embed_dim=EMBED_DIM,
            actor=actor,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        _release(conn)
        raise
    _release(conn)
    return result


def check_duplicate(failure: str, solution: str) -> DuplicateInfo | None:
    """Return the active record with this ``content_hash`` if one exists.

    Pure read; used by the ``POST /api/records/check-duplicate`` pre-check so
    the form can warn before submitting. The dedup index is never weakened.
    """
    digest = content_hash(failure, solution)
    conn = _checkout()
    try:
        existing = _find_active_by_hash(conn, digest)
        # A SELECT opens a transaction; close it here so the returned pooled
        # connection is not left idle-in-transaction holding an ACCESS SHARE
        # lock on ``records`` that would block the next test's TRUNCATE.
        conn.commit()
        if existing is None:
            return None
        return DuplicateInfo(existing["id"], existing["created_at"])
    finally:
        _release(conn)


# --------------------------------------------------------------------------- #
# Mutation
# --------------------------------------------------------------------------- #


def update_record(
    record_id: UUID, payload: RecordIn, actor: str = "web"
) -> RecordOut:
    """Validate → hash → (re-embed) → update, inside one transaction.

    The failure embedding is recomputed **only** when ``failure_description``
    changed — a category tweak must not invalidate the vector. A new
    ``content_hash`` that collides with another active record raises
    :exc:`DuplicateError`; a missing record raises :exc:`FileNotFoundError`.

    :raises InvalidTaxonomyError: on an out-of-vocabulary taxonomy triple.
    :raises FileNotFoundError: if ``record_id`` resolves to no row.
    :raises DuplicateError: on a hash collision with an active record.
    """
    _validate(payload)
    digest = content_hash(
        payload.failure_description, payload.solution_description
    )

    conn = _checkout()
    try:
        existing = get(conn, record_id)
        if existing is None:
            conn.rollback()
            raise FileNotFoundError(f"record {record_id} not found")

        embedding = (
            embed([payload.failure_description])[0]
            if payload.failure_description != existing.failure_description
            else None
        )

        updated = update(
            conn,
            record_id,
            payload,
            content_hash=digest,
            embedding=embedding,
            actor=actor,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        _release(conn)
        raise
    _release(conn)
    return updated


def archive_record(record_id: UUID, actor: str = "web") -> RecordOut:
    """Soft-delete the record; raises :exc:`FileNotFoundError` if it is absent."""
    conn = _checkout()
    try:
        existing = get(conn, record_id)
        if existing is None:
            conn.rollback()
            raise FileNotFoundError(f"record {record_id} not found")
        updated = archive(conn, record_id, actor)
        conn.commit()
    except Exception:
        conn.rollback()
        _release(conn)
        raise
    _release(conn)
    return updated


def restore_record(record_id: UUID, actor: str = "web") -> RecordOut:
    """Restore the record; raises :exc:`FileNotFoundError` / :exc:`DuplicateError`."""
    conn = _checkout()
    try:
        existing = get(conn, record_id)
        if existing is None:
            conn.rollback()
            raise FileNotFoundError(f"record {record_id} not found")
        updated = restore(conn, record_id, actor)
        conn.commit()
    except Exception:
        conn.rollback()
        _release(conn)
        raise
    _release(conn)
    return updated


# --------------------------------------------------------------------------- #
# Bulk import
# --------------------------------------------------------------------------- #


def bulk(items: list[RecordIn], actor: str = "web") -> dict:
    """Bulk-import ``items``, one item per transaction.

    Each item is processed in isolation, so one bad item (bad taxonomy, a
    colliding hash, a missing id, a DB error) is recorded and skipped instead of
    aborting the whole batch. A :class:`~app.api.RecordBulkItem` with an ``id``
    updates that existing record via :func:`update_record`; one without inserts a
    new record via :func:`submit`. An item whose content already exists elsewhere
    is reported as an ``"duplicate"`` error and counted as ``updated`` — it is
    never silently turned into a second row.

    :return: ``{"created": int, "updated": int, "errors": [dict]}``` where each
        error is ``{"index": int, "code": str, "message": str}```.
    """
    created = 0
    updated = 0
    errors: list[dict] = []

    for index, item in enumerate(items):
        has_id = getattr(item, "id", None) is not None
        try:
            record = (
                update_record(item.id, item, actor=actor)
                if has_id
                else submit(item, actor)
            )
            if has_id:
                updated += 1
            else:
                created += 1
            logger.info(
                "bulk item %d %s record %s",
                index,
                "updated" if has_id else "created",
                record.id,
            )
        except InvalidTaxonomyError as exc:
            errors.append(
                {"index": index, "code": "invalid_taxonomy", "message": str(exc)}
            )
        except DuplicateError as exc:
            updated += 1
            errors.append(
                {
                    "index": index,
                    "code": "duplicate",
                    "message": f"duplicate of {exc.existing_id}",
                }
            )
        except FileNotFoundError as exc:
            errors.append(
                {"index": index, "code": "not_found", "message": str(exc)}
            )
        except Exception as exc:  # noqa: BLE001 - isolate a single bad item
            errors.append({"index": index, "code": "internal", "message": str(exc)})

    duplicates = sum(1 for e in errors if e["code"] == "duplicate")
    logger.info(
        "bulk ingest finished: %d created, %d updated, %d duplicates, %d errors",
        created,
        updated,
        duplicates,
        len(errors),
    )
    return {"created": created, "updated": updated, "errors": errors}
