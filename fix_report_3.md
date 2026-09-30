# Fix report 3 — MCP layer findings (M1–M16)

Repository: `super-fiffia` · branch `fix/mcp-review` · governing doc `fix_prompt_3.md`
Layer: `app/mcp.py` (+ `app/chat.py` cross-surface, `scripts/run_mcp.py`, README, tests).

## Status table

| # | Finding | Outcome | Location |
|---|---------|---------|----------|
| M1 | `search` RRF ordering not stable/sorting | Fixed | `app/mcp.py` `search` |
| M2 | empty-query rejected as validation | Fixed | `app/mcp.py` `search` |
| M3 | unknown source rejected | Fixed | `app/mcp.py` `search` |
| M4 | `top_k` bounds enforced | Fixed | `app/mcp.py` `search` |
| M5 | non-UUID id rejected as validation | Fixed | `app/mcp.py` `get_record` |
| M6 | `list_records` `limit` bounds | Fixed | `app/mcp.py` `list_records` |
| M7 | `list_records` offset non-negative | Fixed | `app/mcp.py` `list_records` |
| M8 | `list_records` status enum | Fixed | `app/mcp.py` `list_records` |
| M8b | `session_token` validated in `chat` | Fixed | `app/mcp.py` `chat` |
| M9 | exceptions in tools → `internal` envelope | Fixed | `app/mcp.py` `chat` |
| M10 | auth posture documented | Done (docs-only) | `README.md` MCP section |
| M11 | docstring batch | Done | `app/mcp.py` module + tool docstrings, README |
| M12 | docstring batch (search RRF wording) | Done | `app/mcp.py` `search` docstring |
| M13 | MCP health probe liveness-only | Fixed | `scripts/run_mcp.py` |
| M14 | `clear_session` tool | Added | `app/mcp.py` |
| M15 | docstring batch (clear_session, chat) | Done | `app/mcp.py` docstrings |
| M16 | failed turn leaves dangling user message | Fixed | `app/chat.py` `chat` |

## Implementation notes

### M13 — health probe (scripts/run_mcp.py)
Dropped the local 200-OK stub and the now-unneeded `import json`. Reuse the API's
module-level probe exactly (`no duplicate logic`):

```python
from app.api import _health
app.add_route("/api/health", _health, methods=["GET"])
```
Same `200`-with-`"degraded"`-body semantics as `GET /api/health`. The compose
healthcheck command is untouched.

### M8b + M14 + M16 — validation, clear_session, rollback
`chat` now validates `session_token` (M8b) before touching the agent: empty-after-strip
and >128 chars return the `validation` envelope.

`clear_session` (M14) validates the token the same way and returns
`{"session": token, "cleared": True/False}` (`False` when no session existed):

```python
rejected = _validate_session_token(session_token)
if rejected is not None:
    return rejected
cleared = clear_session_tool(session_token)
return {"session": session_token, "cleared": cleared}
```

`app/chat.py::chat` (M16) now records the pre-turn history length and, on any
`run_agent` exception, truncates back to that length before re-raising:

```python
before = len(history)
history.extend(messages)
...
try:
    outcome = run_agent(...)
except Exception:
    del history[before:]   # M16: drop the failed turn's user message(s)
    raise
```

### M9 — error envelope
The `chat` tool wraps `run_agent` in `try/except Exception` and returns
`{"error": {"code": "internal", "message": <exception class name>}}` (the existing
envelope for MCP; per the REST contract `internal` = "unhandled internal failure").

### Logging (`app/mcp.py`)
Uses the app's structured logger (no third-party handler). The existing
`logger.error("mcp.chat failure for session %r", session_token, exc_info=True)`
is left as-is (matches the established app logging idiom); the call site records the
session token and exception type for observability.

## Verification gates

- `pytest` — **170 passed**. `tests/test_mcp.py` = 19 tests (M2, M3, M4, M5, M6,
  M7, M8, M8b, M9, M13, M14, M16; plus RRF correctness and success cases).
  `tests/test_chat.py` = 12 tests (11 existing + M16 rollback, and the
  LRU/session-cache eviction tests M8b-adjacent).
- `mcp.list_tools()` returns the 5 tools (`chat`, `clear_session`, `get_record`,
  `list_records`, `search`).
- `scripts/run_mcp.py` and `app.webui.create_app()` import and construct cleanly.

## Tests added (tests/test_mcp.py)

- **Validation matrix (no DB seeding):** empty query (M2), unknown source (M3),
  `top_k` 0/101 (M4), non-UUID id (M5), `limit` 0/501 (M6), negative offset (M7),
  unknown status (M8), blank/oversized `session_token` (M8b), unknown `lang`.
- **RRF correctness:** both-sources fusion sorted by score desc; single-source
  ordering; `source="records"` omits RAG.
- **chat success / failure / clear_session** field-by-field.
- **M16 rollback:** `run_agent` raising leaves the failed turn's user message
  out of `_history` (pre-turn turns retained).

## Decisions / open items

- **M10 — auth posture: docs-only.** No code this round; documented as an accepted
  risk in the README MCP section: unauthenticated + LAN-only, same trust boundary as
  the API/WebUI container; binding `0.0.0.0` intentional on a trusted LAN; tokens
  are future work.
- **M13 — healthcheck:** left unchanged (per prompt) — the *command* stays; only the
  handler it invokes was changed to a liveness+dependency probe.
- No unaddressed findings; no disagreements with the governing doc.

## Attribution
This work will be committed with messages ending
`Co-Authored-By: Claude Code <noreply@anthropic.com>`. Nothing has been pushed.
