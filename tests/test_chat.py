"""Tests for the chat orchestration layer (T3.2).

Exercises the *core* behaviour of ``app.chat`` against the real agent loop:
a turn folds the user turn into history, runs :func:`run_agent`, and appends the
assistant answer back to history; ``clear_session`` drops that history. Ollama
calls are scripted via ``monkeypatch`` on ``app.agent.chat_structured`` /
``app.agent.chat`` (the module the loop actually calls), and retrieval is stubbed
to nothing so no database is touched.
"""

from __future__ import annotations

from typing import Sequence

import pytest

from app.agent import AgentOutcome
from app import chat as chat_mod
from app.chat import chat, clear_session, to_response
from app.records_repo import ChatTurn
from app.config import settings


def _outcome(
    *,
    answer: str = "an answer",
    turns_used: int = 1,
    ended_with: str = "final_answer",
    sources: list | None = None,
) -> AgentOutcome:
    """Build a scripted AgentOutcome."""
    return AgentOutcome(
        answer=answer,
        sources=sources or [],
        turns_used=turns_used,
        ended_with=ended_with,
    )


def _script(monkeypatch, outcome):
    """Patch the agent loop so one turn yields a fixed AgentOutcome.

    Patch ``app.chat.run_agent`` (not ``app.agent.run_agent``): ``chat.py`` binds
    ``run_agent`` into its own namespace via ``from app.agent import run_agent``,
    so only the reference in the chat module is the one that gets called.
    """
    monkeypatch.setattr(
        "app.chat.run_agent",
        lambda scope, messages, lang, budget, **_: outcome,
    )


@pytest.fixture(autouse=True)
def _reset_history() -> None:
    """Drop any accumulated history after each test.

    The chat history lives in a single module-level dict keyed by session
    token; without isolation a turn in one test leaks into the next.
    """
    yield
    chat_mod._history.clear()


def test_turn_returns_answer_sources_turns_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn returns the agent's answer + sources + turns_used from run_agent."""
    sources = [
        {"id": "11111111-1111-1111-1111-111111111111", "source": "records", "score": 0.9},
        {"id": "22222222-2222-2222-2222-222222222222", "source": "rag", "score": 0.5},
    ]
    monkeypatch.setattr(settings.agent, "max_turns", 5)
    _script(monkeypatch, _outcome(answer="hi there", turns_used=3, sources=sources))

    resp = chat(scope=None, messages=[ChatTurn(role="user", content="q")], lang="en")

    assert resp.answer == "hi there"
    assert resp.turns_used == 3
    assert len(resp.sources) == 2

    # The user turn reached run_agent, which wrapped it in history.
    history = chat_mod._history[chat_mod._DEFAULT_SESSION]
    assert history[-2] == ChatTurn(role="user", content="q")
    assert history[-1] == ChatTurn(role="assistant", content="hi there")


def test_history_accumulates_across_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A follow-up turn continues the running conversation."""
    monkeypatch.setattr(settings.agent, "max_turns", 5)
    _script(monkeypatch, _outcome(answer="again"))

    chat(scope=None, messages=[ChatTurn(role="user", content="one")], lang="en")
    chat(scope=None, messages=[ChatTurn(role="user", content="two")], lang="en")

    history = chat_mod._history[chat_mod._DEFAULT_SESSION]
    # user(1), assistant(answer), user(2), assistant(answer).
    assert [t.role for t in history] == ["user", "assistant", "user", "assistant"]
    assert history[0].content == "one"
    assert history[2].content == "two"


def test_agent_gets_full_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_agent receives the full running history, not just the new turn."""
    monkeypatch.setattr(settings.agent, "max_turns", 5)
    seen: list[Sequence] = []

    def spy(_scope, messages, _lang, budget, **_):
        seen.append(list(messages))
        return _outcome()

    monkeypatch.setattr("app.chat.run_agent", spy)

    chat(scope=None, messages=[ChatTurn(role="user", content="a")], lang="en")
    chat(scope=None, messages=[ChatTurn(role="user", content="b")], lang="en")

    assert len(seen) == 2
    # Second turn's history carries the first exchange plus the new turn.
    assert seen[0][-1].content == "a"
    assert seen[1][-1].content == "b"
    assert len(seen[1]) == len(seen[0]) + 2


def test_clear_resets_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear resets the per-session history."""
    monkeypatch.setattr(settings.agent, "max_turns", 5)
    _script(monkeypatch, _outcome())

    chat(scope=None, messages=[ChatTurn(role="user", content="q")], lang="en")
    assert chat_mod._history[chat_mod._DEFAULT_SESSION]

    clear_session(chat_mod._DEFAULT_SESSION)
    assert chat_mod._DEFAULT_SESSION not in chat_mod._history

    # A new turn after clear starts fresh.
    _script(monkeypatch, _outcome())
    chat(scope=None, messages=[ChatTurn(role="user", content="fresh")], lang="en")
    history = chat_mod._history[chat_mod._DEFAULT_SESSION]
    assert len(history) == 2


def test_session_token_isolates_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different session tokens keep separate histories."""
    monkeypatch.setattr(settings.agent, "max_turns", 5)
    _script(monkeypatch, _outcome())

    chat(scope=None, messages=[ChatTurn(role="user", content="a")], lang="en", session_token="s1")
    chat(scope=None, messages=[ChatTurn(role="user", content="b")], lang="en", session_token="s2")

    assert chat_mod._history["s1"][0].content == "a"
    assert chat_mod._history["s2"][0].content == "b"


def test_clear_missing_session_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing a session that does not exist does not raise."""
    clear_session("never-used")


def test_history_trims_to_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session's history is trimmed to the last MAX_HISTORY_TURNS turns."""
    monkeypatch.setattr(settings.agent, "max_turns", 5)
    _script(monkeypatch, _outcome(answer="a"))

    # Each turn adds 2 turns (user + assistant); MAX_HISTORY_TURNS is 20, so 11
    # turns (22 entries) must be trimmed back to exactly the last 20.
    for _ in range(11):
        chat(scope=None, messages=[ChatTurn(role="user", content="q")], lang="en")

    history = chat_mod._history[chat_mod._DEFAULT_SESSION]
    assert len(history) == chat_mod.MAX_HISTORY_TURNS
    # The retained window is the tail: the newest user turn is last, and the
    # oldest turns (first user turn, its assistant reply) have been dropped.
    assert history[-1].role == "assistant"
    assert history[-2].role == "user"
    assert history[-2].content == "q"


def test_history_evicts_least_recently_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sessions past MAX_SESSIONS are evicted LRU; the most-recently-used one is
    retained."""
    monkeypatch.setattr(settings.agent, "max_turns", 5)
    _script(monkeypatch, _outcome())

    # Exercise enough sessions to exceed the bound; touch ``hot`` last so it is
    # the most-recently-used.
    for i in range(chat_mod.MAX_SESSIONS + 20):
        chat(scope=None, messages=[ChatTurn(role="user", content=f"q{i}")], lang="en", session_token=f"s{i}")
    # A distinct "hot" session touched last.
    chat(scope=None, messages=[ChatTurn(role="user", content="hot")], lang="en", session_token="hot")

    # The store is capped: we never held more than MAX_SESSIONS at once.
    assert len(chat_mod._history) <= chat_mod.MAX_SESSIONS
    # ``hot`` was touched last, so it survives eviction.
    assert "hot" in chat_mod._history
    # The sessions evicted are the least-recently-used ones — they are gone.
    for i in range(5):
        assert f"s{i}" not in chat_mod._history


def test_eviction_preserves_lru_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """After eviction, the retained sessions are in most-recently-used order."""
    monkeypatch.setattr(settings.agent, "max_turns", 5)
    _script(monkeypatch, _outcome())

    for i in range(chat_mod.MAX_SESSIONS + 10):
        chat(scope=None, messages=[ChatTurn(role="user", content="q")], lang="en", session_token=f"t{i}")

    keys = list(chat_mod._history)
    assert len(keys) == chat_mod.MAX_SESSIONS
    # Least-recently-used (oldest) evicted; retained sessions keep LRU order,
    # newest at the end.
    assert keys[0] == "t10"
    assert keys[-1] == f"t{chat_mod.MAX_SESSIONS + 9}"


def test_clear_evicts_when_overbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing a session evicts the least-recently-used session if over bound."""
    monkeypatch.setattr(settings.agent, "max_turns", 5)
    _script(monkeypatch, _outcome())

    for i in range(chat_mod.MAX_SESSIONS):
        chat(scope=None, messages=[ChatTurn(role="user", content="q")], lang="en", session_token=f"u{i}")
    assert len(chat_mod._history) == chat_mod.MAX_SESSIONS

    # Clearing an existing session (``u0``) still enforces the bound.
    clear_session("u0")
    assert "u0" not in chat_mod._history
    assert len(chat_mod._history) <= chat_mod.MAX_SESSIONS


def test_chat_rolls_back_history_on_raised_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn whose run_agent raises leaves no user turns in history."""
    monkeypatch.setattr(settings.agent, "max_turns", 5)
    _script(monkeypatch, _outcome())
    chat(scope=None, messages=[ChatTurn(role="user", content="q1")], lang="en")
    # A following turn whose agent raises.
    def boom(_scope, _messages, _lang, **_):
        raise RuntimeError("boom")
    monkeypatch.setattr("app.chat.run_agent", boom)

    with pytest.raises(RuntimeError):
        chat(scope=None, messages=[ChatTurn(role="user", content="q2")], lang="en")

    history = chat_mod._history[chat_mod._DEFAULT_SESSION]
    # Only the prior turn survives; the failed turn's user message is gone.
    assert [t.content for t in history] == ["q1", "an answer"]
    assert history[-1].role == "assistant"


def test_to_response_maps_fields() -> None:
    """to_response maps an AgentOutcome onto the ChatResponse contract."""
    outcome = _outcome(answer="x", turns_used=4, ended_with="budget_exhausted")

    resp = to_response(outcome)

    assert resp.answer == "x"
    assert resp.turns_used == 4
    assert list(resp.sources) == []
