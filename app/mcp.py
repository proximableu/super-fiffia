"""MCP (Model Context Protocol) tools for the F&S knowledge base.

This is an *additive* layer that exposes the same retrieval / record / chat
pipeline as MCP tools, so MCP clients (Claude Code, Cursor, Windsurf, …) can
drive super-fiffia alongside the existing WebUI (:9001) and API (:9000).

See the "MCP" section in ``README.md`` for the operational overview (what the
layer is, the server port, the four tool names, read-only posture). Here we
record the design decisions that do not appear in the user guide.

Design

------

- We use an **explicit tool list** rather than auto-deriving tools from the
  OpenAPI schema. That lets us (a) choose exactly which tools MCP clients see,
  and (b) hide the state-mutating ``submit`` / ``update`` / ``archive`` /
  ``restore`` endpoints that an agent should not be calling.
- Tools reuse the same service / retrieval / chat modules the API and WebUI use,
  so they share one source of truth. There is no duplicate logic.
- Tools run on a **separate uvicorn port** (default 9002); the WebUI and API
  processes are never touched. The MCP app is created with
  ``mcp.http_app(path="/")`` — a Starlette app whose lifespan is the FastMCP
  session manager (required for Streamable HTTP, otherwise the first request
  raises "Task group is not initialized").

Read-only
---------

The surface is deliberately **read-only**: ``search``, ``list_records``,
``get_record``, ``chat`` and ``clear_session`` only read the knowledge base and
the running session. There is intentionally no tool for submitting, updating,
archiving or restoring records — submission stays on the WebUI/API, where the
taxonomy validation, dedup hashing, embedding and audit trail belong.

Tools
-----

- ``search``       — hybrid RRF search over stored records + RAG docs.
- ``list_records`` — list records with structured filters, optional free-text.
- ``get_record``   — fetch a single record by id.
- ``chat``         — run the agent over the running session history.

The error envelope mirrors the REST API (``{"error": {"code", "message"}}``) so
MCP tool results read consistently with ``/api/*``.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Optional

from fastmcp import FastMCP

from app.chat import clear_session as clear_session_tool
from app.chat import chat as run_chat
from app.db import _checkout, _release
from app.records_repo import ChatTurn, Scope, get, list_records as list_records_repo, NotFoundError
from app.retrieval import retrieve_fs, retrieve_rag

logger = logging.getLogger(__name__)


def _envelope(code: str, message: str) -> dict:
    """Build the module's error envelope (``{"error": {"code", "message"}}``)."""
    return {"error": {"code": code, "message": message}}


def _validate_session_token(token: str) -> dict | None:
    """Validate an MCP-bound session token.

    Clamps the token to what the MCP surface can safely carry: non-empty
    (after stripping), at most 128 characters. Returns a validation envelope on
    a rejected token, or ``None`` when the token is acceptable.
    """
    if not token.strip():
        return _envelope("validation", "session_token must not be empty")
    if len(token) > 128:
        return _envelope("validation", "session_token must be at most 128 characters")
    return None


# The MCP tools server. Named "super-fiffia" so tool discovery is self-documenting.
mcp = FastMCP("super-fiffia")


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
@mcp.tool()
def search(
    query: str,
    *,
    source: str = "both",
    top_k: int = 5,
    category: Optional[str] = None,
    product: Optional[str] = None,
    article_number: Optional[str] = None,
) -> dict:
    """Search the F&S knowledge base.

    Hybrid retrieval combining the structured stored records and the RAG docs.
    RRF fuses the vector and lexical legs *within* each source; the ``source``
    parameter selects which source(s) to run, and ``"both"`` concatenates the
    results of the two legs (the records leg and the RAG leg are each fused
    independently, then joined — they are **not** fused across the boundary).
    Returns ranked hits ordered by ``score`` descending.

    Args:
        query: The natural-language question or failure description (non-empty).
        source: ``"both"`` (records + RAG, concatenated), ``"records"`` (stored
            records only), or ``"rag"`` (RAG docs only).
        top_k: Maximum number of hits to return (``1`` to ``100``).
        category: Optional taxonomy category filter.
        product: Optional taxonomy product filter.
        article_number: Optional article-number filter (records only).

    Returns:
        ``{"source", "scope", "count", "results"}`` where ``results`` holds the
        unified :class:`~app.records_repo.Hit` dicts, sorted by ``score`` desc —
        or ``{"error": {"code": "validation", ...}}`` on a rejected input.
    """
    query_stripped = query.strip()
    if not query_stripped:
        return {"error": {"code": "validation", "message": "query must not be empty"}}
    if source not in ("both", "records", "rag"):
        return {
            "error": {
                "code": "validation",
                "message": f"unknown source: {source!r}",
            }
        }
    if not (1 <= top_k <= 100):
        return {
            "error": {
                "code": "validation",
                "message": "top_k must be between 1 and 100",
            }
        }

    scope = Scope(
        category=category,
        product=product,
        article_number=article_number,
    )

    if source in ("both", "records"):
        records_hits = retrieve_fs(scope, query, top_k)
    else:
        records_hits = []

    if source in ("both", "rag"):
        rag_hits = retrieve_rag(query, top_k, scope=scope)
    else:
        rag_hits = []

    if source == "both":
        combined = records_hits + rag_hits
    else:
        combined = records_hits if source == "records" else rag_hits

    combined.sort(key=lambda h: h.score, reverse=True)

    return {
        "source": source,
        "scope": scope.model_dump(),
        "count": len(combined),
        "results": [hit.model_dump() for hit in combined],
    }


# --------------------------------------------------------------------------- #
# list_records
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_records(
    *,
    category: Optional[str] = None,
    product: Optional[str] = None,
    article_number: Optional[str] = None,
    status: str = "active",
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List stored records with structured filters plus an optional free-text search.

    Args:
        category: Optional taxonomy category filter.
        product: Optional taxonomy product filter.
        article_number: Optional article-number filter.
        status: ``"active"`` or ``"archived"``.
        q: Optional free-text query (lexical ordering over the FTS index).
        limit: Maximum number of records to return.
        offset: Record offset for pagination.

    Returns:
        ``{"status": <status>, "total": <match_count>, "limit": <limit>,
        "offset": <offset>, "records": [<record>, ...]}``
        where each record is the :class:`~app.records_repo.RecordOut` dict.
    """
    if status not in ("active", "archived"):
        return {
            "error": {
                "code": "validation",
                "message": f"unknown status: {status!r}",
            }
        }
    if not (1 <= limit <= 500):
        return {
            "error": {
                "code": "validation",
                "message": "limit must be between 1 and 500",
            }
        }
    if offset < 0:
        return {
            "error": {"code": "validation", "message": "offset must not be negative"}
        }

    scope = Scope(
        category=category,
        product=product,
        article_number=article_number,
    )
    conn = _checkout()
    try:
        records, total = list_records_repo(
            conn,
            scope,
            status,
            q,
            limit=limit,
            offset=offset,
        )
    finally:
        _release(conn)
    return {
        "status": status,
        "total": total,
        "limit": limit,
        "offset": offset,
        "count": len(records),
        "records": [r.model_dump() for r in records],
    }


# --------------------------------------------------------------------------- #
# get_record
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_record(record_id: str) -> dict:
    """Fetch a single record by id.

    Args:
        record_id: The UUID of the record to fetch (as a string).

    Returns:
        The :class:`~app.records_repo.RecordOut` dict, or ``{"error":
        {"code": "not_found", "message": ...}}`` when the id does not resolve.
    """
    try:
        record_id = uuid.UUID(record_id)
    except (ValueError, AttributeError):
        return {"error": {"code": "validation", "message": f"invalid record id: {record_id!r}"}}

    conn = _checkout()
    try:
        record = get(conn, record_id)
    finally:
        _release(conn)
    if record is None:
        return {
            "error": {
                "code": "not_found",
                "message": f"record {record_id} not found",
            }
        }
    return record.model_dump()


# --------------------------------------------------------------------------- #
# chat
# --------------------------------------------------------------------------- #
@mcp.tool()
def chat(
    message: str,
    *,
    session_token: str = "default",
    category: Optional[str] = None,
    product: Optional[str] = None,
    article_number: Optional[str] = None,
    lang: str = "sv",
) -> dict:
    """Ask the F&S agent a question over the running session history.

    The message is folded into the session's in-memory history and the agent
    reasons over the whole conversation, returning an answer plus the source
    hits it relied on. The session history is held in-memory per process: it is
    **not** shared with the WebUI or API processes and is lost on restart.

    Routing (resolved by ``app.chat`` via ``app.rag_routing``): a message tagged
    ``#fails`` runs the stored-records leg first; one tagged ``#docs`` queries RAG
    first; a follow-up turn queries RAG and falls back to records; the first
    question runs records first. See ``app/chat.py``.

    Args:
        message: The new user question for this turn.
        session_token: Identifies the conversation; reuse it across turns so the
            agent carries context. Defaults to ``"default"``.
        category: Optional taxonomy category to scope retrieval.
        product: Optional taxonomy product to scope retrieval.
        article_number: Optional article-number to scope retrieval.
        lang: ``"sv"`` or ``"en"`` — the language the agent answers in.

    Returns:
        ``{"answer": <text>, "sources": [<hit>, ...], "turns_used": <n>}``, or
        ``{"error": {"code": "internal", "message": <exception class name>}}`` on
        failure.
    """
    if lang not in ("sv", "en"):
        return _envelope(
            "validation", f"lang must be \"sv\" or \"en\" (got {lang!r})"
        )
    rejected = _validate_session_token(session_token)
    if rejected is not None:
        return rejected
    try:
        scope = Scope(
            category=category,
            product=product,
            article_number=article_number,
        )
        response = run_chat(
            scope,
            [ChatTurn(role="user", content=message)],
            lang,
            session_token,
        )
    except Exception as exc:  # noqa: BLE001 - surface any agent failure as JSON
        logger.error("mcp.chat failure for session %r", session_token, exc_info=sys.exc_info())
        return _envelope("internal", type(exc).__name__)
    return {
        "answer": response.answer,
        "sources": [hit.model_dump() for hit in response.sources],
        "turns_used": response.turns_used,
    }


# --------------------------------------------------------------------------- #
# clear_session
# --------------------------------------------------------------------------- #
@mcp.tool()
def clear_session(*, session_token: str = "default") -> dict:
    """Clear the running session's in-memory history.

    The WebUI has an equivalent (``/chat/clear``); this gives MCP clients the same
    ability to drop a conversation so the next turn starts fresh.

    Args:
        session_token: The session to clear.

    Returns:
        ``{"session": <token>, "cleared": <bool>}`` — ``cleared`` is ``True``
        when a session existed and was emptied, ``False`` otherwise.
    """
    rejected = _validate_session_token(session_token)
    if rejected is not None:
        return rejected
    cleared = clear_session_tool(session_token)
    return {"session": session_token, "cleared": cleared}
