# TODO — Implementation Roadmap

> Phased build order. Each phase is independently shippable and testable.
> **Task IDs refer to `AGENT.md` §4** — assemble the per-task prompt with `./run_task.sh <TASK_ID>`.
> Exact schemas/DDL/JSON live in `CONTRACT.md`; rationale in `SPECIFICATIONS.md`.
> Tick boxes as work lands. A phase is "done" when its acceptance line is met.

---

## Phase 0 — Scaffolding & Data Foundation
- [ ] **T0.1** Project skeleton + typed config loader (`app/config.py`, `requirements.txt`, `config/settings.yaml`, `config/taxonomy.yaml`).
- [ ] **T0.2** DB connection + migration runner + `migrations/0001_init.sql` + `tests/conftest.py` test-DB fixture.
- **Acceptance:** `settings`/`taxonomy` import cleanly; `run_migrations()` succeeds and `\dt` shows `records`, `records_audit`, `rag_chunks`; conftest fixture usable.

## Phase 1 — Domain & Pipeline
- [ ] **T1.1** Taxonomy module (cascade helpers + validation).
- [ ] **T1.2** `content_hash` (MD5, normalization, NUL separator).
- [ ] **T1.3** Ollama client (`app/ollama.py`, shared `OLLAMA_LOCK`) + embedding client (`app/embedding.py`).
- [ ] **T1.4** Records repository (insert with dedup, get, list, update, archive/restore, audit).
- [ ] **T1.5** Records service — the pipeline: validate → hash → embed → insert.
- **Acceptance:** pytest green for `test_taxonomy.py`, `test_hashing.py`, `test_ollama.py`, `test_embedding.py`, `test_records_repo.py`, `test_records_service.py`; duplicate insert raises `DuplicateError`; audit rows written.

## Phase 2 — Retrieval & RAG
- [ ] **T2.1** F&S scoped retrieval (structured-first WHERE + semantic/lexical RRF).
- [ ] **T2.2** RAG chunker + idempotent ingestion (`app/rag.py` + `scripts/ingest_rag.py` CLI).
- [ ] **T2.3** RAG retrieval (`retrieve_rag`).
- **Acceptance:** seeded corpus returns ranked hits; filters scope correctly; empty scope → `[]`; lexical fallback on embed failure; re-ingest is idempotent (unchanged chunks are not re-embedded).

## Phase 3 — Agent & Chat
- [ ] **T3.1** Agent tools + turn budget (structured `AgentAction` loop, decision logging).
- [ ] **T3.2** Chat orchestration (`{answer, sources, turns_used}`, in-memory per-session history).
- **Acceptance:** scripted provider sequence exercises search→answer, clarification, budget-exhaustion, parse-retry paths; loop always terminates.

## Phase 4 — Backend API
- [ ] **T4.1** FastAPI app factory + `/api/health` + error envelope.
- [ ] **T4.2** Records endpoints: `POST /api/records`, `POST /api/records/check-duplicate`, `GET /api/records`.
- [ ] **T4.4** Record mutation endpoints: `GET/PUT /api/records/{id}`, `archive`/`restore`, `POST /api/records/bulk` (per-item transactional).
- [ ] **T4.3** Search + chat + RAG ingest endpoints: `POST /api/search`, `POST /api/chat`, `POST /api/rag/ingest`.
- **Acceptance:** curl tests — 201 valid, 409 duplicate (body carries `existing_id`), 422 invalid taxonomy; bulk per-item errors; `/api/search` and `/api/chat` shapes; `/api/rag/ingest` idempotent.

## Phase 5 — WebUI (FastAPI + Jinja2 + HTMX)
- [ ] **T5.1** Base layout, nav, CSS/JS, language selector (`templates/base.html`, `static/`, `app/webui.py` routes).
- [ ] **T5.2** Submit form: cascade selects, collapsible optional group, three indicator states, duplicate 409 message.
- [ ] **T5.3** Troubleshooting stage: scope header + conversation + Send (HTMX partials), busy indicator, `/chat/clear`.
- [ ] **T5.4** Record list + edit + archive: `templates/records_list.html`, `templates/edit.html`, filters, soft-delete button.
- **Acceptance:** create/edit/archive a record entirely in the browser; invalid `article_number` rejected with a clear message; a full troubleshooting conversation works in Swedish and English with the busy spinner shown for the whole turn.

## Phase 6 — Statistics, Hardening & Deployment
- [ ] **T6.1** Read-only role `fs_stats_reader` (`migrations/0002_stats_role.sql`) + `sql/stats.sql` queries.
- [ ] **T6.2** Full test suite green + structured JSON logging + `README.md` (setup/run/test steps).
- [ ] **T6.3** Docker: `Dockerfile`, `docker-compose.yml` (app + postgres/pgvector + ollama), `docker/init_db.sql`, `docker/ollama_init.sh`, `.env.example`.
- **Acceptance:** connect as `fs_stats_reader` — the four stat queries run, `INSERT` is denied; `pytest` green; `uvicorn app.api:app` serves `/api/health`; `docker compose up` brings the full stack up.

---

## Suggested build order & rough effort

| Order | Phase | Notes |
|---|---|---|
| 1 | 0 | Foundation: config, DB, migrations, test fixture. |
| 2 | 1 | Domain: taxonomy, hash, Ollama, repo, pipeline. |
| 3 | 2 | Retrieval + RAG corpus. |
| 4 | 3 | Agent brain + chat orchestration. |
| 5 | 4 | Script-facing API (T4.4 can follow T4.2 directly, before T4.3). |
| 6 | 5 | WebUI (submit → chat → list/edit/archive). |
| 7 | 6 | Stats role, tests/docs, Docker. |

> Phases 2–3 must be sequential; **T5.4 / T4.4** can be pulled forward in parallel once their dependencies land.
> Per-task prompts: `./run_task.sh T0.1` … `./run_task.sh T6.3`.