"""Tests for the records submission pipeline (``CONTRACT.md`` §9, T1.5).

The fixture applies the migration and opens a connection to the dedicated test
database, so every test exercises the real ``records`` schema (dedup index,
audit rows, ``_as_vector`` binding).

Embedding is faked so the pipeline runs without a live Ollama endpoint; the
fake returns a valid 768-d vector so the ``embedding vector(768)`` column
accepts the row.
"""

from __future__ import annotations
from unittest.mock import patch

from uuid import uuid4

import psycopg
import pytest

from app.records_repo import RecordIn
from app.records_service import (
    DuplicateError,
    DuplicateInfo,
    InvalidTaxonomyError,
    archive_record,
    bulk,
    check_duplicate,
    restore_record,
    submit,
    update_record,
)


# A 1024-dimensional fake embedding so the pipeline runs with no live Ollama
# and the row still fits the ``embedding vector(1024)`` column.
def _fake_embed(texts):
    return [[0.1] * 1024 for _ in texts]


def _record(**kw) -> RecordIn:
    base = dict(
        category="hydraulics",
        product="valve_b",
        article_number="200-010",
        failure_description="F",
        solution_description="S",
        source="test",
    )
    base.update(kw)
    return RecordIn(**base)


# --------------------------------------------------------------------------- #
# submit
# --------------------------------------------------------------------------- #


@patch("app.records_service.embed", _fake_embed)
def test_submit_persists_row(db_conn):
    out = submit(_record(), actor="test")

    assert out.category == "hydraulics"
    assert out.product == "valve_b"
    assert out.article_number == "200-010"
    assert out.source == "test"
    assert out.status == "active"
    assert out.created_at is not None

    # the row exists in the DB
    cur = db_conn.execute("SELECT id FROM records WHERE id = %s", (out.id,))
    assert cur.fetchone() is not None

    # an audit row was written on create
    cur = db_conn.execute(
        "SELECT action FROM records_audit WHERE record_id = %s", (out.id,)
    )
    actions = [r[0] for r in cur.fetchall()]
    assert "create" in actions


@patch("app.records_service.embed", _fake_embed)
def test_submit_duplicate_raises(db_conn):
    submit(_record(), actor="test")
    with pytest.raises(DuplicateError):
        submit(_record(), actor="test")


@patch("app.records_service.embed", _fake_embed)
def test_check_duplicate_returns_info_on_collision():
    submit(_record(), actor="test")
    info = check_duplicate("F", "S")
    assert isinstance(info, DuplicateInfo)
    assert info.existing_id is not None


def test_check_duplicate_none_when_absent():
    assert check_duplicate("absent", "failure") is None


def test_submit_invalid_taxonomy_raises():
    with pytest.raises(InvalidTaxonomyError):
        submit(_record(category="NOPE"))


# --------------------------------------------------------------------------- #
# update / archive / restore
# --------------------------------------------------------------------------- #


@patch("app.records_service.embed", _fake_embed)
def test_update_record_changes_source(db_conn):
    out = submit(_record(), actor="test")
    updated = update_record(out.id, _record(source="updated"), actor="test")
    assert updated.source == "updated"

    cur = db_conn.execute("SELECT source FROM records WHERE id = %s", (out.id,))
    assert cur.fetchone()[0] == "updated"


@patch("app.records_service.embed", _fake_embed)
def test_archive_then_restore(db_conn):
    out = submit(_record(), actor="test")
    archived = archive_record(out.id, actor="test")
    assert archived.status == "archived"

    restored = restore_record(out.id, actor="test")
    assert restored.status == "active"


def test_archive_missing_raises():
    with pytest.raises(FileNotFoundError):
        archive_record(uuid4(), actor="test")


def test_update_missing_raises():
    with pytest.raises(FileNotFoundError):
        update_record(uuid4(), _record(), actor="test")


# --------------------------------------------------------------------------- #
# bulk
# --------------------------------------------------------------------------- #


@patch("app.records_service.embed", _fake_embed)
def test_bulk_reports_duplicates(db_conn):
    submit(_record(), actor="test")
    items = [
        _record(),  # duplicate of the one above (same failure + solution)
        _record(failure_description="G"),  # fresh, unique failure → created
    ]
    outcome = bulk(items, actor="test")

    assert outcome["created"] == 1
    assert outcome["updated"] == 1
    assert len(outcome["errors"]) == 1
    assert outcome["errors"][0]["code"] == "duplicate"


@patch("app.records_service.embed", _fake_embed)
def test_bulk_isolates_bad_taxonomy():
    items = [_record(category="NOPE"), _record(failure_description="G")]
    outcome = bulk(items, actor="test")

    assert outcome["created"] == 1
    assert outcome["errors"][0]["code"] == "invalid_taxonomy"
