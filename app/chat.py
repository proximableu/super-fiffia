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
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.config import settings
from app.agent import run_agent
from app.records_repo import ChatTurn, Hit, Scope

if TYPE_CHECKING:
    from collections.abc import Sequence
    from app.agent import AgentOutcome

logger = logging.getLogger(__name__)

# Per-session in-memory conversation history: ``{session_token: [ChatTurn, ...]}``.
# Cleared by :func:`clear_session` and lost on process restart. History is lossy:
# only the last ``MAX_HISTORY_TURNS`` turns of a session are retained (older turns
# are dropped, not just the assistant answers) — see :func:`_trim_history`.
_history: dict[str, list[ChatTurn]] = {}

# Upper bound on the number of turns kept in a session's in-memory history. We
# keep the most recent ``MAX_HISTORY_TURNS`` turns; anything older is trimmed on
# the next turn so a long-lived session cannot grow the cache without bound.
MAX_HISTORY_TURNS = 20


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
    history.extend(messages)

    outcome = run_agent(
        scope, list(history), lang, budget=settings.agent.max_turns
    )

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
    """Reset the in-memory history for a session (the webui's ``/chat/clear``)."""
    popped = _history.pop(session_token, None)
    if popped:
        logger.info("chat history cleared for session %r", session_token)
