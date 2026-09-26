# AGENT.md — Coding guide for a local agentic coding model

> This file is the **source of truth** for a **local agentic coding model** (MoE ~35B total / 4B active) running in a **Linux CLI with a tool harness** (read/write files, run shell commands), **256k context** with **compaction**. It is given **one coding task at a time**. It tells the agent (a) how to work, (b) the invariants every task must respect, (c) the canonical context, and (d) the full task breakdown.

---

## 0. How to use this document

You are an **agentic CLI coding agent** with a **tool harness** (read files, write/edit files, run shell commands). You will be given **one task** (a task ID, e.g. `T1.2`). Do **only** that task.

**Workflow:**
1. **Read this file** (`AGENT.md`) first — §1 (rules), §2 (overview), §3 (canonical context), and your task in §4.
2. **Read the Context files** listed in your task (e.g. `SPECIFICATIONS.md` §1–2). The specs — `REQUIREMENTS.md`, `CONTRACT.md`, `F&S_REQUIREMENTS.md`, `WEBUI.md`, `TODO.md` — are authoritative; consult them whenever a detail is unclear.
3. **Implement** exactly the task's **Deliverable**. Stay scoped to those files.
4. **Verify** by running the task's **Acceptance** command (via your shell tool) and paste its output.
5. **Escalate** if blocked: stop and state exactly what is missing. Do not invent.

**Scope discipline:** read what you need from the files above; do not build other tasks; do not refactor beyond the Deliverable.

**Compaction:** your context (256k) will be compacted over a long session. To stay safe:
- Keep the **Critical invariants** (§1 + the list in the prompt) in mind; if you lose them, **re-read `AGENT.md` §1–§3**.
- Prefer **small, verifiable steps**; run the Acceptance command often.
- After compaction, re-confirm you are still on the assigned task ID before continuing.

---

## Prompt template (copy-paste per task)

Replace `<TASK_ID>` and paste into the CLI agent at the repo root. For tasks with a non-trivial Deliverable, also paste that task's §4 block (Goal / Deliverable / Acceptance) so it survives compaction.

```text
# ROLE
You are a CLI coding agent on Linux (bash, Python 3.12) with a tool harness
(read/write files, run shell commands). Work in the repository root.

# MISSION
Complete exactly ONE task: <TASK_ID>. Do not start any other task.

# HOW TO WORK
1. Read AGENT.md (repo root): §1 rules, §2 overview, §3 canonical context, §4 tasks. Your task is <TASK_ID>.
2. Read the Context files listed in <TASK_ID>. The specs (REQUIREMENTS.md, CONTRACT.md,
   F&S_REQUIREMENTS.md, WEBUI.md, TODO.md) are authoritative — consult them when unclear.
3. Implement exactly <TASK_ID>'s Deliverable.
4. Run <TASK_ID>'s Acceptance command and paste its output.
5. If blocked, STOP and state exactly what is missing. Do not invent.

# CRITICAL INVARIANTS (keep these even after context compaction)
- Stack: Python 3.12 · FastAPI · HTMX · PostgreSQL + pgvector · Ollama (embed + LLM) · YAML config · psycopg v3.
- One database `fiffia_fs`, three tables: `records`, `records_audit`, `rag_chunks`. No hard DELETE — archive/restore via `status`; audit every change.
- All Ollama calls (embed + LLM) go through `app/ollama.py` under the shared `OLLAMA_LOCK` — one request at a time.
- Field name is `article_number` (NEVER `article_nr`).
- Taxonomy is config-driven (category → product → article_numbers) from config/taxonomy.yaml; never hardcode.
- Enums: status ∈ {active, archived}; source ∈ {manual, import, api}.
- Dedup: content_hash = MD5(normalize(failure) + "\u0000" + normalize(solution)); block default (unique index → 409).
- Both submission paths (WebUI + API) run the SAME pipeline: validate → dedup → embed → insert.
- Retrieval is structured-first: scope by category+product(+article_number), then semantic+lexical RRF.

# CODE QUALITY
- No placeholders (no TODO/FIXME/stubs). Fully implement. PEP 8, full type hints, small functions.

# DEFINITION OF DONE
- Acceptance command passes (output pasted). Only <TASK_ID> files touched. No new lint/type errors.

# ESCALATION
If you cannot complete <TASK_ID> with the available files, STOP and list exactly what is missing.
```

---

## 1. Rules of engagement (apply to every task)

1. **One task at a time.** Keep changes scoped to the files in the Deliverable.
2. **No placeholders.** No `TODO`, `FIXME`, `pass  # later`, or stubbed bodies. Fully implement.
3. **Idiomatic Python 3.12**, PEP 8, full type hints. Small functions. No dead code.
4. **Never hardcode taxonomy values.** Read them from `config/taxonomy.yaml` via `app/taxonomy.py`.
5. **Field name is `article_number`** (never `article_nr`).
6. **Enums:** `status ∈ {active, archived}`; `source ∈ {manual, import, api}`.
7. **Dedup:** `content_hash = MD5(normalize(failure) + "\u0000" + normalize(solution))`; **block** is the default (unique index → `409` + existing id).
8. **Both submission paths (WebUI + API) run the SAME pipeline:** validate → dedup → embed → insert.
9. **Retrieval is structured-first:** scope by `category` + `product` (+ `article_number`), then semantic + lexical RRF within that set.
10. **Soft delete** via `status`; never `DELETE` a record.
11. **Embedding provenance:** always store `embed_model` + `embed_dim` on the row.
12. **Verify before done:** run the Acceptance command; paste output. If it fails, fix and re-run.
13. **If blocked**, state exactly what is missing (file, symbol, or decision). Do not invent.
14. **Serialize Ollama calls:** every embed/LLM HTTP call runs under the shared `OLLAMA_LOCK` in `app/ollama.py` — the Ollama server handles one request at a time (NFR-2).
15. **Embedding target:** `records.embedding` is the embedding of `failure_description` only (solution_embedding is out of scope for v1).

---

## 2. System overview (the invariants)

**What it is:** a Failures & Solutions (F&S) knowledge base with a WebUI (submit + troubleshooting chat) and a REST API (automated submit + search). One PostgreSQL database serves two consumers: (A) agent retrieval (RAG) and (B) an external backend that reads statistics **directly from the DB**.

**Tech stack:** Python 3.12 · FastAPI · HTMX (server-rendered, no SPA) · PostgreSQL + `pgvector` · Ollama (local embeddings + LLM) · YAML config · `psycopg` (v3).

**Repo layout (target):**
```
super-fiffia/
├── AGENT.md  REQUIREMENTS.md  SPECIFICATIONS.md  CONTRACT.md
├── WEBUI.md  F&S_REQUIREMENTS.md  TODO.md  README.md
├── requirements.txt
├── .env.example
├── config/
│   ├── settings.yaml          # db, ollama, retrieval, agent, ui, stats (exact: CONTRACT §3)
│   └── taxonomy.yaml          # category → product → article_numbers (exact: CONTRACT §4)
├── migrations/
│   ├── 0001_init.sql          # records, records_audit, rag_chunks, indexes, triggers
│   └── 0002_stats_role.sql    # read-only fs_stats_reader role + grants
├── sql/
│   └── stats.sql              # the statistics queries (for the external backend)
├── scripts/
│   └── ingest_rag.py          # CLI: chunk + embed + upsert RAG docs (wrapper around app.rag)
├── app/
│   ├── __init__.py
│   ├── config.py              # load settings.yaml + taxonomy.yaml
│   ├── db.py                  # psycopg pool + run_migrations()
│   ├── taxonomy.py            # cascade helpers + validation
│   ├── hashing.py             # content_hash (MD5)
│   ├── ollama.py              # shared Ollama HTTP client + OLLAMA_LOCK (serializes all calls)
│   ├── embedding.py           # embed() + EMBED_MODEL/EMBED_DIM + EmbeddingError
│   ├── records_repo.py        # DB access: insert/dedup/get/list/update/archive/restore + audit
│   ├── records_service.py     # the pipeline: validate → hash → embed → insert
│   ├── retrieval.py           # scoped semantic+lexical RRF (records + rag_chunks)
│   ├── rag.py                 # chunker + ingestion (idempotent)
│   ├── agent.py               # tools + turn budget (structured AgentAction loop)
│   ├── chat.py                # scope + messages → {answer, sources, turns_used}
│   ├── api.py                 # FastAPI app + /api/* routes
│   └── webui.py               # Jinja2 + HTMX routes (/ingest, /chat, /lang)
├── templates/
│   ├── base.html  submit.html  records_list.html  edit.html  troubleshooting.html
├── static/
│   ├── app.css  app.js
├── docker/
│   ├── init_db.sql            # CREATE DATABASE fiffia_fs (compose)
│   └── ollama_init.sh         # pull LLM + embed models
├── Dockerfile  docker-compose.yml
└── tests/
    ├── conftest.py            # test DB fixture (created in T0.2)
    ├── test_config.py  test_taxonomy.py  test_hashing.py  test_ollama.py
    ├── test_embedding.py  test_records_repo.py  test_records_service.py
    ├── test_retrieval_fs.py  test_rag_ingest.py  test_retrieval_rag.py
    ├── test_agent.py  test_chat.py  test_api.py
    └── test_webui.py          # WebUI app: routes + taxonomy cascade + render (in-process TestClient)
    └── ...
```

**Two consumers, one DB:**
- **Consumer A (agent retrieval):** scoped `WHERE` + HNSW semantic + GIN lexical → RRF top-k.
- **Consumer B (external stats):** connects to the DB **directly** as the read-only `fs_stats_reader` role and runs `GROUP BY` queries.

**Dual submission (first-class):**
- **Manual:** WebUI form → `POST /api/records`.
- **Automated:** external script (e.g., reading another DB) → `POST /api/records`.
- **Both** run the identical pipeline (validate → dedup → embed → insert). API submits may carry only `article_number`; the external script resolves it to `category`+`product` before posting.

---

## 3. Canonical context (reference — read when a task needs it)

### 3.1 Database schema (`migrations/0001_init.sql`)

> The exact authoritative copy lives in `CONTRACT.md` §5; the block below is identical (idempotent form).

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;          -- gen_random_uuid()

CREATE TABLE IF NOT EXISTS records (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- taxonomy (denormalized; indexed for scoping + statistics)
    category             TEXT        NOT NULL,
    product              TEXT        NOT NULL,
    article_number       TEXT,
    -- content
    failure_description  TEXT        NOT NULL,
    solution_description TEXT        NOT NULL,
    -- dedup: md5 hex of normalized failure+solution (failure+solution only)
    content_hash         CHAR(32)    NOT NULL,
    -- provenance / audit
    ncr                  TEXT,
    bug_record_number    TEXT,
    source               TEXT        NOT NULL DEFAULT 'manual',   -- manual | import | api
    created_by           TEXT,
    status               TEXT        NOT NULL DEFAULT 'active',   -- active | archived
    -- retrieval
    embedding            vector(1024),
    fts                  tsvector,
    -- embedding provenance (safe model upgrades)
    embed_model          TEXT,
    embed_dim            INTEGER,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- dedup: BLOCK is the default -> the unique index is the authority (race-safe)
CREATE UNIQUE INDEX IF NOT EXISTS uq_records_content_hash ON records (content_hash) WHERE status = 'active';

-- scoping + statistics
CREATE INDEX IF NOT EXISTS ix_records_category         ON records (category);
CREATE INDEX IF NOT EXISTS ix_records_category_product ON records (category, product);
CREATE INDEX IF NOT EXISTS ix_records_product          ON records (product);
CREATE INDEX IF NOT EXISTS ix_records_article          ON records (article_number);
CREATE INDEX IF NOT EXISTS ix_records_status           ON records (status);
CREATE INDEX IF NOT EXISTS ix_records_created_at       ON records (created_at);

-- retrieval
CREATE INDEX IF NOT EXISTS ix_records_embedding_hnsw   ON records USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_records_fts              ON records USING gin (fts);

-- keep fts in sync
CREATE OR REPLACE FUNCTION records_fts_trigger() RETURNS trigger AS $$
BEGIN
  NEW.fts := setweight(to_tsvector('simple', coalesce(NEW.failure_description,'')), 'A')
          || setweight(to_tsvector('simple', coalesce(NEW.solution_description,'')), 'B');
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_records_fts ON records;
CREATE TRIGGER trg_records_fts BEFORE INSERT OR UPDATE ON records
  FOR EACH ROW EXECUTE FUNCTION records_fts_trigger();

-- keep updated_at in sync
CREATE OR REPLACE FUNCTION records_touch() RETURNS trigger AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_records_touch ON records;
CREATE TRIGGER trg_records_touch BEFORE UPDATE ON records
  FOR EACH ROW EXECUTE FUNCTION records_touch();

-- append-only audit log (helps troubleshoot code / trace who changed what, when)
CREATE TABLE IF NOT EXISTS records_audit (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id     UUID NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    action        TEXT NOT NULL,              -- create | update | archive | restore
    changed       JSONB,                      -- {field: {old, new}}
    actor         TEXT,                       -- created_by / api client
    at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_records_audit_record ON records_audit (record_id, at DESC);

-- RAG documentation chunks (separate table, same database)
CREATE TABLE IF NOT EXISTS rag_chunks (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_file    TEXT NOT NULL,
    chunk_index    INTEGER NOT NULL,
    section_header TEXT,
    chunk_text     TEXT NOT NULL,
    content_hash   CHAR(64) NOT NULL,          -- sha256(source_file + "\x00" + chunk_text)
    embedding      vector(1024),
    fts            tsvector,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_chunk ON rag_chunks (source_file, content_hash);
CREATE INDEX IF NOT EXISTS ix_rag_hnsw ON rag_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_rag_fts  ON rag_chunks USING gin (fts);

CREATE OR REPLACE FUNCTION rag_fts_trigger() RETURNS trigger AS $$
BEGIN
  NEW.fts := setweight(to_tsvector('simple', coalesce(NEW.section_header,'')), 'A')
          || setweight(to_tsvector('simple', coalesce(NEW.chunk_text,'')), 'B');
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_rag_fts ON rag_chunks;
CREATE TRIGGER trg_rag_fts BEFORE INSERT OR UPDATE ON rag_chunks
  FOR EACH ROW EXECUTE FUNCTION rag_fts_trigger();
```

### 3.2 API contract (summary — exact JSON in `CONTRACT.md` §10)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | `200 {"status":"ok","db":"ok","ollama":"ok","llm_model":…,"embed_model":…}`; any dependency down → `503` with the failing component named |
| `POST` | `/api/records` | submit a record (manual **or** automated) → `201` `RecordOut`; duplicate → `409` (+ existing id/created_at); invalid taxonomy → `422` |
| `POST` | `/api/records/check-duplicate` | pre-check (`warn` UX) → `{duplicate, existing_id?, created_at?}` |
| `GET` | `/api/records` | list (filters: category/product/article_number/status; `q` → hybrid ordering; paging) → `{items, count}` |
| `GET` | `/api/records/{id}` | get one → `RecordOut` \| `404` |
| `PUT` | `/api/records/{id}` | update (re-validate, re-hash, re-embed on text change) → `RecordOut` \| `404` \| `409` \| `422` |
| `POST` | `/api/records/{id}/archive` | soft delete → `RecordOut` (`status='archived'`) |
| `POST` | `/api/records/{id}/restore` | restore → `RecordOut` (`status='active'`) \| `409` if the hash now collides |
| `POST` | `/api/records/bulk` | per-item transactional create/upsert → `{created, updated, errors}` |
| `POST` | `/api/search` | hybrid retrieval → `{results: [Hit], count}` (`source: records\|rag\|both`) |
| `POST` | `/api/chat` | agent turn → `{answer, sources: [Hit], turns_used}` |
| `POST` | `/api/rag/ingest` | run RAG ingestion → `{files, chunks, embedded, upserted}` |
| `GET` | `/api/taxonomy/products?category=` | products for a category (WebUI cascade) |
| `GET` | `/api/taxonomy/articles?category=&product=` | article_numbers for a product (WebUI cascade) |

**Shapes:**
```
RecordIn : { category, product, article_number?, failure_description, solution_description, ncr?, bug_record_number?, source? }
RecordOut: { id, category, product, article_number, failure_description, solution_description,
             ncr, bug_record_number, content_hash, source, status, embed_model, embed_dim,
             created_at, updated_at }
Hit      : { id, source: "records"|"rag", score,
             category?, product?, article_number?, failure_description?, solution_description?,
             ncr?, bug_record_number?,             # records hits
             source_file?, section_header?, chunk_text? }   # rag hits
```
**Error envelope:** `{ "error": { "code": "duplicate"|"invalid_taxonomy"|"not_found"|"validation"|"internal", "message": "..." } }` — a `duplicate` error additionally carries `existing_id` + `created_at`.

### 3.3 Taxonomy format (`config/taxonomy.yaml`)

```yaml
# category → product → article_numbers (an enumerator per product)
categories:
  - id: "hydraulics"
    label_sv: "Hydraulik"
    label_en: "Hydraulics"
    products:
      - id: "pump_a"
        label_sv: "Pump A"
        label_en: "Pump A"
        article_numbers:
          - "100-001"
          - "100-002"
      - id: "valve_b"
        label_sv: "Ventil B"
        label_en: "Valve B"
        article_numbers:
          - "200-010"
```
Rules: `category` and `product` are **required**; `article_number` is **optional** and must be a member of the selected product's list. Different products have different `article_number` sets.

### 3.4 Conventions

- **Hash:** `content_hash = MD5( norm(failure) + "\u0000" + norm(solution) )`, where `norm = strip → lower → collapse whitespace`. 32-char hex. Failure+solution **only** (a duplicate is a duplicate even across products/categories).
- **Dedup:** `block` default (unique index → `409`). `warn` is a pre-submission UX served by `POST /api/records/check-duplicate` — the unique index never weakens.
- **Retrieval:** structured-first (scope by category+product+article_number) → semantic (HNSW cosine) + lexical (GIN `ts_rank`) → **RRF** → top-k.
- **Embedding:** Ollama; the records embedding is computed from `failure_description` only; store `embed_model` + `embed_dim` per row.
- **Ollama access:** all HTTP calls (embed + LLM) go through `app/ollama.py` under the shared `OLLAMA_LOCK` — one request at a time (NFR-2).
- **Settings keys (`settings.yaml`):** `db.dsn/pool_min/pool_max`, `ollama.base_url/embed_model/llm_model`, `retrieval.pool/top_k_records/top_k_rag/rrf_k`, `agent.max_turns`, `ui.lang_default`, `stats.role_name/role_password` (exact: `CONTRACT.md` §3; env overrides `FS_DSN`, `FS_OLLAMA_URL`).
- **Soft delete:** `status='archived'` (excluded from retrieval + active stats; kept for history); restore back to `'active'` re-checks the unique hash (409 on collision).
- **Audit:** every create/update/archive/restore writes a `records_audit` row.
- **Language:** UI + agent responses in **Swedish (default) / English**.

---

## 4. Tasks (do in order; each is self-contained)

> For each task, the prompt = §1 + §2 + §3 + this task block.

### T0.1 — Project skeleton + config loader
- **Goal:** importable package, config files, and a typed config loader.
- **Context:** §3.3, §3.4, `SPECIFICATIONS.md` §1–2.
- **Deliverable:**
  - `requirements.txt` (fastapi, uvicorn, psycopg[binary], pydantic, pyyaml, httpx, pytest).
  - `app/__init__.py`.
  - `app/config.py` — load `config/settings.yaml` + `config/taxonomy.yaml` into typed objects; expose `settings` and `taxonomy`.
  - `config/settings.yaml` (exact schema: `CONTRACT.md` §3 — `db.dsn/pool_min/pool_max`, `ollama.base_url/embed_model/llm_model`, `retrieval.pool/top_k_records/top_k_rag/rrf_k`, `agent.max_turns`, `ui.lang_default`, `stats.role_name/role_password`).
  - `config/taxonomy.yaml` (sample per §3.3).
- **Acceptance:** `python -c "from app.config import settings, taxonomy; print(settings.ollama.base_url, settings.agent.max_turns); print([c.id for c in taxonomy.categories])"` prints without error.
- **Depends:** —

### T0.2 — DB connection + migration runner + `0001_init.sql`
- **Goal:** connect to Postgres and apply migrations idempotently.
- **Context:** §3.1 (full DDL), §3.4.
- **Deliverable:**
  - `app/db.py` — psycopg v3 connection pool; `run_migrations()` applies `migrations/*.sql` in filename order, tracking applied files in a `schema_migrations` table; performs the documented `{{ key.path }}` settings substitution before execution (needed by `0002_stats_role.sql`).
  - `migrations/0001_init.sql` — the full DDL from §3.1.
  - `tests/conftest.py` + `tests/test_db.py` — a test-DB fixture (`TEST_DATABASE_DSN` env overrides the DSN, else `settings.db.dsn`; runs `run_migrations()`) and a smoke test that migrations apply.
- **Acceptance:** `pytest tests/test_db.py` is green; `\dt` shows `records`, `records_audit`, `rag_chunks`.
- **Depends:** T0.1

### T1.1 — Taxonomy module
- **Goal:** load + validate the taxonomy; cascade helpers.
- **Context:** §3.3, §3.4.
- **Deliverable:** `app/taxonomy.py` — `products_for_category(cat) -> list`, `article_numbers_for_product(cat, prod) -> list`, `is_valid(cat, prod, art|None) -> bool`.
- **Acceptance:** `pytest tests/test_taxonomy.py` (write tests: valid/invalid combos; per-product enumerators; `article_number=None` is valid).
- **Depends:** T0.1

### T1.2 — `content_hash` (MD5)
- **Goal:** implement the dedup hash exactly per §3.4.
- **Context:** §3.4, `F&S_REQUIREMENTS.md` §5.1.
- **Deliverable:** `app/hashing.py` — `content_hash(failure: str, solution: str) -> str` (MD5, normalization, NUL separator).
- **Acceptance:** `pytest tests/test_hashing.py` (normalization; NUL separator avoids (a+b,c)/(a,b+c) collision; same text under different products → same hash; returns 32-char hex).
- **Depends:** —

### T1.3 — Ollama client + embedding
- **Goal:** a shared, serialized Ollama HTTP client; embed text via Ollama; batch; typed errors; expose model + dim.
- **Context:** §3.4, `CONTRACT.md` §7, `SPECIFICATIONS.md` §7.
- **Deliverable:**
  - `app/ollama.py` — `OLLAMA_LOCK: threading.Lock`, `ollama_post(path, payload) -> dict`, `chat(messages) -> str`, `chat_structured(messages, schema) -> str` (Ollama `/api/chat`, `format=<json schema>`); every call acquires the lock; typed `OllamaError`/`LLMError`.
  - `app/embedding.py` — `embed(texts: list[str]) -> list[list[float]]` (Ollama `/api/embed`), `EMBED_MODEL: str`, `EMBED_DIM: int`; raises `EmbeddingError` on failure.
- **Acceptance:** `pytest tests/test_ollama.py tests/test_embedding.py` (mock the Ollama HTTP call; assert shape `(n, dim)`; assert `EmbeddingError` on a 500; assert two threads calling `embed()` never overlap — sleep inside the mock and verify the lock serializes them).
- **Depends:** T0.1

### T1.4 — Records repository
- **Goal:** DB access for records (insert with dedup, get, list, archive) + audit writes.
- **Context:** §3.1 (DDL), §3.4.
- **Deliverable:** `app/records_repo.py` — `insert(record, content_hash, embedding, embed_model, embed_dim, actor) -> uuid` (catches the unique violation and raises `DuplicateError(existing_id, created_at)`), `get(id)`, `list(scope, status, q?, limit, offset)`, `update(id, record, actor)` (audit with `{field:{old,new}}`), `archive(id, actor)`, `restore(id, actor)`, `audit(record_id, action, changed, actor)`.
- **Acceptance:** `pytest tests/test_records_repo.py` (integration: insert; a second insert with the same hash → `DuplicateError`; a `records_audit` row exists for the insert; update writes an audit row with changed fields; archive → restore round-trip).
- **Depends:** T0.2, T1.2

### T1.5 — Records service (the pipeline)
- **Goal:** validate → hash → embed → insert; duplicates propagate as typed errors.
- **Context:** §3.1, §3.3, §3.4, `CONTRACT.md` §9.
- **Deliverable:** `app/records_service.py` — `submit(payload: RecordIn, actor) -> RecordOut` (validates taxonomy via `app/taxonomy.py`, computes hash, embeds, inserts; on `DuplicateError` **propagates** it — the API maps it to `409`, the WebUI to the duplicate indicator); `check_duplicate(failure, solution)`; `update_record`; `bulk` (per-item transactional).
- **Acceptance:** `pytest tests/test_records_service.py` (valid submit → `RecordOut`; invalid taxonomy → `InvalidTaxonomyError`; duplicate → `DuplicateError` with existing id).
- **Depends:** T1.1, T1.3, T1.4

### T2.1 — F&S scoped retrieval
- **Goal:** structured-first hybrid retrieval over `records`.
- **Context:** §3.1, §3.4, `CONTRACT.md` §8, `F&S_REQUIREMENTS.md` §7.
- **Deliverable:** `app/retrieval.py` — `retrieve_fs(scope: Scope, query: str, top_k: int | None) -> list[Hit]` (scope `WHERE` on category+product(+article_number), status='active', semantic + lexical RRF; lexical-only fallback when the query embedding fails).
- **Acceptance:** `pytest tests/test_retrieval_fs.py` (seed rows; a scoped query returns the expected rows ranked; a scope matching no rows → `[]`).
- **Depends:** T1.3, T0.2

### T2.2 — RAG chunker + ingestion
- **Goal:** chunk documents, embed, upsert into `rag_chunks` (idempotent).
- **Context:** §3.1 (`rag_chunks` DDL), `CONTRACT.md` §13.
- **Deliverable:**
  - `app/rag.py` — `chunk(text) -> list[(chunk_index, section_header | None, chunk_text)]`, `ingest(source_dir) -> RagIngestResult` (idempotent by `(source_file, content_hash)`: `ON CONFLICT DO UPDATE`; chunks whose hash is already stored are skipped and never re-embedded).
  - `scripts/ingest_rag.py` — thin CLI: `--source`, `--chunk-chars`, `--overlap`, `--dry-run` (counts without writing).
- **Acceptance:** `pytest tests/test_rag_ingest.py` (ingest a doc → chunks present with embeddings; re-ingest the same doc → no duplicate rows and no new embedding calls).
- **Depends:** T1.3, T0.2

### T2.3 — RAG retrieval
- **Goal:** hybrid retrieval over `rag_chunks`.
- **Context:** §3.1, `CONTRACT.md` §8.
- **Deliverable:** `app/retrieval.py` (extend) — `retrieve_rag(query, top_k) -> list[Hit]`.
- **Acceptance:** `pytest tests/test_retrieval_rag.py` (seed chunks; query returns ranked hits).
- **Depends:** T2.2

### T3.1 — Agent tools + turn budget
- **Goal:** tool layer (`search_records`, `search_rag`) driven by structured `AgentAction` output + a hard turn budget.
- **Context:** §3.4, `CONTRACT.md` §6 (AgentAction) + §11 (loop), `SPECIFICATIONS.md` §6.
- **Deliverable:** `app/agent.py` — `run_agent(scope, messages, lang, budget) -> AgentOutcome` (builds context from the language-aware system prompt + history + scope note; each turn calls `chat_structured` via `app/ollama.py` under the lock; parses `AgentAction` — retry once on parse failure, then force `final_answer`; dispatches `search_records` (scoped via `action.filters or scope`) and `search_rag`; collects/dedups hits; stops at the budget with a forced final answer from gathered context; logs each turn: action, query, filters, turn index).
- **Acceptance:** `pytest tests/test_agent.py` (mock the structured client with a scripted `AgentAction` sequence; assert `retrieve_fs` is called with the scope; assert clarification yields; assert budget exhaustion forces a final answer; assert parse-failure retry).
- **Depends:** T2.1, T2.3

### T3.2 — Chat orchestration
- **Goal:** scope + messages → agent → answer + sources + turns_used.
- **Context:** §3.4, `CONTRACT.md` §11 (chat), `WEBUI.md` §4.
- **Deliverable:** `app/chat.py` — `chat(scope, messages, lang) -> ChatResponse` (`{answer, sources, turns_used}`; wraps `run_agent` with `settings.agent.max_turns`; keeps per-session in-memory history; a clear action resets it).
- **Acceptance:** `pytest tests/test_chat.py` (a turn returns an answer + a `sources` list + `turns_used`; clear resets the history).
- **Depends:** T3.1

### T4.1 — FastAPI app + health + error envelope
- **Goal:** app factory, `/api/health`, and the error envelope.
- **Context:** §3.2, `CONTRACT.md` §10.
- **Deliverable:** `app/api.py` — `create_app()`, `GET /api/health`, exception handlers producing the §3.2 error envelope.
- **Acceptance:** `uvicorn app.api:app` then `curl -s localhost:8000/api/health` → `200`.
- **Depends:** T0.1

### T4.2 — Records endpoints (submit + pre-check + list)
- **Goal:** `POST /api/records`, `POST /api/records/check-duplicate`, `GET /api/records`.
- **Context:** §3.2, `CONTRACT.md` §10.
- **Deliverable:** `app/api.py` (extend) — the three endpoints wired to `records_service`.
- **Acceptance:** `curl` tests: valid → `201`; duplicate → `409` with `existing_id` + `created_at` in the error body; invalid taxonomy → `422`.
- **Depends:** T1.5, T4.1

### T4.3 — Search + chat + RAG ingest endpoints
- **Goal:** `POST /api/search`, `POST /api/chat`, `POST /api/rag/ingest`.
- **Context:** §3.2, `CONTRACT.md` §10, §13.
- **Deliverable:** `app/api.py` (extend) — all three endpoints wired to `retrieval` / `chat` / `rag.ingest`.
- **Acceptance:** `curl` tests: `/api/search` returns `{results, count}`; `/api/chat` returns `{answer, sources, turns_used}`; `/api/rag/ingest` returns `{files, chunks, embedded, upserted}` and is idempotent.
- **Depends:** T2.2, T3.2, T4.1

### T4.4 — Record mutation endpoints (get/update/archive/restore/bulk)
- **Goal:** complete the record lifecycle over the API — read one, update, soft delete, restore, bulk.
- **Context:** §3.2, `CONTRACT.md` §9 + §10, `F&S_REQUIREMENTS.md` §9.
- **Deliverable:** `app/api.py` (extend) — `GET /api/records/{id}`, `PUT /api/records/{id}` (re-validate → re-hash → re-embed only when `failure_description` changed → 409 on hash collision), `POST /api/records/{id}/archive`, `POST /api/records/{id}/restore` (409 on hash collision), `POST /api/records/bulk` (per-item transactional, one bad item does not abort the batch).
- **Acceptance:** `curl` tests: get → 200/404; update changes fields and writes an audit row; archive → `status='archived'`; restore → back to `'active'`; bulk → `{created, updated, errors}` with a bad item isolated.
- **Depends:** T1.5, T4.2

### T5.1 — HTMX base layout + static
- **Goal:** base template, top nav, CSS/JS, language selector.
- **Context:** `WEBUI.md` §1, §5, §6.
- **Deliverable:** `templates/base.html`, `static/app.css`, `static/app.js`, `app/webui.py` (routes `/ingest`, `/chat`, `POST /lang`).
- **Acceptance:** server up; `GET /ingest` and `GET /chat` render the base layout with nav.
- **Depends:** T4.1

### T5.2 — Submit form
- **Goal:** cascade selects + collapsible optional group + indicators.
- **Context:** `WEBUI.md` §2, §3.
- **Deliverable:** `templates/submit.html`; `GET /api/taxonomy/products` + `GET /api/taxonomy/articles`; submit wiring (`hx-post /api/records`, `hx-indicator`).
- **Acceptance:** manual: category→product→article cascade works; Submit shows success; a duplicate shows the `409` message; Clear resets.
- **Depends:** T5.1, T4.2

### T5.3 — Troubleshooting form + chat subform
- **Goal:** context header + conversation area + Send (HTMX partials).
- **Context:** `WEBUI.md` §4.
- **Deliverable:** `templates/troubleshooting.html`; chat partials; `POST /chat` + `POST /chat/clear` wiring; busy indicator `#chat-busy` on the blocking POST.
- **Acceptance:** manual: send a query → answer + sources appear; a follow-up works; Clear resets the conversation.
- **Depends:** T5.1, T4.3

### T5.4 — Record list + edit + archive
- **Goal:** browse/filter records, edit, archive/restore (soft delete) in the WebUI (FR-1.6).
- **Context:** `WEBUI.md` §3.5, `CONTRACT.md` §12.
- **Deliverable:** `templates/records_list.html` (filterable list partial: category/product/article_number/status/q), `templates/edit.html`; `app/webui.py` routes `/ingest/list`, `/ingest/{id}/edit` (GET/POST), `/ingest/{id}/archive`, `/ingest/{id}/restore` — wired to the T4.4 API endpoints.
- **Acceptance:** manual: list filters by category/product/status + free text; edit saves (text change re-embeds; duplicate → 409 message); archive hides the record from the default list; restore brings it back.
- **Depends:** T5.1, T4.4

### T6.1 — Read-only role + stat queries
- **Goal:** the `fs_stats_reader` role and the statistics SQL.
- **Context:** `F&S_REQUIREMENTS.md` §8.
- **Deliverable:** `migrations/0002_stats_role.sql` (role + `GRANT SELECT`); `sql/stats.sql` (counts by category/product, volume over time, unique failures, top article_numbers).
- **Acceptance:** connect as `fs_stats_reader`; the four queries in `sql/stats.sql` run; `INSERT` is denied.
- **Depends:** T0.2

### T6.2 — Full test suite + logging + run instructions
- **Goal:** all tests green; structured JSON logging; a `README.md` with setup/run/test steps.
- **Context:** all of §3, `CONTRACT.md` §14.
- **Deliverable:** complete `tests/` (`conftest.py` test-DB fixture exists since T0.2 — extend as needed); structured JSON logging (request id, agent turn index, action, query, filters, source ids + scores, latency — NFR-8); `README.md` (venv path + Docker path + config guide + API reference).
- **Acceptance:** `pytest` is green; `uvicorn app.api:app` serves `/api/health`.
- **Depends:** all prior tasks

### T6.3 — Docker deployment
- **Goal:** `docker compose up` brings up the full stack (NFR-5).
- **Context:** `CONTRACT.md` §2–3, `SPECIFICATIONS.md` §12.
- **Deliverable:** `Dockerfile` (python:3.12-slim, installs `requirements.txt`, runs uvicorn); `docker-compose.yml` (services: `app`, `postgres` using a pgvector image, `ollama` with model pre-pull; env `FS_DSN`, `FS_OLLAMA_URL`); `docker/init_db.sql` (`CREATE DATABASE fiffia_fs` + app role); `docker/ollama_init.sh`; `.env.example` (all env vars incl. `FS_DSN`, `FS_OLLAMA_URL`).
- **Acceptance:** `docker compose up` → `curl -s localhost:8000/api/health` returns `200` with `db` and `ollama` `ok`.
- **Depends:** T0.2, T4.1

---

## 5. Definition of done (per task)

- The **Acceptance** command passes (output pasted).
- No new lint/type errors in the touched files.
- Existing tests still pass.
- No work outside the Deliverable.

## 6. Escalation

If a task cannot be completed with the given context, **stop** and report exactly what is missing (a file, a symbol, a config key, or a decision). Do not invent defaults that contradict §1–§3.
