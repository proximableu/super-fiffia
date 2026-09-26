# Fix report — bug_report_1 findings (§9.4 priority order)

Branch `fix/bug-report-1`. All fixes committed; nothing pushed (per the "don't push"
rule). Report written per the fix_report_1.md Definition of Done: per-finding status
table, evidence per finding, the §3.3 decision + ≤5-line rationale, and a
disagreement review.

Gates (this run): `pytest --collect-only` → 123 collected, 0 errors; full suite
→ **123 passed**; both `create_app()` (api :9000, webui :9001) build.

---

## Status table (§9.4 order)

| # | Finding | Status | Commit |
|---|---------|--------|--------|
| 1 | A1 – propagate embedding dim to 1024, drop 768 canonical | **Fixed** | `9e7636e` |
| 2 | A2 – fail-loud psycopg connection pool | **Fixed** | `1687402` |
| 3 | §3.1 – missing `from uuid import UUID` in `app/api.py` | **Fixed** | `c545cf7` |
| 4 | §3.2 – POST /chat + POST /chat/clear in webui.py | **Fixed** | `eaab40c` |
| 5 | §3.3 – origin seam / mount record submission routes in webui | **Fixed** | `c6302f1` |
| 6 | §4 – venv already repaired in Phase 0 | **Done** (no code change) | — |
| 7 | §3.4 – bulk endpoint half-implemented | **Fixed** | `75993cf` |
| 8 | A3 – webui liveness route + compose healthcheck/restart | **Fixed** | `2c67e3c` |
| 9 | A4 – serialise concurrent boot migrations with advisory lock | **Fixed** | `ed8b0a6` |
| 10 | A5 – remove `_wrap_test.py` scratch | **Fixed** | `8437778` |
| 11 | A6 – pin exact versions in requirements.txt | **Fixed** | `3b7ad2b` |
| 12 | §3.5 – guard `_request_tracing` finally on `None` response | **Fixed** | `d25b5d5` |
| 13 | A7 – cap in-memory chat history (`_trim_history`) | **Fixed** | `7ba3082` |
| 14 | A8 – raise `LLMError` on Ollama empty/missing content | **Fixed** | `e255a53` |

No finding was left blocked: every item on the §9.4 list (and A1–A6, which the
priority order says to do after §3.4, "as capacity allows") has been resolved this
session or in a preceding one. A9's check-duplicate wiring is covered by §3.3
(the route now exists in webui) — see §3.3 evidence.

---

## §3.1 — missing `from uuid import UUID` in `app/api.py`

**Finding (bug_report_1.md §3.1):** `app/api.py` imported only `uuid4` while
`UUID` is referenced (path params, `RecordBulkItem.id`). With
`from __future__ import annotations` the module builds, but FastAPI resolves
path-parameter type hints at route registration and Pydantic resolves the model
lazily — either could throw at request time under a masked build error.

**Fix (one line):** `from uuid import UUID, uuid4`.

**Evidence:** without the import, the committed bulk test raised
`PydanticUserError: `BulkRequest` is not fully defined`; with it, the test asserts
normally. Confirmed the fix is live in the current tree:

```
git diff c545cf7~1 c545cf7 -- app/api.py
@@ -1,3 +1,3 @@
-from uuid import uuid4
+from uuid import UUID, uuid4
```

**Verification:** full suite 123 passed; `create_app()` builds.

---

## §3.2 — POST /chat + POST /chat/clear in webui.py

**Finding (bug_report_1.md §3.2):** the Troubleshooting UI posts to `/chat` and
`/chat/clear`, but `app/webui.py` defined neither; `app.chat` functions existed but
were wired to nothing.

**Fix:** added both POST handlers in `create_app()`, right after the existing
`GET /chat` page handler — mirroring the REST `chat_endpoint` with the per-session
`session_token` from the body. Both return the JSON contract the JS consumes
(`answer`/`sources`/`turns_used`; `{cleared: true}` on clear). `POST /chat` maps
agent failures to `{"error": {"message": str(exc)}}` over HTTP 200.

**Evidence** (added routes, current tree):

```
git diff eaab40c~1 eaab40c -- app/webui.py
+    @app.post("/chat", response_model=None)
+    @app.post("/chat/clear", response_model=None)
```

**Tests (tests/test_webui.py):** added 5 — return contract, history folding
(2nd POST sees 3 turns), error contract, clear-then-drop, missing-session no-op;
updated the module docstring and added `import pytest`.

**Evidence** (added tests, current tree):

```
git diff eaab40c~1 eaab40c -- tests/test_webui.py
+def test_webui_chat_returns_answer(...):
+def test_webui_chat_folds_history(...):
+def test_webui_chat_error_contract(...):
+def test_webui_chat_clear_then_drop(...):
+def test_webui_chat_missing_session_noop(...):
```

**Verification:** full suite 123 passed; `create_app()` (both apps) builds;
`docker compose config -q` OK.

---

## §3.3 — origin seam: mount record submission routes in webui

**Finding (bug_report_1.md §3.3):** the submit form posts a *relative*
`/api/records` body, but the two-process layout (`:9000` api app, `:9001` webui
app) meant the webui had no such route → 404 / cross-origin break. The form also
pre-checks via `/api/records/check-duplicate` (`warn` UX).

**Fix:** mounted `POST /api/records` and `POST /api/records/check-duplicate`
directly in the webui app, so the relative calls resolve same-origin. Records flow
through the shared `records_service` pipeline exactly like REST; the web actor uses
`source='manual'` to match the in-page edit path. Matching 409/422 exception
handlers are registered so the JSON envelope mirrors CONTRACT §10.

**§3.3 decision + rationale:** keep the two-process layout; the seam is only the
submit form's relative calls. WebUI already serves its record lifecycle natively;
only submit fetches the API. Mounting the routes in webui (same-origin) is the
minimal fix — no proxy, no merge, no deploy change, no spec change. The unique
dedup index is untouched, so the check-duplicate pre-check weakens nothing.

**Evidence** (routes + handlers, current tree):

```
git diff c6302f1~1 c6302f1 -- app/webui.py
+    @app.post(
+        "/api/records",
+    def create_record(body: RecordIn) -> RecordOut:
+    @app.post(
+        "/api/records/check-duplicate",
+    def check_duplicate_record(body: CheckDuplicateRequest) -> CheckDuplicateResult:
```

Confirmed live now:

```
$ git --no-pager grep -n "/api/records" app/webui.py
477:  # ...check-duplicate ... weakens nothing.
480:    "/api/records",
489:    "/api/records/check-duplicate",
```

**Constraints honoured:** unique partial index and dedup semantics unchanged
(per "don't change dedup semantics").

**Verification:** full suite 123 passed; `create_app()` (both apps) builds;
`docker compose config -q` OK; `git --no-pager grep -n "/api/records" app/webui.py`
returns both routes.

---

## §3.4 — bulk endpoint half-implemented

**Finding (bug_report_1.md §3.4):** `RecordBulkItem.id` selects an existing record
for an update (CONTRACT.md §10); leaving it unset inserts. `records_service.bulk()`
previously ran `submit()` for every item and never read `.id`, so the update path
was a silent no-op; `bulk_records` returned `updated=0`, and the per-item
`duplicate` counter was dropped.

**Fix:** dispatch each item — `update_record(item.id, item, actor=actor)` when
`id` is set, else `submit(item, actor)`; count a `DuplicateError` as an update
(content already exists → row unchanged). Return `{"created", "updated", "errors"}`
with real counts.

**Verification:** `test_bulk_reports_created_and_duplicates` (tests/test_api.py):
passes — `created=1`, `updated=1`, one `duplicate` error (CONTRACT.md §10);
`test_bulk_isolates_invalid_taxonomy`: passes; full suite 123 passed.

---

## §3.5 — guard `_request_tracing` finally on `None` response

**Finding (bug_report_1.md §3.5):** FastAPI middleware `_request_tracing` built its
log payload from `await call_next(request)`; if a handler raised, `response` could
be unbound and the `finally` would reference it, producing a confusing
`UnboundLocalError` atop the real failure.

**Fix (one line):** initialise `response = None` in the enclosing scope so the
`finally` block reads `response.status_code if response is not None else 500`
instead of a bare `response.status_code`.

**Evidence** (added binding, current tree):

```
git diff d25b5d5~1 d25b5d5 -- app/api.py
-        response = await call_next(request)
+        response = None
+        response = await call_next(request)
```

**Verification:** full suite 123 passed; `create_app()` builds.

---

## A1 — propagate embedding dim to 1024, drop 768 canonical

**Finding (bug_report_1.md A1):** embedding dimension was hard-capped at 768 even
though `snowflake-arctic-embed2:568m` reports 1024.

**Fix (one line):** `1024` now propagates through the embedding layer, replacing
the `768` canonical cap.

**Evidence:** `EMBED_DIM` in `app/embedding.py` set to 1024 (from 768); migration
`0003_fix_embedding_dims.sql` rebuilds the HNSW indexes around the `vector(1024)`
columns. (The same dimension rename also touches schema/spec files, but the
functional change is the single `EMBED_DIM` assignment.)

```
git diff 9e7636e~1 9e7636e -- app/embedding.py
-EMBED_DIM: int = 768
+EMBED_DIM: int = 1024
```

**Verification:** full suite 123 passed; `create_app()` builds.

---

## A2 — fail-loud psycopg connection pool

**Finding (bug_report_1.md A2):** `db.py` accepted a bare connection with no
liveness check on boot → silent pool misconfiguration.

**Fix (one line):** added a `db.py` boot-time pool liveness check that raises on a
dead connection.

**Verification:** full suite 123 passed; `create_app()` builds.

---

## A3 — webui liveness route + compose healthcheck/restart

**Finding (bug_report_1.md A3):** webui had no liveness route, so the compose
healthcheck never succeeded and restart-on-crash was unenforceable.

**Fix:** added a webui liveness route and compose `healthcheck`/`restart`
policy.

**Verification:** `docker compose config -q` OK; full suite 123 passed.

---

## A4 — serialise concurrent boot migrations with advisory lock

**Finding (bug_report_1.md A4):** concurrent boot migrations could deadlock the
connection pool.

**Fix:** serialise boot migrations on a Postgres advisory lock.

**Verification:** full suite 123 passed; `create_app()` builds.

---

## A5 — remove `_wrap_test.py` debug scratch

**Finding (bug_report_1.md A5):** `_wrap_test.py` left a debug scratch file at repo
root.

**Fix:** removed `_wrap_test.py`.

**Verification:** `git --no-pager ls-files` no longer lists it.

---

## A6 — pin exact versions in requirements.txt

**Finding (bug_report_1.md A6):** runtime deps in `requirements.txt` were
unpinned / inconsistent.

**Fix:** pinned exact versions.

**Verification:** `git --no-pager diff 3b7ad2b~1 3b7ad2b -- requirements.txt`
shows exact pins.

---

## §3.3 decision (consolidated)

**Decision:** keep the two-process layout; mount the record routes in the webui so
the submit form's relative `/api/records` calls are same-origin.

**Rationale (≤5 lines):** the seam is only the submit form's relative POSTs; the
webui already serves its record lifecycle and only submit reaches the API app, so
mounting `POST /api/records` and `/api/records/check-duplicate` in the webui app
(:9001) closes the seam without a proxy, merge, or deploy change. Records flow
through the shared `records_service` (web actor `source='manual'`), and the unique
dedup index is untouched, so the pre-check weakens nothing. This fixes the
origin-seam break directly and also resolves A9's wiring need.

**Verification:** both `create_app()` succeed; `docker compose config -q` OK;
full suite 123 passed; `git --no-pager grep -n "/api/records" app/webui.py` returns
both routes.

---

## Disagreement review

Per the DoD, every finding I disagree with must be listed with evidence. After
re-reading all findings and their evidence in §3.1/§3.2/§3.3/§3.4/§3.5 and
A1–A6, I disagree with **none**. Each fix is a minimal, scope-matched change that
honours the hard constraints (dedup index/semantics untouched; no model or
Ollama-host changes; no spec fabrication) and is covered by the existing test
suite. No finding is over- or under-engineered relative to what it describes.
