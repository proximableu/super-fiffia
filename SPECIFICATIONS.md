# SPECIFICATIONS — Troubleshooting Assistant ("super-fiffia")

> The **how**. Read `REQUIREMENTS.md` first for scope and locked decisions.
> Implementation-level detail (exact DDL, schemas, signatures, endpoint JSON) lives in `CONTRACT.md`.
> Task breakdown for the coding model lives in `AGENT.md` (§4) — build via `./run_task.sh <TASK_ID>`.
> **Stack note:** the older FastHTML/SQLAlchemy design was superseded. The operative stack is **FastAPI + psycopg v3 + one Postgres DB**. This document reflects that.

---

## 1. System Overview

```
                         ┌──────────────────────────────────────────────┐
                         │              super-fiffia app                │
   Browser (technician)  │  ┌───────────────┐       ┌────────────────┐  │
   ─────────────────────►│  │ WebUI (Jinja2 │       │  Backend API   │  │
   (HTML5 / HTMX)        │  │  + HTMX)      │       │  (REST/JSON)   │  │
                         │  │ /ingest /chat │       │ /api/*         │  │
                         │  └──────┬────────┘       └───────┬────────┘  │
   Script ──────────────►│         │      same FastAPI app   │           │
   (HTTP client)         │         ▼                         ▼           │
                         │  ┌──────────────────────────────────────┐    │
                         │  │           Application Core           │    │
                         │  │  ┌──────────┐  ┌─────────────────┐   │    │
                         │  │  │  Agent   │  │  Retrieval      │   │    │
                         │  │  │  Loop    │◄►│  (hybrid RRF)   │   │    │
                         │  │  └────┬─────┘  └────────┬────────┘   │    │
                         │  │       │                 │            │    │
                         │  │  ┌────▼─────────────────▼─────────┐  │    │
                         │  │  │   app/ollama.py (serialized)   │  │    │
                         │  │  │   embed + chat + structured    │  │    │
                         │  │  └────────────────────────────────┘  │    │
                         │  │        psycopg v3 pool               │    │
                         │  └───────────────┬──────────────────────┘    │
                         └──────────────────┼───────────────────────────┘
                                            │
                     ┌──────────────────────▼───────────────────────┐
                     │            PostgreSQL: fiffia_fs             │
                     │  records · records_audit · rag_chunks        │
                     │  (pgvector + pgcrypto, HNSW + GIN + btree)   │
                     └──────────────┬────────────────┬──────────────┘
                                    │                │
                          ┌─────────▼──────┐  ┌──────▼─────────────────┐
                          │ Ollama server  │  │ External stats backend │
                          │ LLM + embeddings│ │ (read-only role        │
                          │ one req at a time│ │  fs_stats_reader)     │
                          └────────────────┘  └────────────────────────┘
```

One **application process** (FastAPI, uvicorn) serves both the WebUI (HTML/HTMX via Jinja2) and the backend API (JSON). It talks to **one Postgres database** (`fiffia_fs`) with three tables (`records`, `records_audit`, `rag_chunks`) and to an **Ollama** server for LLM + embeddings. The external statistics backend reads the same database directly under a read-only role.

---

## 2. Technology Stack

| Layer | Choice | Notes |
|---|---|---|
| WebUI | **FastAPI + Jinja2 + HTMX** | Server-rendered, HTML5, partial swaps. No React/Vue, no SPA. |
| App framework | FastAPI (Starlette) | One process serves `/` pages and `/api/*` JSON. |
| DB access | **psycopg v3** (sync) + connection pool | Sync endpoints run in Starlette's threadpool; bound parameters everywhere. |
| Vector store | **PostgreSQL + pgvector** | `vector(768)`, HNSW cosine index. |
| Lexical search | Postgres `tsvector` (`simple` config, trigger-weighted) + GIN | Language-robust; optional `pg_trgm` later. |
| LLM / embeddings | **Ollama** over HTTP (`httpx`) | Single shared `threading.Lock` — one request at a time (NFR-2). |
| Future LLM | OpenAI-compatible endpoint | Swap inside `app/ollama.py`-equivalent provider seam (C-8). |
| Config | YAML files (`settings.yaml`, `taxonomy.yaml`) | Vocabulary + runtime config externalized. |
| Deployment | **Docker Compose** (provided) or venv + local Postgres | app + postgres(pgvector) + ollama. |
| Language | **Python 3.12** | Typed, modular, PEP 8. |

---

## 3. Component Breakdown

| Component | Responsibility |
|---|---|
| **WebUI** (`app/webui.py`, `templates/`, `static/`) | Submit form (cascade), record list/edit/archive, chat stage, language selector, busy indicators. |
| **Backend API** (`app/api.py`) | REST/JSON CRUD + bulk + archive, hybrid search, chat, RAG ingest trigger, taxonomy cascade, health. Consumed by the script. |
| **Agent** (`app/agent.py`) | Turn loop: build prompt → structured `AgentAction` → dispatch tool → repeat; bounded by budget. |
| **Chat orchestration** (`app/chat.py`) | Scope + messages → agent → `{answer, sources, turns_used}`; in-memory per-session history. |
| **Retrieval** (`app/retrieval.py`) | Scoped hybrid RRF search over `records` and `rag_chunks`; metadata filters; lexical-only fallback. |
| **Ollama client** (`app/ollama.py`) | Shared HTTP client + `OLLAMA_LOCK` (serializes all Ollama calls). |
| **Embedding** (`app/embedding.py`) | `embed(texts) -> list[list[float]]`, `EMBED_MODEL`, `EMBED_DIM`; typed errors. |
| **Records repo** (`app/records_repo.py`) | Insert with dedup (catches unique violation), get, list, archive/restore, audit writes. |
| **Records service** (`app/records_service.py`) | The pipeline: validate → hash → embed → insert. |
| **Taxonomy** (`app/taxonomy.py`) | Load/validate `config/taxonomy.yaml`; cascade helpers. |
| **Hashing** (`app/hashing.py`) | `content_hash` (MD5, normalized failure+solution, NUL separator). |
| **RAG** (`app/rag.py`) | Chunker + idempotent ingestion into `rag_chunks`. |
| **DB** (`app/db.py`) | psycopg pool + idempotent migration runner (`schema_migrations`). |
| **Config** (`app/config.py`) | Load `settings.yaml` + `taxonomy.yaml` into typed objects. |
| **Ingest script** (`scripts/ingest_rag.py`) | CLI wrapper around `app/rag.ingest` (chunk + embed + upsert). |

---

## 4. Data Model

One database `fiffia_fs`. Full DDL in `CONTRACT.md` §5 and `F&S_REQUIREMENTS.md` §5.

### 4.1 `records` — knowledge records (F&S)

Single table. One row = one failure↔solution pair.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `category` / `product` | `text` NOT NULL | Denormalized taxonomy (indexed) — serves scoping **and** statistics. |
| `article_number` | `text` NULL | Validated against config rule; per-product enumerator. |
| `failure_description` | `text` NOT NULL | Primary search target (also the embedding source). |
| `solution_description` | `text` NOT NULL | |
| `content_hash` | `char(32)` NOT NULL | MD5 of normalized failure+solution (failure+solution only). Dedup key. |
| `ncr` / `bug_record_number` | `text` NULL | Provenance references. |
| `source` | `text` NOT NULL default `'manual'` | `manual` \| `import` \| `api`. |
| `created_by` | `text` NULL | `webui` / `api` / client name. |
| `status` | `text` NOT NULL default `'active'` | `active` \| `archived` (soft delete). |
| `embedding` | `vector(768)` | Embedding of `failure_description`. |
| `fts` | `tsvector` | Trigger-maintained; failure weighted A, solution weighted B. |
| `embed_model` / `embed_dim` | `text` / `int` | Embedding provenance (safe model upgrades). |
| `created_at` / `updated_at` | `timestamptz` | `created_at` immutable; `updated_at` via trigger. |

Indexes: unique partial on `content_hash WHERE status='active'` (dedup authority, block default); btree on taxonomy columns, `status`, `created_at`; HNSW on `embedding`; GIN on `fts`.

**`records_audit`** — append-only log: `record_id`, `action` (create/update/archive/restore), `changed` (JSONB `{field:{old,new}}`), `actor`, `at`.

> `solution_embedding` is **out of scope** for v1 (search targets the failure description). Trivial to add later as a second vector column.

### 4.2 `rag_chunks` — documentation chunks

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `source_file` | `text` NOT NULL | Provenance. |
| `chunk_index` | `int` NOT NULL | Position within the file; part of the idempotency key. |
| `section_header` | `text` NULL | Nearest heading. |
| `chunk_text` | `text` NOT NULL | |
| `content_hash` | `char(64)` NOT NULL | sha256(source_file + "\0" + chunk_text) — traceability. |
| `embedding` | `vector(768)` | |
| `fts` | `tsvector` | Trigger-maintained; header weighted A, text weighted B. |
| `created_at` | `timestamptz` | |

Indexes: unique `(source_file, content_hash)` (idempotency authority); HNSW on `embedding`; GIN on `fts`.

---

## 5. Retrieval Design (hybrid RRF)

**Goal:** scope by structured metadata first, then combine semantic + lexical ranking, language-robust.

0. **Structured scoping (first)** — when the caller has selected `category` and `product` (and optionally `article_number`), the candidate set is first restricted by a plain indexed query `WHERE category=? AND product=? [AND article_number=?] AND status='active'`. All ranking below runs **within** that scoped set.
1. **Semantic leg** — cosine nearest-neighbour on the 768-dim embedding (HNSW), top `POOL` (default 30).
2. **Lexical leg** — `ts_rank` over `fts` using `plainto_tsquery('simple', q)`, top `POOL`.
   - `simple` config = tokenization **without stemming** → safe for mixed Swedish/English and technical tokens (part numbers, error codes).
   - Optional enhancement: `pg_trgm` `similarity()` for substring/partial-code matching (phase 2).
3. **Fusion** — **Reciprocal Rank Fusion**: `score = Σ 1/(K + rank)` with `K = 60`, over a `FULL OUTER JOIN` of the two legs.
4. **Truncate** to `TOP_K` (default 10 for records, 8 for RAG; configurable).
5. **Fallback** — if query embedding fails, run the lexical leg only and log a warning (never fail the search).

Exact SQL in `CONTRACT.md` §8.

---

## 6. Agent Loop

### 6.1 Decision contract (structured output)

The agent emits a typed `AgentAction` (Pydantic) via Ollama structured output (`format=`):

```
AgentAction {
  thought: str                       # reasoning (logged, not shown verbatim)
  action: "search_records" | "search_rag" | "ask_clarification" | "final_answer"
  query: str?                        # reformulated query for a search action
  filters: {category?, product?, article_number?}?   # optional metadata filters
  answer: str?                       # required when action == final_answer
  clarification: str?                # required when action == ask_clarification
}
```

### 6.2 State machine

```
        ┌────────────┐
 START ►│ build ctx  │   (system prompt by lang + history + scope note + user message)
        └─────┬──────┘
              ▼
        ┌────────────┐   ask_clarification   ┌──────────────────┐
        │ LLM decide │──────────────────────►│ return question  │──► wait for user
        └─────┬──────┘                        └──────────────────┘     (end turn)
   ┌──────────┼─────────────────┐
   ▼          ▼                 ▼
search_   search_         final_answer
records    rag             ┌──────────────────┐
   │          │            │ return answer +  │──► END
   ▼          ▼            │ cited sources    │
append      append
results     results
to context  to context
   └──────────┬─────────────┘
              ▼
        turn < budget?  ──yes──► back to "LLM decide"
              │no
              ▼
     force final_answer from gathered context ──► END
```

### 6.3 Rules

- **Turn budget** default **5** (`agent.max_turns` in `settings.yaml`). Each LLM decision = one turn.
- **Context assembly:** system prompt (language-aware) + in-memory conversation history + scope note (selected category/product/article_number) + accumulated retrieval results + latest user message.
- **Scoped tools:** `search_records` runs **scoped-first** (structured `WHERE` from the UI context or agent `filters`, then hybrid RRF); `search_rag` is semantic (not scoped).
- **Termination guarantee:** the loop always ends — on `final_answer`, on `ask_clarification` (yields to user), or by forcing a final answer when the budget is exhausted. Parse failure → retry once → force `final_answer`.
- **No native tool-calling** — structured output only (deterministic, per decision C-11).
- **Decision logging:** every turn logs `action`, `query`, `filters`, selected source ids, and turn index (NFR-8).

### 6.4 Prompts & language

- System prompt is selected by UI language (Swedish default / English) and instructs the model to **answer in that language**, to **prefer citing stored records** before RAG docs, and to **ask a clarifying question** when the failure is under-specified. If the scoped set is empty, say so and broaden (clarify / RAG) — never hallucinate.
- The model is a **21B–35B instruct** model (default). The provider seam allows swapping in a reasoning model without code change (C-8/C-10).

---

## 7. LLM / Embedding Access

```
app/ollama.py   — OLLAMA_LOCK: threading.Lock (module-level, shared)
                  ollama_post(path, payload) -> dict   # httpx POST, raises OllamaError
app/embedding.py— embed(texts) -> list[list[float]]   # /api/embed, 768-dim, under lock
                  EMBED_MODEL / EMBED_DIM; EmbeddingError on failure
app/agent.py    — chat_structured(messages, schema)    # /api/chat with format=<json schema>
                  chat(messages) -> str                # plain completion
```

- **Call serialization:** every Ollama call happens under the single shared `OLLAMA_LOCK`, because the Ollama server handles **one request at a time** (NFR-2). Concurrent user requests queue; the WebUI shows the busy indicator meanwhile.
- **Future cloud path:** replace the transport inside the provider seam with an OpenAI-compatible `/v1` client (cloud, or local Ollama `/v1`). No other code changes.

---

## 8. RAG Ingestion Pipeline

`app/rag.py` (library) + `scripts/ingest_rag.py` (CLI) + `POST /api/rag/ingest` (API trigger). Idempotent:

1. Walk the source directory for `.md` / `.txt`.
2. Split into chunks by heading/paragraph; target ~2000 chars with small overlap; capture the nearest heading as `section_header`.
3. Prepend context (`source_file` + `section_header`) for embedding quality.
4. `embed()` each new chunk (768-dim) — batched, under the Ollama lock.
5. **Upsert** into `rag_chunks` keyed by `(source_file, content_hash)`; chunks already stored are not re-embedded. Re-running is safe.
6. Log counts (files, chunks, embedded, upserted). `--dry-run` reports without writing.

---

## 9. WebUI

Two stages, one page each (`/ingest`, `/chat`), top nav, shared taxonomy cascade, language selector (sv/en), three indicator states (progress / success / error) everywhere a server call is made. Full interaction spec: `WEBUI.md`.

| Route | Purpose |
|---|---|
| `/` | Redirect to `/ingest`. |
| `/ingest` | Ingest stage: submit form + record list. |
| `/ingest/list` | HTMX partial: filtered record list. |
| `/ingest/{id}/edit` | Edit form (GET/POST). |
| `/ingest/{id}/archive` | Soft delete (POST) → list re-render. |
| `/chat` | Chat stage: context header (scope) + conversation + Send. |
| `/chat/clear` | Reset in-memory conversation. |
| `/lang` | Set session language (sv/en), full re-render. |

- **Submit:** cascade selects → HTMX `POST /api/records` → success ack / duplicate 409 message / validation error. Progress spinner while embedding+inserting.
- **Chat:** blocking POST with `hx-indicator` busy spinner ("Agenten tänker… / Agent is thinking…") for the whole turn; answer + cited sources swap into the conversation.
- **History:** in-memory per session (C-19); "clear conversation" resets it.

---

## 10. Backend API (summary)

Full request/response JSON in `CONTRACT.md` §10.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/records` | Create record (201; duplicate → 409 + existing id; bad taxonomy → 422) |
| `POST` | `/api/records/check-duplicate` | Pre-check (warn UX) → `{duplicate, id?, created_at?}` |
| `GET` | `/api/records` | List (filters: `category`, `product`, `article_number`, `status`, `q`, paging) |
| `GET` | `/api/records/{id}` | Get one |
| `PUT` | `/api/records/{id}` | Update (re-validate, re-hash, re-embed if text changed; 409 if now duplicate) |
| `POST` | `/api/records/{id}/archive` | Soft delete → `status='archived'` |
| `POST` | `/api/records/{id}/restore` | Restore → `status='active'` (409 if hash now conflicts) |
| `POST` | `/api/records/bulk` | Bulk create/upsert, per-item transactional |
| `POST` | `/api/search` | Hybrid search (`source=records\|rag\|both`, `q`, optional scope, `top_k`) |
| `POST` | `/api/chat` | Agent turn → `{answer, sources, turns_used}` |
| `POST` | `/api/rag/ingest` | Trigger RAG ingestion |
| `GET` | `/api/health` | Liveness + DB/Ollama status |
| `GET` | `/api/taxonomy/products?category=` | Product options for a category (cascade) |
| `GET` | `/api/taxonomy/articles?category=&product=` | Article-number options for a product (cascade) |

No auth in v1 (C-17). Bind to the trusted network; document exposure risk (NFR-7).

---

## 11. External Configuration

### `config/settings.yaml` (runtime config)

```yaml
db:        {dsn, pool_min, pool_max}
ollama:    {base_url, embed_model, llm_model}
retrieval: {pool, top_k_records, top_k_rag, rrf_k}
agent:     {max_turns}
ui:        {lang_default}
stats:     {role_name, role_password}   # used by migration 0002
```

Optional env overrides: `FS_DSN`, `FS_OLLAMA_URL`. Exact schema in `CONTRACT.md` §3.

### `config/taxonomy.yaml` (vocabulary)

A single nested file defines the hierarchy **category → product → article_number**:

- `categories[]` — each with `id`, `label_sv`, `label_en`, and `products[]`.
- `products[]` — each with `id`, `label_sv`, `label_en`, and `article_numbers[]`.
- `article_numbers[]` — the allowed article numbers for that product (an **enumerator**; different sets per product).

The WebUI cascade (`WEBUI.md` §2) and API validation both read from this file. Changing it changes the form options and validation **without code changes**.

---

## 12. Deployment

**venv path (default dev flow):** `python -m venv venv` → `pip install -r requirements.txt` → Postgres with pgvector reachable per `settings.db.dsn` → `uvicorn app.api:app --port 8000`. Migrations run automatically at startup.

**Docker Compose (provided):** services `app` (build .), `postgres` (pgvector image; `docker/init_db.sql` creates `fiffia_fs`), `ollama` (pre-pulls LLM + embed model; optional GPU passthrough). App env: `FS_DSN`, `FS_OLLAMA_URL`.

---

## 13. i18n Strategy

- A small **translation table** (sv/en) for UI strings, selected by session language.
- Config vocabularies carry `label_sv` / `label_en` so dropdowns render in the active language.
- The agent's response language is set by the system prompt (FR-2.7).
- Retrieval is content-language-agnostic (`simple` tsconfig + multilingual-capable embeddings).

---

## 14. Error Handling & Edge Cases

- **Ollama busy / timeout** → provider call is serialized; on timeout, return a friendly "still thinking / please retry" (UI indicator stays up). No partial writes.
- **Embedding failure on ingest** → that record's insert is rolled back; others proceed (per-record transaction).
- **Duplicate insert** → unique partial index raises → `409` with existing `id` + `created_at` (block default; race-safe).
- **Restore conflict** → restoring an archived record whose `content_hash` now collides with another active record → `409`.
- **No retrieval hits** → agent is instructed to say so and ask a clarifying question rather than hallucinate.
- **Invalid `article_number`** → form + API reject with `422` and a clear message referencing the config rule.
- **Budget exhaustion** → forced best-effort final answer with the sources gathered.
- **Query-embedding failure during search** → lexical-only fallback + warning log.

---

## 15. Observability

- Structured logs (JSON) with: request id, stage, agent turn index, action, query, filters, selected source ids + scores, provider, latency.
- `/api/health` reports DB reachability and Ollama reachability.
- `records_audit` doubles as a human-readable change history per record.