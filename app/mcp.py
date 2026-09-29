"""MCP (Model Context Protocol) tools for the F&S knowledge base.

This is an *additive* layer that exposes the same retrieval / record / chat
pipeline as MCP tools, so MCP clients (Claude Code, Cursor, Windsurf, …) can
drive super-fiffia alongside the existing WebUI (:9001) and API (:9000).

Design (see the research note in ``README.md``):

- We use an **explicit tool list** rather than auto-deriving tools from the
  OpenAPI schema. That lets us (a) choose exactly which tools MCP clients see,
  and (b) hide the state-mutating ``submit`` / ``update`` / ``archive`` /
  ``restore`` endpoints that an agent should not be calling.
- Tools reuse the same service / retrieval / chat modules the API and WebUI use,
  so they share one source of truth. There is no duplicate logic.
- Tools run on a **separate uvicorn port** (default 9002); the WebUI and API
  processes are never touched. The MCP app is mounted into a *fresh* FastAPI
  app whose lifespan is the FastMCP session manager (required for Streamable
  HTTP, otherwise the first request raises "Task group is not initialized").

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
import uuid
from typing import Optional

from fastmcp import FastMCP

from app.chat import chat as run_chat
from app.db import _checkout, _release
from app.records_repo import ChatTurn, Scope, get, list_records as list_records_repo, NotFoundError
from app.retrieval import retrieve_fs, retrieve_rag

logger = logging.getLogger(__name__)

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

    Hybrid retrieval combining the structured stored records and the RAG docs,
    fused by reciprocal rank fusion. Returns ranked hits ordered by score.

    Args:
        query: The natural-language question or failure description.
        source: ``"both"`` (records + RAG, fused), ``"records"`` (stored
            records only), or ``"rag"`` (RAG docs only).
        top_k: Maximum number of hits to return per leg.
        category: Optional taxonomy category filter.
        product: Optional taxonomy product filter.
        article_number: Optional article-number filter (records only).

    Returns:
        ``{"source": <resolved>, "scope": {...}, "results": [<hit>, ...]}``
        where each hit is the unified :class:`~app.records_repo.Hit` dict.
    """
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
        if record is None:
            raise NotFoundError(record_id)
    finally:
        _release(conn)
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

    The message is appended to the session's in-memory history and the agent
    reasons over the whole conversation, returning an answer plus the source
    hits it relied on.

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
        ``{"error": {"code": "internal", "message": ...}}`` on failure.
    """
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
        logger.error("mcp.chat failure for session %r: %s", session_token, exc)
        return {"error": {"code": "internal", "message": str(exc)}}
    return {
        "answer": response.answer,
        "sources": [hit.model_dump() for hit in response.sources],
        "turns_used": response.turns_used,
    }
