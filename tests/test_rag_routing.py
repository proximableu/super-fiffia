"""Tests for ``app/rag_routing.py``.

Covers the operator's turn-level signal that routes a chat turn between records
(fs) and RAG documentation. The policy enforced inside :func:`run_agent` lives in
``app/agent.py``; this module only asserts the detection.
"""

from __future__ import annotations

from app.rag_routing import MARKER, resolve_rag_first, resolve_records_first


def test_no_marker_is_records_first() -> None:
    assert resolve_rag_first("pump making noise") is False


def test_marker_triggers_rag_first_case_insensitive() -> None:
    assert resolve_rag_first("where is the component? #docs") is True
    assert resolve_rag_first("where is the component? #DOCS") is True


def test_marker_embedded_in_word_still_matches() -> None:
    # MARKER is a substring probe, not a token boundary — document that behaviour.
    assert resolve_rag_first("look #docs") is True


def test_fails_marker_routes_records_first() -> None:
    assert resolve_records_first("show me the fs failures") is False
    assert resolve_records_first("show fs failures #fails") is True
    assert resolve_records_first("#FAILS") is True


def test_fails_marker_embedded_in_word_still_matches() -> None:
    # Same substring-probe behaviour as #docs.
    assert resolve_records_first("look #fails") is True
