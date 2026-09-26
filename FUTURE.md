# FUTURE.md — Next Iteration

> Purpose of this file: capture design decisions that are **not yet in the code** so
> they survive a context reset. The entries below are written to be self-contained —
> a reader with zero conversation history should be able to implement them from this
> file alone.

## Standing constraints (do not lose these)

- **Do not push.** This branch stays local; the user pushes manually.
- **Never commit review records.** `bug_report_1.md`, `bug_report_2.md`,
  `fix_prompt.md`, `fix_prompt_2.md`, `fix_report_1.md` are review artifacts, not
  code.
- **Do not fake the DB fixture.** Tests use the in-memory `_fake_embed` constant
  (1024-D), never real Ollama.
- **Do not revert intentional config.** Embedder is `snowflake-arctic-embed2:568m`,
  LLM is `gemma4:e4b`, `EMBED_DIM=1024`, FS uses `FS_OLLAMA_URL`.

## Iteration 1: RAG routing by operator intent — IMPLEMENTED ✅

### The goal (now done)

Today, retrieval for a chat turn is decided **solely by the model**: `run_agent`
calls `_next_action`, gets an `AgentAction`, and only `search_rag` queries the
`rag_chunks` store (`app/retrieval.py:retrieve_rag`, scoped-free, document chunks).
The system prompt (`app/agent.py:_system_prompt`) tells the model to *prefer
records*, so `search_rag` is opt-in by the model.

The operator now forces the route. When the user types a chat message, retrieval
order is chosen by a policy in `app/chat.py` — see "Current behavior" below.
This iteration is complete; the "Required changes" section below records what the
final code looks like for the next person.

---

### What "current state" is (reference only — already committed, do not change the
retrieval machinery)

- `chat()` in `app/chat.py:90` folds the user turn into the per-session in-memory
  history (`_history.setdefault(session_token, [])`), then calls
  `run_agent(scope, list(history), lang, budget=settings.agent.max_turns)` and
  appends the returned answer back, trims history, returns `ChatResponse`.
- `run_agent()` in `app/agent.py:224`:
  - builds a single `context` list: system prompt + prior turns + latest user msg
    (`app/agent.py:254`).
  - loops up to `budget` turns (default 5, from `settings.agent.max_turns`),
    each turn calling `_next_action` → one `chat_structured` call (retried once;
    second failure forces `final_answer`).
  - dispatches the parsed `AgentAction` (`app/records_repo.py:100`) per
    `app/agent.py:267`:
    - `ask_clarification` → return, `ended_with='clarification'`.
    - `final_answer` → return, `ended_with='final_answer'`.
    - `search_records` → `_dispatch` → `retrieve_fs(action.filters or scope, query)`
      (scoped, `records` table).
    - `search_rag` → `_dispatch` → `retrieve_rag(query)` (scoped-free, `rag_chunks`).
  - dedupes hits by id across turns (`collected: dict[str, Hit]`, `app/agent.py:260`),
    renders them (`_format_hits`) and feeds them back into `context` for the next
    turn.
  - budget exhausted → forced best-effort `final_answer` (`app/agent.py:303`),
    `ended_with='budget_exhausted'`.
- `AgentAction` (`app/records_repo.py:100`): `thought` + `action` (one of
  `search_records | search_rag | ask_clarification | final_answer`) + `query`
  (optional) + `filters: Scope` (optional, for records) + `clarification` (optional)
  + `answer` (optional).
- `retrieve_fs` / `retrieve_rag` (`app/retrieval.py:317` / `:281`) each do a hybrid
  vector + lexical retrieval fused by RRF, fall back to lexical-only on
  `EmbeddingError`, capped at `top_k_records` / `top_k_rag`.

None of that retrieval logic changes. The only additions are: **detect intent**,
**thread a flag**, and **suppress the records leg** while RAG-only is active.

---

### Current behavior (final — the implemented design)

Three routes, decided per-turn by `chat()` in `app/chat.py` and threaded through
`run_agent(scope, messages, lang, budget, *, rag_first=False, rag_only=False)`
into `app/agent.py`:

| Situation | `rag_first` | `rag_only` | Order |
|---|---|---|---|
| `#fails` tag (either question) | `False` | `False` | records-first; RAG suppressed |
| First question, no tag | `False` | `False` | records-first (unchanged default) |
| Follow-up (2nd+ question in the session) | `True` | `False` | RAG, then records fallback |
| Latest message tagged `#docs` | `False` | `True` | RAG-only; records never queried |

Precedence, highest first: `#fails` (records-first) beats `#docs` (RAG-only) when
both appear in a turn. A follow-up with `#docs` is therefore RAG-only, not
RAG-first — the explicit tag overrides the session follow-up rule. Without any
tag, the first question is records-first and follow-ups are RAG-first.

**Detection.** `app/rag_routing.py` holds both markers and detectors:

```python
MARKER = "#docs"              # -> RAG-only
FAILS_MARKER = "#fails"       # -> records-first

def resolve_rag_first(turn: str) -> bool:
    """True when the turn carries the `#docs` marker (case-insensitive)."""
    return MARKER.lower() in turn.lower()

def resolve_records_first(turn: str) -> bool:
    """True when the turn carries the `#fails` marker (case-insensitive)."""
    return FAILS_MARKER.lower() in turn.lower()
```

`chat()` resolves the two flags from the latest user message text. Both flags off
is records-first, so `#fails` needs no change in `run_agent` — it just clears the
flags:

```python
history = _history.setdefault(session_token, [])
is_first_turn = len(history) == 0
latest = messages[-1].content if messages else ""
fails_tagged = resolve_records_first(latest)
docs_tagged = resolve_rag_first(latest)
if fails_tagged:
    rag_first, rag_only = False, False          # #fails -> records-first
elif docs_tagged:
    rag_first, rag_only = False, True           # #docs -> RAG-only
else:
    rag_first, rag_only = not is_first_turn, False  # follow-up -> RAG-first
```

**Enforcement in `app/agent.py`.**
- `_system_prompt(..., rag_first, rag_only)`: `rag_only` takes precedence — when
  true, the prompt says "consult only RAG documentation; do not query stored
  records" (English and Swedish). Otherwise `rag_first` yields "cite RAG before
  records then records"; otherwise the records-first default.
- `_dispatch(action, scope, turn, *, rag_first, rag_only)`: `rag_only` returns
  `retrieve_rag(query)` unconditionally (records never searched, even if the
  model emits `search_records`). `rag_first` queries RAG and falls back to
  `retrieve_fs(action.filters or scope, query)` only when RAG returns nothing.
  Both cases short-circuit on RAG hits (records leg skipped).
- `run_agent` threads both flags into `_dispatch` and both `_system_prompt`
  calls (the turn loop and the budget-exhausted forced answer).

**Tests.** `tests/test_rag_routing.py` covers `resolve_rag_first`;
`tests/test_agent.py` covers `rag_first` (RAG-then-fallback, and RAG-hit
short-circuit) and `rag_only` (records never queried even on `search_records`).

### Remaining future ideas (confirm with user before touching)

- **More markers.** Today only `#docs` (case-insensitive) triggers RAG-only.
  The old candidate set (`!rag`, `documentation`) is not implemented; add to
  `app/rag_routing.py` if the user wants more.
- **Per-session persistence.** RAG routing is stateless-per-message today (except
  the "follow-up" rule, which keys off the in-memory `_history`). Holding
  RAG-only across several messages of a session would extend `_history`.
- **Clearing a persistent override.** If a session-persistent RAG-only is added,
  decide the cancel trigger (e.g. a `#records` marker) vs. session-end.

### Verification once implemented

- Webui `/chat`, message containing the marker → response `sources` carry only
  `source='rag'` hits; no `source='records'` hits.
- Same message without the marker → `search_records` remains the primary path
  (records-first default unchanged).
- `search_rag` still degrades to lexical-only on `EmbeddingError`
  (`app/retrieval.py:306`).
- NFR-8 turn log still records the suppressed-records decisions.
- Tests: `chat.py`/`agent.py` in-process; `_fake_embed` unchanged; no Ollama.
