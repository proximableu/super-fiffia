"""Tests for the records repository layer (``CONTRACT.md`` §9, T1.4).

The fixture applies ``migrations/0001_init.sql`` and opens a connection to the
dedicated test database, so every test exercises the real ``records`` /
``records_audit`` schema (dedup unique index, audit rows, ``vector(768)``
binding).

Unlike the service tests these hit ``records_repo`` directly, so there is no
Ollama to fake: a valid 768-d embedding is built once and passed straight into
``insert`` / ``update``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from app.records_repo import (
    DuplicateError,
    NotFoundError,
    RecordIn,
    RecordOut,
    Scope,
    _as_vector,
    archive,
    get,
    insert,
    list_records,
    restore,
    update,
)

# A 1024-dimensional fake embedding so the pipeline runs without a live Ollama
# and the row still fits the ``embedding vector(1024)`` column.
_EMBEDDING = [0.1] * 1024
EMBED_MODEL = "test-model"
EMBED_DIM = 1024


def _record(**kw) -> RecordIn:
    base = dict(
        category="hydraulics",
        product="valve_b",
        article_number="200-010",
        failure_description="Pump loses pressure",
        solution_description="Replace the seal ring",
        source="test",
    )
    base.update(kw)
    return RecordIn(**base)


def _insert(conn: psycopg.Connection, **kw) -> RecordOut:
    """Insert *and commit* so rows persist for later assertions.

    Every call gets a distinct content hash so a second ``_insert`` (which most
    tests make) cannot collide with the dedup unique index and raise a
    ``DuplicateError``.
    """
    out = insert(
        conn,
        _record(**kw),
        content_hash=str(uuid4()).replace("-", "")[:32],
        embedding=_EMBEDDING,
        embed_model=EMBED_MODEL,
        embed_dim=EMBED_DIM,
        actor="tester",
    )
    conn.commit()
    return out


def _audit_rows(conn: psycopg.Connection, record_id: UUID) -> list[dict]:
    cur = conn.execute(
        """
        SELECT id, action, changed, actor, at
        FROM records_audit
        WHERE record_id = %s
        ORDER BY id
        """,
        (record_id,),
    )
    return [
        {
            "id": r[0],
            "action": r[1],
            "changed": r[2],
            "actor": r[3],
            "at": r[4],
        }
        for r in cur.fetchall()
    ]


# --------------------------------------------------------------------------- #
# _as_vector (internal helper)
# --------------------------------------------------------------------------- #


def test_as_vector_wires_a_list_into_the_vector_literal():
    assert _as_vector([0.1, -0.2, 3.0]) == "[0.1,-0.2,3.0]"
    assert _as_vector([1]) == "[1]"


# --------------------------------------------------------------------------- #
# insert
# --------------------------------------------------------------------------- #


def test_insert_persists_row_and_rows_audit(db_conn):
    out = _insert(db_conn)

    assert isinstance(out, RecordOut)
    assert out.category == "hydraulics"
    assert out.product == "valve_b"
    assert out.article_number == "200-010"
    assert out.source == "test"
    assert out.status == "active"
    assert out.embed_model == EMBED_MODEL
    assert out.embed_dim == EMBED_DIM
    assert isinstance(out.id, UUID)
    assert isinstance(out.created_at, datetime)

    # the row is really in the DB (committed by ``_insert``)
    row = get(db_conn, out.id)
    assert row is not None
    assert row.id == out.id

    # exactly one audit row, action 'create', written with the actor
    audit = _audit_rows(db_conn, out.id)
    assert len(audit) == 1
    assert audit[0]["action"] == "create"
    assert audit[0]["changed"] == {}
    assert audit[0]["actor"] == "tester"
    assert audit[0]["at"] is not None


# --------------------------------------------------------------------------- #
# duplicate detection
# --------------------------------------------------------------------------- #


def test_duplicate_second_insert_raises_duplicate_error_with_existing(db_conn):
    first = insert(
        db_conn,
        _record(),
        content_hash="cafe" * 8,
        embedding=_EMBEDDING,
        embed_model=EMBED_MODEL,
        embed_dim=EMBED_DIM,
        actor="tester",
    )
    db_conn.commit()  # persist the first row so the second can collide

    with pytest.raises(DuplicateError) as excinfo:
        insert(
            db_conn,
            _record(),  # identical hash on purpose
            content_hash="cafe" * 8,
            embedding=_EMBEDDING,
            embed_model=EMBED_MODEL,
            embed_dim=EMBED_DIM,
            actor="tester",
        )
    db_conn.rollback()  # the aborted insert left a rolled-back transaction

    exc = excinfo.value
    assert isinstance(exc.existing_id, UUID)
    assert exc.existing_id == first.id
    assert exc.created_at == first.created_at
    assert str(first.id) in str(exc)

    # exactly one active row survives the duplicate
    rows, total = list_records(db_conn, None, "active", None, 100, 0)
    assert total == 1
    assert rows[0].id == first.id


# --------------------------------------------------------------------------- #
# get
# --------------------------------------------------------------------------- #


def test_get_returns_none_for_missing_id(db_conn):
    assert get(db_conn, uuid4()) is None


def test_get_returns_row_for_inserted_id(db_conn):
    out = _insert(db_conn)
    again = get(db_conn, out.id)
    assert again is not None
    assert again.id == out.id
    assert again.failure_description == "Pump loses pressure"


# --------------------------------------------------------------------------- #
# list_records
# --------------------------------------------------------------------------- #


def test_list_records_filters_by_scope_and_counts(db_conn):
    a = _insert(db_conn, article_number="100-001", category="hydraulics")
    b = _insert(db_conn, article_number="100-002", category="pneumatics")
    c = _insert(db_conn, article_number="100-003", category="pneumatics")
    d = _insert(db_conn, article_number="100-004", category="pneumatics")

    rows, total = list_records(db_conn, None, "active", None, 100, 0)
    assert total == 4
    assert {r.id for r in rows} == {a.id, b.id, c.id, d.id}

    rows, total = list_records(
        db_conn, Scope(category="pneumatics"), "active", None, 100, 0
    )
    assert total == 3
    assert {r.id for r in rows} == {b.id, c.id, d.id}

    # scope by article_number
    rows, total = list_records(
        db_conn, Scope(article_number="100-001"), "active", None, 100, 0
    )
    assert total == 1
    assert rows[0].id == a.id


def test_list_records_excludes_archived_rows(db_conn):
    keep = _insert(db_conn, article_number="100-001")
    tomb = _insert(db_conn, article_number="100-002")
    archive(db_conn, tomb.id, "tester")
    db_conn.commit()

    rows, total = list_records(db_conn, None, "active", None, 100, 0)
    assert total == 1
    assert rows[0].id == keep.id

    rows, total = list_records(db_conn, None, "archived", None, 100, 0)
    assert total == 1
    assert rows[0].id == tomb.id


# --------------------------------------------------------------------------- #
# update
# --------------------------------------------------------------------------- #


def test_update_changes_fields_and_rows_audit_row(db_conn):
    out = _insert(db_conn, source="test")

    updated = update(
        db_conn,
        out.id,
        _record(source="updated", failure_description="Changed failure"),
        content_hash="0" * 32,
        embedding=None,  # only failure changed; caller passes None to skip re-embed
        actor="editor",
    )
    db_conn.commit()

    assert updated.source == "updated"
    assert updated.failure_description == "Changed failure"

    audit = _audit_rows(db_conn, out.id)
    actions = [r["action"] for r in audit]
    assert "create" in actions
    assert "update" in actions
    update_rows = [r for r in audit if r["action"] == "update"]
    assert len(update_rows) == 1
    changed = update_rows[0]["changed"]
    assert changed["source"] == {"old": "test", "new": "updated"}
    assert changed["failure_description"] == {
        "old": "Pump loses pressure",
        "new": "Changed failure",
    }
    assert update_rows[0]["actor"] == "editor"

    # the persisted row actually changed
    on_db = get(db_conn, out.id)
    assert on_db.source == "updated"


def test_update_only_rows_fields_that_actually_changed(db_conn):
    out = _insert(db_conn, source="test")
    update(
        db_conn,
        out.id,
        _record(source="test"),  # identical payload -> nothing changed
        content_hash="0" * 32,
        embedding=None,
        actor="editor",
    )
    db_conn.commit()

    audit = _audit_rows(db_conn, out.id)
    update_rows = [r for r in audit if r["action"] == "update"]
    assert len(update_rows) == 1
    assert update_rows[0]["changed"] == {}


def test_update_on_missing_id_raises_not_found(db_conn):
    with pytest.raises(NotFoundError) as excinfo:
        update(
            db_conn,
            uuid4(),
            _record(),
            content_hash="0" * 32,
            embedding=None,
            actor="editor",
        )
    db_conn.rollback()
    assert isinstance(excinfo.value.record_id, UUID)


def test_update_to_a_colliding_hash_raises_duplicate_error(db_conn):
    a = _insert(db_conn, source="keep")
    b = _insert(db_conn, source="other")  # distinct hash, both active

    with pytest.raises(DuplicateError):
        update(
            db_conn,
            b.id,
            _record(source="other"),
            content_hash=a.content_hash,  # collide with active `a`
            embedding=None,
            actor="editor",
        )
    db_conn.rollback()

    # nothing changed on the aborted update
    on_db = get(db_conn, b.id)
    assert on_db.source == "other"


# --------------------------------------------------------------------------- #
# archive / restore
# --------------------------------------------------------------------------- #


def test_archive_then_restore_round_trip(db_conn):
    out = _insert(db_conn)

    archived = archive(db_conn, out.id, "tester")
    db_conn.commit()
    assert archived.status == "archived"
    assert get(db_conn, out.id).status == "archived"
    assert [r["action"] for r in _audit_rows(db_conn, out.id)] == ["create", "archive"]

    restored = restore(db_conn, out.id, "tester")
    db_conn.commit()
    assert restored.status == "active"
    assert get(db_conn, out.id).status == "active"

    # the append-only audit log records all three transitions in order
    assert [r["action"] for r in _audit_rows(db_conn, out.id)] == [
        "create",
        "archive",
        "restore",
    ]


def test_archive_missing_raises_not_found(db_conn):
    with pytest.raises(NotFoundError):
        archive(db_conn, uuid4(), "tester")
    db_conn.rollback()


def test_restore_rejects_a_colliding_hash(db_conn):
    original = _insert(db_conn, source="keep")
    other = _insert(db_conn, source="other")

    # Give `other` the same content_hash as `original` (archived) so two active
    # rows would share a hash once `original` is restored.
    archive(db_conn, original.id, "tester")
    db_conn.commit()
    update(
        db_conn,
        other.id,
        _record(source="other"),
        content_hash=original.content_hash,
        embedding=None,
        actor="tester",
    )
    db_conn.commit()

    # Restoring `original` would make two active rows share its hash.
    with pytest.raises(DuplicateError):
        restore(db_conn, original.id, "tester")
    db_conn.rollback()

    # `other` kept its hash; `original` is still archived.
    assert get(db_conn, other.id).content_hash == original.content_hash
    assert get(db_conn, original.id).status == "archived"
