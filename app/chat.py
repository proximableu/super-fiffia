"""Chat orchestration (T3.2).

Bridges the request boundary and the agent turn loop: a scope plus the incoming
messages become a chat *turn* through :func:`run_agent`, which returns an
:class:`AgentOutcome`. This module keeps the per-session conversation history in
memory so follow-up turns carry the running context.

Conversation model (see `CONTRACT.md` §11 / `WEBUI.md` §4):

- Each request arrives with the *new* user turn(s) and a session token (the
  webui mints it into ``localStorage`` and posts it in the JSON request body).
- The incoming user turn(s) are appended to that session's history, and the full
  history is handed to :func:`run_agent` so the agent reasons over the running
  conversation.
- The assistant answer is appended back to the history as an assistant turn, so
  the next follow-up sees it.
- :func:`clear_session` drops a session's history (the webui's ``/chat/clear``
  route calls it).
- :func:`chat` keeps the history lossy: only the last ``MAX_HISTORY_TURNS`` turns
  of a session are retained; older turns are trimmed on the next turn so a
  long-lived session cannot grow the in-memory cache without bound.

History lives in a single module-level dict keyed by session token and is only
ever touched here; the Ollama calls :func:`run_agent` makes stay serialised
through ``app.ollama.OLLAMA_LOCK``.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.config import settings
from app.agent import run_agent
from app.records_repo import ChatTurn, Hit, Scope
from app.rag_routing import resolve_rag_first, resolve_records_first

if TYPE_CHECKING:
    from collections.abc import Sequence
    from app.agent import AgentOutcome

logger = logging.getLogger(__name__)

# Per-session in-memory conversation history, held in an :class:`OrderedDict`
# used as a least-recently-used cache (see M8): the most recently accessed
# session is moved to the end and the oldest is evicted once the session-count
# bound below is exceeded. Sessions are cleared by :func:`clear_session` and
# lost on process restart. Per-session history is lossy — only the last
# ``MAX_HISTORY_TURNS`` turns of a session are retained (older turns are dropped,
# not just the assistant answers) — see :func:`_trim_history`.
_history: OrderedDict[str, list[ChatTurn]] = OrderedDict()

# Upper bound on the number of turns kept in a session's in-memory history. We
# keep the most recent ``MAX_HISTORY_TURNS`` turns; anything older is trimmed on
# the next turn so a long-lived session cannot grow the cache without bound.
MAX_HISTORY_TURNS = 20

# Upper bound on the number of *sessions* retained in :data:`_history`. The cache
# is a least-recently-used store: every :func:`chat` / :func:`clear_session`
# access moves its session to the most-recently-used end, and once more than
# ``MAX_SESSIONS`` sessions are held the least-recently-used one is evicted. This
# bounds the memory of this module-level store regardless of how many distinct
# callers (MCP, API, WebUI) touch it in a single process.
MAX_SESSIONS = 64


def _trim_history(session_token: str, history: list[ChatTurn]) -> None:
    """Drop the oldest turns of a session so ``history`` keeps the last
    ``MAX_HISTORY_TURNS`` turns (lossy trim).

    Called after the assistant turn is appended. The agent already sees the full
    history for the turn it is answering, so trimming here only affects which
    context the *next* turn carries.
    """
    excess = len(history) - MAX_HISTORY_TURNS
    if excess > 0:
        del history[:excess]
        logger.info(
            "trimmed %d turn(s) from session %r (now %d)",
            excess,
            session_token,
            len(history),
        )


def _touch_and_bound(session_token: str) -> None:
    """Manage :data:`_history` bookkeeping: LRU placement plus the session bound.

    Moves ``session_token`` to the most-recently-used end of :data:`_history`
    (a session created by ``setdefault`` is simply added at the end) and evicts
    the least-recently-used sessions once the count exceeds :data:`MAX_SESSIONS`.
    Safe to call for a session that is about to be cleared (clearing just pops
    it again).
    """
    if session_token in _history:
        _history.move_to_end(session_token)
    while len(_history) > MAX_SESSIONS:
        evicted, _ = _history.popitem(last=False)
        logger.info("evicted least-recently-used session %r", evicted)


class ChatResponse(BaseModel):
    """Chat turn response contract (``{answer, sources, turns_used}``) — see
    `CONTRACT.md` §6. Owned here so this file stays self-contained; the request
    shapes (``ChatTurn`` / ``Scope``) live in ``records_repo``."""

    answer: str
    sources: list[Hit] = []
    turns_used: int

# The default session token used when the caller does not identify a session
# (the webui always passes one; the core stays callable without a token).
_DEFAULT_SESSION = "default"


def chat(
    scope: Scope | None,
    messages: Sequence[ChatTurn],
    lang: str,
    session_token: str = _DEFAULT_SESSION,
) -> ChatResponse:
    """Run one orchestrator turn: fold the user turn into history and run the agent.

    The incoming ``messages`` are the new user turn(s) for this request. They are
    appended to the session history, the whole history is passed to
    :func:`run_agent` under ``settings.agent.max_turns``, and the resulting
    assistant answer is appended back so follow-up turns see the full exchange.
    """
    history = _history.setdefault(session_token, [])
    _touch_and_bound(session_token)
    # Capture the pre-turn length before folding in the new user turn(s):
    # truncate back to this on the way out of the except if the agent raises.
    before = len(history)
    history.extend(messages)
    is_first_turn = len(history) == 0

    # Retrieval routing (highest-precedence marker wins):
    #   #fails tag   -> records first (both flags off — force the fs fallback)
    #   #docs tag    -> rag_only     (query RAG, never touch stored records)
    #   follow-up    -> rag_first    (query RAG, fall back to stored records)
    #   first Q1     -> records      (records-first; unchanged default)
    latest = messages[-1].content if messages else ""
    fails_tagged = resolve_records_first(latest)
    docs_tagged = resolve_rag_first(latest)
    if fails_tagged:
        rag_first, rag_only = False, False
    elif docs_tagged:
        rag_first, rag_only = False, True
    else:
        rag_first, rag_only = not is_first_turn, False

    try:
        outcome = run_agent(
            scope,
            list(history),
            lang,
            budget=settings.agent.max_turns,
            rag_first=rag_first,
            rag_only=rag_only,
        )
    except Exception:
        # M16: the user turn(s) were appended to history just above; the agent
        # raised before emitting an answer, so truncate back to the pre-turn
        # length before re-raising to keep history consistent.
        del history[before:]
        raise

    history.append(ChatTurn(role="assistant", content=outcome.answer))
    _trim_history(session_token, history)
    logger.info(
        "chat turn for session %r: %d turn(s), ended_with=%s",
        session_token,
        outcome.turns_used,
        outcome.ended_with,
    )
    return to_response(outcome)


def to_response(outcome: AgentOutcome) -> ChatResponse:
    """Adapt a :class:`AgentOutcome` into the response contract (``{answer, sources,
    turns_used}``)."""
    return ChatResponse(
        answer=outcome.answer,
        sources=list(outcome.sources),
        turns_used=outcome.turns_used,
    )


def clear_session(session_token: str = _DEFAULT_SESSION) -> None:
    """Reset the in-memory history for a session (the webui's ``/chat/clear``).

    Also evicts the least-recently-used sessions if the cache is over bound:
    :data:`MAX_SESSIONS`.
    """
    popped = _history.pop(session_token, None)
    if popped:
        logger.info("chat history cleared for session %r", session_token)
    _touch_and_bound(session_token)
