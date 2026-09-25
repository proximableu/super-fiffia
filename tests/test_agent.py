"""Tests for ``app/agent.py`` — the agent turn loop (T3.1).

Ollama calls are fully mocked: ``chat_structured`` is scripted to return a
sequence of ``AgentAction`` objects, and ``retrieve_fs`` / ``retrieve_rag`` are
spied on so the dispatch and budget behaviour can be asserted without a database
or an LLM.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app import agent
from app.agent import AgentOutcome, run_agent
from app.records_repo import AgentAction, ChatTurn, Hit, Scope


def _hit(source: str = "records", *, id: str | None = None, **kw) -> Hit:
    return Hit(
        id=uuid.uuid4() if id is None else uuid.UUID(id),
        source=source,
        score=0.5,
        **kw,
    )


def _actions(*actions: AgentAction) -> MagicMock:
    """A ``chat_structured`` mock scripted to yield ``actions`` as raw JSON."""
    return MagicMock(side_effect=[a.model_dump_json() for a in actions])


@pytest.mark.parametrize(
    "lang,expected_substring",
    [("sv", "maskfel"), ("en", "machine fault")],
)
def test_system_prompt_language(
    monkeypatch: pytest.MonkeyPatch, lang: str, expected_substring: str
) -> None:
    assert expected_substring in agent._system_prompt(lang, Scope())


def test_search_then_final_answer_dispatches_and_returns():
    seen_scope: Scope | None = None

    def fake_fs(scope: Scope, query: str):
        nonlocal seen_scope
        seen_scope = scope
        return [_hit(id="00000000-0000-0000-0000-000000000001", category="a")]

    scope = Scope(category="press")
    messages = [ChatTurn(role="user", content="pumpen lyder konstigt")]
    actions = [
        AgentAction(
            thought="search", action="search_records", query="pump", filters=scope
        ),
        AgentAction(
            thought="done",
            action="final_answer",
            answer="the answer",
        ),
    ]

    with (
        patch("app.agent.chat_structured", _actions(*actions)) as cs,
        patch("app.agent.retrieve_fs", side_effect=fake_fs) as fs,
    ):
        out = run_agent(scope, messages, lang="en")

    assert isinstance(out, AgentOutcome)
    assert out.ended_with == "final_answer"
    assert out.answer == "the answer"
    assert out.turns_used == 2
    assert fs.call_count == 1
    assert seen_scope == scope
    assert [str(h.id) for h in out.sources] == [
        "00000000-0000-0000-0000-000000000001"
    ]
    # Structured output is used (not the plain chat()).
    assert cs.call_count == 2


def test_search_rag_dispatches_and_returns():
    actions = [
        AgentAction(thought="rag", action="search_rag", query="bearing noise"),
        AgentAction(thought="done", action="final_answer", answer="ok"),
    ]
    rag = MagicMock(return_value=[_hit(source="rag", id="11111111-0000-0000-0000-000000000011")])

    with (
        patch("app.agent.chat_structured", _actions(*actions)) as cs,
        patch("app.agent.retrieve_rag", rag),
        patch("app.agent.retrieve_fs") as fs,
    ):
        out = run_agent(Scope(), [ChatTurn(role="user", content="q")], lang="en")

    assert out.ended_with == "final_answer"
    assert rag.call_count == 1
    assert fs.call_count == 0
    assert cs.call_count == 2


def test_ask_clarification_yields():
    actions = [AgentAction(thought="?", action="ask_clarification", clarification="which model?")]

    with patch("app.agent.chat_structured", _actions(*actions)) as cs:
        out = run_agent(Scope(), [ChatTurn(role="user", content="q")], lang="en")

    assert out.ended_with == "clarification"
    assert out.answer == "which model?"
    assert out.turns_used == 1
    assert cs.call_count == 1


def test_budget_exhaustion_forces_final_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    actions = [
        AgentAction(thought="search", action="search_records", query="a"),
        AgentAction(thought="search", action="search_records", query="b"),
        AgentAction(thought="search", action="search_records", query="c"),
    ]
    forced = MagicMock(return_value="forced answer from gathered context")

    monkeypatch.setattr(agent, "chat", forced)
    monkeypatch.setattr(
        agent, "chat_structured", _actions(*actions)
    )
    monkeypatch.setattr(agent, "retrieve_fs", lambda scope, query: [])
    monkeypatch.setattr(agent, "retrieve_rag", lambda query: [])

    out = run_agent(Scope(), [ChatTurn(role="user", content="q")], lang="en", budget=3)

    assert out.ended_with == "budget_exhausted"
    assert out.answer == "forced answer from gathered context"
    assert out.turns_used == 3
    assert forced.call_count == 1
    # The forced answer is built from the gathered context, which must include
    # the latest user message.
    args = forced.call_args.args[0]
    last = args[-1]
    assert last["role"] == "user"
    # The forced answer is built from the gathered context, which prepends the
    # per-turn summary before the latest user message.
    assert last["content"].endswith("q")
    assert "search_records" in last["content"]


def test_parse_failure_retries_then_forces(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``chat_structured`` returns raw JSON. The first call returns invalid JSON
    # so ``model_validate_json`` fails; the retry returns a valid final_answer.
    bad = "not json"
    good = AgentAction(
        thought="forced", action="final_answer", answer="best effort"
    ).model_dump_json()

    monkeypatch.setattr(agent, "chat_structured", MagicMock(side_effect=[bad, good]))
    monkeypatch.setattr(agent, "chat", MagicMock(return_value="n/a"))
    monkeypatch.setattr(agent, "retrieve_fs", lambda scope, query: [])

    out = run_agent(Scope(), [ChatTurn(role="user", content="q")], lang="en", budget=3)

    assert out.ended_with == "final_answer"
    assert out.answer == "best effort"
    assert out.turns_used == 1


def test_empty_scope_defaults():
    out = run_agent(
        None,
        [ChatTurn(role="user", content="q")],
        lang="en",
        budget=1,
    )
    assert out.ended_with == "final_answer"
