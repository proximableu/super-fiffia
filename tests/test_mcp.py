"""Tests for the MCP layer (``app.mcp``), covering findings M1–M9, M12, M14, M16.

These exercise the tool implementations directly rather than over the HTTP edge:
the tools are plain callables registered with FastMCP, so the suite imports
``app.mcp`` and calls ``search`` / ``list_records`` / ``get_record`` / ``chat`` /
``clear_session`` with the argument names MCP clients use. Retrieval and the
orchestrator are stubbed so no database / Ollama is required — the RRF sort
correction (M1) is asserted against scripted hits.

The error envelope mirrors the REST API (``{"error": {"code", "message"}}``).
"""

from __future__ import annotations

import pytest

from app import mcp as mcp_mod
from app.chat import ChatResponse
from app.records_repo import Hit
from app.chat import ChatResponse


def _hit(source: str, score: float) -> Hit:
    """Build a :class:`Hit` for scripted retrieval results."""
    return Hit(id="11111111-1111-1111-1111-111111111111", source=source, score=score)


# --------------------------------------------------------------------------- #
# search — validation matrix
# --------------------------------------------------------------------------- #


def test_search_rejects_empty_query() -> None:
    """A whitespace-only query is rejected with a validation envelope (M2)."""
    result = mcp_mod.search(query="   ")
    assert result == {"error": {"code": "validation", "message": "query must not be empty"}}


def test_search_rejects_unknown_source() -> None:
    """A source outside {both, records, rag} is rejected (M2)."""
    result = mcp_mod.search(query="hydraulic failure", source="all")
    assert result["error"]["code"] == "validation"
    assert "unknown source" in result["error"]["message"]


def test_search_rejects_top_k_out_of_range() -> None:
    """top_k outside [1, 100] is rejected rather than clamped (M5)."""
    assert mcp_mod.search(query="x", top_k=0)["error"]["code"] == "validation"
    assert mcp_mod.search(query="x", top_k=101)["error"]["code"] == "validation"


def test_search_rejects_nonexistent_records_by_id() -> None:
    """A record id that does not resolve returns a not_found envelope (M6)."""
    result = mcp_mod.get_record("00000000-0000-0000-0000-000000000000")
    assert result["error"]["code"] == "not_found"


def test_search_rejects_bad_uuid() -> None:
    """A non-UUID record id is rejected with a validation envelope (M6)."""
    result = mcp_mod.get_record("not-a-uuid")
    assert result["error"]["code"] == "validation"


def test_search_rejects_zero_limit() -> None:
    """A limit below 1 is rejected rather than clamped (M4)."""
    result = mcp_mod.list_records(limit=0)
    assert result["error"]["code"] == "validation"


def test_search_rejects_limit_too_large() -> None:
    """A limit above 500 is rejected rather than clamped (M4)."""
    result = mcp_mod.list_records(limit=501)
    assert result["error"]["code"] == "validation"


def test_search_rejects_negative_offset() -> None:
    """A negative offset is rejected (M4)."""
    result = mcp_mod.list_records(offset=-1)
    assert result["error"]["code"] == "validation"


def test_search_rejects_invalid_status() -> None:
    """A status outside {active, archived} is rejected (M3)."""
    result = mcp_mod.list_records(status="activee")
    assert result["error"]["code"] == "validation"
    assert "status" in result["error"]["message"]


def test_search_rejects_blank_session_token() -> None:
    """A whitespace-only session token is rejected (M8b)."""
    result = mcp_mod.chat(message="hi", session_token="   ")
    assert result["error"]["code"] == "validation"


def test_search_rejects_oversized_session_token() -> None:
    """A session token longer than 128 chars is rejected (M8b)."""
    result = mcp_mod.chat(message="hi", session_token="x" * 129)
    assert result["error"]["code"] == "validation"


def test_search_rejects_lang_that_not() -> None:
    """A lang outside {sv, en} is rejected (M7)."""
    result = mcp_mod.chat(message="hi", lang="de")
    assert result["error"]["code"] == "validation"


# --------------------------------------------------------------------------- #
# search — RRF correctness (M1)
# --------------------------------------------------------------------------- #


def test_search_ranks_by_score_desc_both_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Combined records + RAG hits are returned sorted by score descending (M1)."""
    monkeypatch.setattr(
        mcp_mod, "retrieve_fs", lambda scope, query, top_k: [_hit("records", 0.5)]
    )
    monkeypatch.setattr(
        mcp_mod, "retrieve_rag", lambda query, top_k, scope: [_hit("rag", 0.9), _hit("rag", 0.3)]
    )

    result = mcp_mod.search(query="hydraulic failure", source="both")

    assert result["source"] == "both"
    scores = [h["score"] for h in result["results"]]
    assert scores == sorted(scores, reverse=True)
    assert scores == [0.9, 0.5, 0.3]


def test_search_ranks_by_score_desc_single_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-source query is also returned sorted by score descending (M1)."""
    monkeypatch.setattr(
        mcp_mod, "retrieve_fs", lambda scope, query, top_k: [_hit("records", 0.2), _hit("records", 0.8)]
    )

    result = mcp_mod.search(query="hydraulic failure", source="records")

    assert result["source"] == "records"
    assert [h["score"] for h in result["results"]] == [0.8, 0.2]


def test_search_records_source_omits_rag(monkeypatch: pytest.MonkeyPatch) -> None:
    """source='records' excludes RAG hits (M1)."""
    monkeypatch.setattr(
        mcp_mod, "retrieve_fs", lambda scope, query, top_k: [_hit("records", 0.5)]
    )
    monkeypatch.setattr(
        mcp_mod, "retrieve_rag", lambda query, top_k, scope: [_hit("rag", 0.9)]
    )

    result = mcp_mod.search(query="x", source="records")

    assert {h["source"] for h in result["results"]} == {"records"}


# --------------------------------------------------------------------------- #
# chat — return envelope (M9)
# --------------------------------------------------------------------------- #


def test_chat_success_returns_answer_sources_turns_used(monkeypatch) -> None:
    """A successful chat turn returns answer/sources/turns_used and persists history."""
    monkeypatch.setattr(
        mcp_mod,
        "run_chat",
        lambda scope, messages, lang, session_token: ChatResponse(
            answer="an answer",
            sources=[_hit("records", 0.9)],
            turns_used=3,
        ),
    )

    result = mcp_mod.chat(message="hi", session_token="abc")

    assert result["answer"] == "an answer"
    assert result["turns_used"] == 3
    hit = result["sources"][0]
    # Only the discriminating hit fields are asserted; the rest of the Hit are
    # Optional and default to None in model_dump().
    assert str(hit["id"]) == "11111111-1111-1111-1111-111111111111"
    assert hit["source"] == "records"
    assert hit["score"] == 0.9


def test_chat_returns_exception_class_name_on_error(monkeypatch) -> None:
    """An agent failure surfaces an internal envelope with the exception class name (M9)."""
    def boom(scope, messages, lang, session_token):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_mod, "run_chat", boom)

    result = mcp_mod.chat(message="hi", session_token="abc")
    assert result == {"error": {"code": "internal", "message": "RuntimeError"}}


# --------------------------------------------------------------------------- #
# clear_session — return envelope (M14)
# --------------------------------------------------------------------------- #


def test_clear_session_returns_session_cleared_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """clear_session returns {session, cleared=True} when a session existed (M14)."""
    monkeypatch.setattr(mcp_mod, "clear_session_tool", lambda token: True)

    result = mcp_mod.clear_session(session_token="abc")
    assert result == {"session": "abc", "cleared": True}


def test_clear_session_returns_cleared_false_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """clear_session returns cleared=False when no session existed (M14)."""
    monkeypatch.setattr(mcp_mod, "clear_session_tool", lambda token: False)

    result = mcp_mod.clear_session(session_token="abc")
    assert result == {"session": "abc", "cleared": False}
