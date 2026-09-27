"""The agent turn loop (T3.1).

``run_agent`` implements the structured-output agent contract described in
`CONTRACT.md` §11 / `SPECIFICATIONS.md` §6. There is no native tool calling —
every agent step is a single Ollama ``chat_structured`` call whose raw JSON is
parsed into an :class:`AgentAction`. Based on the action the loop dispatches to
the retrieval tools, applies the results back into the prompt, and repeats —
bounded by a turn budget.

Loop contract (see the contract for the authoritative prose):

1. Build the context: a language-aware system prompt + the conversation history +
   the latest user message.
2. For each turn up to ``budget``: call ``chat_structured`` to obtain the next
   :class:`AgentAction`, log the turn (action, query, filters, source ids, turn
   index — NFR-8), and dispatch:
   * ``search_records`` -> ``retrieve_fs(action.filters or scope, query)``;
   * ``search_rag``     -> ``retrieve_rag(query)``;
   * ``ask_clarification`` -> return with ``ended_with='clarification'``;
   * ``final_answer``     -> return with ``ended_with='final_answer'``.
   Retrieved :class:`Hit` rows are deduped by id across turns.
3. If the budget is exhausted without a terminal action, force a best-effort
   final answer through ``chat()`` and return with ``ended_with='budget_exhausted'``.

Retrieval order is set by ``rag_first`` / ``rag_only`` (see :func:`run_agent`),
which are derived per turn from operator tags (``#docs``, ``#fails``) by
:mod:`app.rag_routing` in :func:`chat`.

A parse failure is retried once; a second failure forces ``final_answer``. The
loop therefore always returns and never exceeds ``budget`` turns (NFR-6).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from app.config import settings
from app.ollama import chat, chat_structured
from app.records_repo import AgentAction, Hit, Scope
from app.retrieval import retrieve_fs, retrieve_rag

if TYPE_CHECKING:
    from app.records_repo import ChatTurn

logger = logging.getLogger(__name__)

DEFAULT_BUDGET: int = 5

ENDED_WITH = Literal["final_answer", "clarification", "budget_exhausted"]


@dataclass
class AgentOutcome:
    """The result of an agent run.

    Attributes:
        answer: The agent's response. A clarification question arrives here as
            the answer text (per the contract).
        sources: The union of all :class:`Hit` rows retrieved this run,
            deduped by id.
        turns_used: How many structured LLM calls were made.
        ended_with: How the loop terminated.
    """

    answer: str
    sources: list[Hit] = field(default_factory=list)
    turns_used: int = 0
    ended_with: ENDED_WITH = "final_answer"


@dataclass
class _TurnLog:
    """One NFR-8 per-turn decision-log entry.

    Kept so the budget-exhausted message can summarize what the model tried
    before giving up.
    """

    turn: int
    action: str
    query: str | None
    filters: str | None


def _system_prompt(
    lang: str,
    scope: Scope | None,
    *,
    rag_first: bool = False,
    rag_only: bool = False,
) -> str:
    """Build the language-aware system prompt.

    Swedish is the default (``lang='sv'``); English is the fallback. The prompt
    instructs the model to answer in that language, prefer citing stored records
    over RAG documents, and ask a clarifying question when the failure is
    under-specified. It also includes the current scope note so the model knows
    which category/product/article_number are already selected.

    Retrieval order is governed by two flags: ``rag_only`` (the operator tagged
    the turn with ``#docs``, so only RAG docs are consulted) takes precedence over
    ``rag_first`` (RAG consulted before stored records). When both are false the
    default records-first order applies.

    Args:
        lang: ``"sv"`` (default) or ``"en"``; the agent answers in this language.
        scope: The current (never-``None``) scope, appended as a note.
        rag_first: When ``True`` RAG docs are consulted before stored records.
        rag_only: When ``True`` only RAG docs are consulted; stored records are
            never queried. Takes precedence over ``rag_first``.
    """
    if lang == "en":
        base = (
            "You are the Fiffia support agent. Answer the operator's question "
            "about a machine fault. "
        )
        if rag_only:
            base += (
                "Consult only RAG documentation; do not query stored records."
            )
        else:
            base += (
                "Cite RAG documentation before stored records, then stored records if "
                "documentation does not answer it."
                if rag_first
                else "Cite stored records before RAG documents."
            )
        base += (
            " When the failure is under-specified, ask one clarifying question. "
            "Never invent information."
        )
    else:
        base = (
            "Du är Fiffia-supportagenten. Svara operatörens fråga om ett "
            "maskfel. "
        )
        if rag_only:
            base += "Använd endast RAG-dokument; sök inte i lagrade poster."
        else:
            base += (
                "Hänvisa till RAG-dokument före lagrade poster, sedan lagrade poster "
                "om dokumenten inte besvarar den."
                if rag_first
                else "Hänvisa till lagrade postar före RAG-dokument."
            )
        base += (
            "Ställ en avklarande fråga om felet är för oklart. Hitta aldrig på "
            "information."
        )

    if scope is not None:
        parts = [f"{key}={value}" for key, value in scope.model_dump().items() if value]
        if parts:
            base += " Selected context: " + ", ".join(parts) + "."
    return base


def _last_user_message(messages: list["ChatTurn"]) -> str:
    """Return the text of the latest message (the final message is a user turn)."""
    if not messages:
        return ""
    return messages[-1].content


def _format_hits(hits: list[Hit]) -> str:
    """Render retrieved hits as a compact block for the next prompt turn."""
    if not hits:
        return "(no retrieval results)"
    lines = [f"{len(hits)} result(s):"]
    for hit in hits:
        if hit.source == "records":
            meta = hit.article_number or hit.category or "record"
            detail = hit.failure_description or ""
        else:
            meta = hit.source_file or hit.section_header or "document"
            detail = (hit.chunk_text or "").strip().replace("\n", " ")
            detail = detail[:200]
        lines.append(f"- #{hit.id} [{hit.source}] {meta}: {detail}")
    return "\n".join(lines)


def _as_messages(messages: list["ChatTurn"]) -> list[dict]:
    """Convert :class:`ChatTurn` history into OpenAI-style dicts."""
    return [
        {"role": turn.role, "content": turn.content} for turn in messages
    ]


def _force_final_answer(reason: str) -> AgentAction:
    """Return a forced ``final_answer`` :class:`AgentAction`.

    Used when the structured response cannot be produced or parsed; the reason
    is recorded in the action's ``thought`` so the decision is logged.
    """
    return AgentAction(
        thought=reason,
        action="final_answer",
        answer="",
    )


def _next_action(context: list[dict], turn: int) -> AgentAction:
    """Obtain the next :class:`AgentAction` for the model's turn.

    Calls ``chat_structured`` once, retrying once if the call fails or the JSON
    does not validate. A second failure returns a forced ``final_answer`` so the
    loop always makes progress (NFR-6).
    """

    def attempt() -> AgentAction:
        raw = chat_structured(context, AgentAction.model_json_schema())
        return AgentAction.model_validate_json(raw)

    try:
        return attempt()
    except Exception:  # noqa: BLE001 - surfaced below via the retry/forced answer
        logger.warning("structured call failed on turn %d; retrying", turn)
        try:
            return attempt()
        except Exception:  # noqa: BLE001 - forced final answer below
            logger.warning("structured call failed again on turn %d; forced answer", turn)
            return _force_final_answer(
                f"structured output failed after retry on turn {turn} "
                "(answering with best effort)"
            )


def _dispatch(
    action: AgentAction,
    scope: Scope,
    turn: int,
    *,
    rag_first: bool,
    rag_only: bool,
) -> list[Hit]:
    """Run the retrieval tool for a search action and log its result.

    Args:
        action: The parsed :class:`AgentAction`.
        scope: The current (never-``None``) scope, used to scope records search.
        turn: The 1-based turn index, for logging.
        rag_first: When ``True`` RAG docs are queried before stored records.
        rag_only: When ``True`` only RAG docs are queried; stored records are
            never searched regardless of the action.

    Returns:
        The retrieved :class:`Hit` rows (possibly empty).
    """
    query = action.query or ""
    if rag_only:
        # Operator forced documentation: RAG only, records are never searched.
        return retrieve_rag(query)
    if rag_first:
        # Operator tagged this turn: query RAG first and fill in records only
        # when RAG returns nothing — the records leg is the fallback here, not a
        # separate search the model has to trigger.
        hits = retrieve_rag(query)
        if hits:
            return hits
        return retrieve_fs(action.filters or scope, query)
    elif action.action == "search_records":
        hits = retrieve_fs(action.filters or scope, query)
    else:  # pragma: no cover - only search_rag reaches here
        hits = retrieve_rag(query)

    logger.info(
        "agent turn %d: action=%s query=%r filters=%s hits=%d",
        turn,
        action.action,
        query,
        action.filters,
        len(hits),
        stage="retrieval",
        turn=turn,
        action=action.action,
        query=query,
        filters=action.filters.model_dump() if action.filters is not None else None,
        source_ids=[str(h.id) for h in hits],
        scores=[round(h.score, 6) for h in hits],
    )
    return hits


def run_agent(
    scope: Scope | None,
    messages: list["ChatTurn"],
    lang: str,
    budget: int | None = DEFAULT_BUDGET,
    *,
    rag_first: bool = False,
    rag_only: bool = False,
) -> AgentOutcome:
    """Run the agent loop and return the outcome.

    Args:
        scope: The UI-selected category/product/article_number context (may be
            ``None``). Used to scope ``search_records`` when the model supplies no
            ``filters``.
        messages: Conversation history; the final message is the new user
            message.
        lang: ``"sv"`` (default) or ``"en"``; the agent answers in this language.
        budget: Maximum number of LLM turns (defaults to ``settings.agent.
            max_turns``).
        rag_first: When ``True`` RAG docs are consulted before stored records
            (enforced on session follow-ups). Defaults to records-first.
        rag_only: When ``True`` only RAG docs are consulted and stored records
            are never searched (enforced when the operator tags the turn
            ``#docs``). Takes precedence over ``rag_first``.

    Returns:
        An :class:`AgentOutcome` describing the answer, sources and how the loop
        terminated.
    """
    if budget is None:
        budget = settings.agent.max_turns
    if budget < 1:
        budget = 1

    current_scope = scope or Scope()
    user_query = _last_user_message(messages)

    context: list[dict] = [
        {"role": "system", "content": _system_prompt(
            lang, current_scope, rag_first=rag_first, rag_only=rag_only
        )},
        *_as_messages(messages[:-1]),
        {"role": "user", "content": user_query},
    ]

    collected: dict[str, Hit] = {}
    log: list[_TurnLog] = []

    for turn in range(1, budget + 1):
        action = _next_action(context, turn)
        log.append(_TurnLog(turn, action.action, action.query, repr(action.filters)))

        if action.action == "ask_clarification":
            return AgentOutcome(
                answer=action.clarification or "",
                sources=list(collected.values()),
                turns_used=turn,
                ended_with="clarification",
            )

        if action.action == "final_answer":
            return AgentOutcome(
                answer=action.answer or "",
                sources=list(collected.values()),
                turns_used=turn,
                ended_with="final_answer",
            )

        hits = _dispatch(action, current_scope, turn, rag_first=rag_first, rag_only=rag_only)
        collected.update({str(h.id): h for h in hits})
        context.append(
            {
                "role": "user",
                "content": f"Retrieval results (turn {turn}):\n{_format_hits(hits)}",
            }
        )

    # Budget exhausted: force a best-effort final answer from the gathered
    # context, instructing the model to answer in `lang` and cite sources.
    logger.info(
        "agent budget %d exhausted after %d turns; forcing final answer",
        budget,
        budget,
    )
    summary = "\n".join(
        f"turn {entry.turn}: {entry.action} {entry.query or ''} filters={entry.filters}"
        for entry in log
    )
    answer = chat(
        [
            {
                "role": "system",
                "content": (
                    _system_prompt(lang, current_scope, rag_first=rag_first, rag_only=rag_only)
                    + f" You reached the turn budget. Answer the user's question in "
                    f"{lang} using only the information gathered so far, and cite the "
                    "sources. User question: "
                    f"{user_query}"
                ),
            },
            {"role": "user", "content": f"{summary}\n{user_query}"},
        ]
    )
    return AgentOutcome(
        answer=answer,
        sources=list(collected.values()),
        turns_used=budget,
        ended_with="budget_exhausted",
    )
