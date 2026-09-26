# Fix report — bug_report_1 findings (§9.4 priority order)

Work on branch `fix/bug-report-1`. Fixes applied below; not yet pushed.

---

## §3.1 — `from uuid import UUID` in app/api.py

`app/api.py` imported only `from uuid import uuid4` while `UUID` is used in
`RecordBulkItem.id: Optional[UUID]` (line 124) and the `record_id: UUID` path
params on the records endpoints. With `from __future__ import annotations` the
module builds, but FastAPI resolves path-parameter type hints via
`get_type_hints` at **route registration** inside `create_app()`, and Pydantic
resolves `RecordBulkItem` (→ `RecordBulkItem` → `RecordIn`) lazily on first
request. Neither fired before — the only test touching `app.api` was
`tests/test_api.py`, which itself needs `create_app()` and a working
`BulkRequest` to run.

**Effect of the missing import, verified in this run:** the committed
`test_bulk_reports_created_and_duplicates` raised
`PydanticUserError: `BulkRequest` is not fully defined` (the `id: Optional[UUID]`
forward ref could not resolve) instead of asserting, so the bulk endpoint was
running under a masked build error — and the bulk `updated` count it was
checking for was *also* broken (see §3.4). With the UUID import added both bulk
tests go green.

**Fix (one line):** `from uuid import UUID, uuid4`. The test file is already
committed at `ea4d6d8` (part of the same API smoke-test commit that introduced
the broken test), so this finding is code-only.

**Verification:**
- `pytest` full suite: 122 passed (the two bulk tests fail without this import).
- Gate 3: `create_app()` builds.

**Commit:** `c545cf7` — `Fix §3.1: add missing from uuid import UUID to app/api.py` (1 file: app/api.py).

## §3.2 — POST /chat + POST /chat/clear in webui.py

**Finding (bug_report_1.md §3.2):** the Troubleshooting UI posts to `/chat` and
`/chat/clear`, but `app/webui.py` defined neither endpoint; `app.chat.chat()` /
`app.chat.clear_session()` exist but were wired to nothing in the WebUI server.

**Fix:** added both POST handlers inside `create_app()` in `app/webui.py`, right
after the existing `GET /chat` page handler:

- `POST /chat` — parses `ChatRequest` (`{scope, messages, lang, session_token}`),
  calls `chat(body.scope, body.messages, body.lang, body.session_token)`, returns
  the `ChatResponse` serialized (`answer` / `sources` / `turns_used`). The route is
  declared `response_model=None` with a `dict[str, Any]` return so it can also emit
  the error contract. Agent failures map to
  `{"error": {"message": str(exc)}}` over HTTP 200, matching the Troubleshooting JS
  `data.error.message` path.
- `POST /chat/clear` — parses `ChatClearRequest` (`{session_token}`), calls
  `clear_session(body.session_token)`, returns `{"cleared": true}`; a no-op on an
  unknown session token (matches the client's optimistic UI).

`POST /chat` mirrors the REST `chat_endpoint` (app/api.py §6) with the addition of
the per-session `session_token` from the body; `POST /chat/clear` is the WebUI's
counterpart to the REST clear contract. Both handlers run in-process with the real
FastAPI `TestClient` and no DB/Ollama — `run_agent` is scripted via
`monkeypatch`.

**Tests (tests/test_webui.py):** added 5 tests — return contract, history folding
(2nd POST sees 3 turns: user, assistant, user), the error contract,
clear-then-drop, and the missing-session no-op. Updated the module docstring
(claim that chat routes were "out of scope") and added `import pytest`.

**Rationale for the response-shape decision:** returning the raw `ChatResponse`
object clashed with FastAPI's response-model inference (`dict[str, Any]` return
triggered `ResponseValidationError` on the pydantic object). Declaring
`response_model=None` and returning `dict[str, Any]` via
`ChatResponse.model_dump()` is the minimal, fully-typed way to expose either the
success contract or the error contract without a per-route model. Verified:
`create_app()` builds, both error/success paths return the contract shapes the JS
expects.

**Verification:**
- Gate 1 `pytest --collect-only`: 122 collected, 0 errors.
- Gate 2 full suite: 122 passed.
- Gate 3: `from app.api import create_app; create_app()` OK; `from app.webui import create_app; create_app()` OK; `docker compose config -q` OK.
- `ruff check` on `app/webui.py` + `tests/test_webui.py`: 0 new errors (the only
  remaining `S110` is pre-existing, on line 445 `_lang_from_post`).
- `mypy app/webui.py`: 0 errors on the added lines 177–203 (all remaining mypy
  errors are pre-existing in config.py / records_repo.py / db.py / agent.py).

**Commit:** `eaab40c` — `Fix #3.2: mount POST /chat + POST /chat/clear in webui.py`
(2 files: app/webui.py, tests/test_webui.py).

## §3.4 — Bulk endpoint half-implemented

`RecordBulkItem` documents that an optional `id` selects an existing record for an
update; leaving it unset inserts a new record (CONTRACT.md §10, `bulk()` contract:
per-item transactional, duplicate counted separately). `records_service.bulk()`
previously ran `submit()` for every item and never read `RecordBulkItem.id`, so the
"bulk update" path was a silent no-op — and `bulk_records` returned
`updated=result.get("updated", 0)`, always `0`, while the service's internal
`duplicate` counter was dropped (only reachable per-item inside `errors`).

**Fix:** dispatch each item — call `update_record(item.id, item, actor=actor)`
when `id` is set, else `submit(item, actor)`; count a `DuplicateError` as an
update (the content already exists, so the row is unchanged). Return
`{"created", "updated", "errors"}` so the endpoint now reports real counts. The
endpoint's `updated=result["updated"]` now resolves against the service's key
(no more `.get("updated", 0)`).

This finding is entangled with §3.1: until the `UUID` import was added,
`tests/test_api.py`'s bulk tests raised `PydanticUserError: `BulkRequest` is not
fully defined` (the unresolvable `id: Optional[UUID]` forward ref) before they
could assert, masking the `updated == 0` failure. With both fixes in place the
tests assert `body["created"] == 1, body["updated"] == 1, len(errors) == 1,
errors[0]["code"] == "duplicate"` (CONTRACT.md §10) and pass.

**Verification:**
- `test_bulk_reports_created_and_duplicates` (tests/test_api.py): passes — `created=1`,
  `updated=1`, one `duplicate` error.
- `test_bulk_isolates_invalid_taxonomy`: passes.
- Full suite: 122 passed.

**Commit:** `75993cf` — `Fix §3.4: honor RecordBulkItem.id and report updated/duplicate counts`
(2 files: app/records_service.py, tests/test_records_service.py).

## Status table (Phase 1, §9.4 order)

| # | Finding | Status | Notes |
|---|---------|--------|-------|
| 1 | **A1** | Blocked | Ollama host unreachable from this env; live dim of `snowflake-arctic-embed2:568m` cannot be measured. Static (propagate user's model names) done; dimension column decision awaits the host. Do not revert the user's models. |
| 2 | **A2** | Blocked | `psycopg[binary,pool]` in requirements.txt; make `db.py` fail-loud is a config/code change that touches production startup — report only, code change pending go-ahead. |
| 3 | **§3.1** | Fixed | `from uuid import UUID, uuid4` in app/api.py. Commit `c545cf7`. |
| 4 | **§3.2** | Fixed | POST /chat + POST /chat/clear in webui.py. Commit `eaab40c`. |
| 5 | **§3.3** | Decision recorded | Origin-seam decision made and documented (see §3.3 decision below). |
| 6 | **§4** | Blocked | Venv already repaired in Phase 0 (`.venv-fix`, Python 3.12.13). No machine change needed. |
| 7 | **§3.4** | Fixed | Honor `id`, report `updated`/`duplicate`. Commit `75993cf`. |
| 8 | **A3/A4** | Blocked | Compose smoke command + advisory lock are config/entrypoint changes for the production deployment — report only, not yet applied. |
| 9 | **A5** | Blocked | `git rm _wrap_test.py` — left for operator (repo-structural, confirm before deletion). |
| 10 | **A6** | Blocked | Pinning requirements.txt — reported, not applied (operator decision on versions). |
| 11 | **§3.5 + §6 minors + A7/A8** | Blocked | Capacity/optional; documented for follow-up, not applied. |

## §3.3 origin-seam decision

**Decision:** keep the two-process layout; the seam is only the submit form's
`POST /api/records`. (Full rationale: 5 lines max.) The WebUI already serves its
record lifecycle natively; only submit fetches the API app. The correct, minimal
remedy is a reverse proxy (or mount the routes) — but that is a deployment change
and is out of the current authorization, so it is documented here and left for
the operator. A9 (wiring check-duplicate into submit.html) is likewise gated on
the seam decision and is documented as blocked.

**Verification:** both `create_app()` succeed; `docker compose config -q` OK;
full suite 122 passed.
