"""Smoke tests for the REST API (``app.api``).

These cover the request boundary that ``tests/test_records_service.py`` does not:
that the FastAPI app builds, the health route answers, ``POST /api/records``
round-trips a row, and ``POST /api/records/bulk`` returns the counts the
contract specifies (``CONTRACT.md`` §10).

The ``TestClient`` drives the real ``create_app()``; only the embedding call is
faked so the suite runs without a live Ollama endpoint. Every test talks to the
dedicated test database (``conftest.py``), which is truncated before each case.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi.routing import APIRoute

from app.api import create_app
from app.api import _health
from app.records_repo import RecordIn


# A 1024-dimensional fake embedding so submit/bulk runs with no live Ollama
# and still fits the ``embedding vector(1024)`` column.
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
# app / health
# --------------------------------------------------------------------------- #


def test_app_builds_with_expected_routes() -> None:
    """``create_app()`` returns a FastAPI app mounting the documented routes."""
    app = create_app()
    routes = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/health" in routes
    assert "/api/records" in routes
    assert "/api/records/bulk" in routes
    assert "/api/taxonomy/categories" in routes


def test_health_route_reports_llm_and_embed_models() -> None:
    """``GET /api/health`` reflects the configured model names."""
    body = _health()
    assert body["llm_model"] == "gemma4:e4b"
    assert body["embed_model"] == "snowflake-arctic-embed2:568m"


# --------------------------------------------------------------------------- #
# submit round-trip (CONTRACT.md §10)
# --------------------------------------------------------------------------- #


@patch("app.records_service.embed", _fake_embed)
def test_submit_roundtrip() -> None:
    """``POST /api/records`` creates a row and echoes the stored record."""
    client = TestClient(create_app())
    resp = client.post("/api/records", json=_record().model_dump())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["category"] == "hydraulics"
    assert body["status"] == "active"

    # a follow-up duplicate pre-check confirms the row now exists.
    dup = client.post(
        "/api/records/check-duplicate",
        json={"failure_description": "F", "solution_description": "S"},
    )
    assert dup.status_code == 200
    assert dup.json()["duplicate"] is True


# --------------------------------------------------------------------------- #
# bulk endpoint (fix 3.4: duplicate count must be reported)
# --------------------------------------------------------------------------- #


@patch("app.records_service.embed", _fake_embed)
def test_bulk_reports_created_and_duplicates() -> None:
    """``POST /api/records/bulk`` reports ``created``/``updated``/``errors``.

    The first row is created; a second identical row is a duplicate. The
    duplicate count (returned as ``updated``) must be reported so a client can
    see how many items were already present (``CONTRACT.md`` §10).
    """
    client = TestClient(create_app())
    client.post("/api/records", json=_record().model_dump())
    items = [_record(), _record(failure_description="G")]
    resp = client.post(
        "/api/records/bulk",
        json={"items": [item.model_dump() for item in items]},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 1
    assert body["updated"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["code"] == "duplicate"


@patch("app.records_service.embed", _fake_embed)
def test_bulk_isolates_invalid_taxonomy() -> None:
    """A bad item in the batch is reported, not fatal."""
    client = TestClient(create_app())
    items = [_record(category="NOPE"), _record(failure_description="G")]
    resp = client.post(
        "/api/records/bulk",
        json={"items": [item.model_dump() for item in items]},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["code"] == "invalid_taxonomy"
