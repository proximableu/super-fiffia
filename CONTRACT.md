# CONTRACT — Implementation Blueprint for the Coding Model

> **Audience:** the coding model that will implement this system (see `AGENT.md` for rules + task breakdown).
> **Rule:** This contract specifies **schemas, DDL, signatures, endpoint shapes, config, and behaviour** precisely. It intentionally does **not** contain full application code — you implement the bodies. Where a signature or schema is given, match it exactly. Where behaviour is described, satisfy it.
> **Read first:** `REQUIREMENTS.md` (scope), `SPECIFICATIONS.md` (design rationale), `F&S_REQUIREMENTS.md` (records/statistics design).
> **Stack (locked):** Python 3.12 · FastAPI + Jinja2 + HTMX · **psycopg v3 (sync)** · **one Postgres DB `fiffia_fs`** · Ollama over HTTP · YAML config.

---

## 0. Ground Rules

- Python **3.12**, fully typed (PEP 484/585). No `Any` where a type is known.
- **psycopg v3** with a `ConnectionPool` (sync). FastAPI endpoints are plain `def` (run in Starlette's threadpool). Every call uses **bound parameters** — no SQL string concatenation of user input, ever.
- **One database** (`fiffia_fs`), three tables: `records`, `records_audit`, `rag_chunks`. Schema changes only via `migrations/*.sql` applied by `app/db.run_migrations()` (idempotent, tracked in `schema_migrations`).
- **No hard deletes.** Records are archived/restored via `status`; every change is written to `records_audit`.
- **No streaming.** Chat turns are blocking requests.
- **Serialize all Ollama calls** (embed + LLM) under the single shared `threading.Lock` in `app/ollama.py` — one request at a time (NFR-2).
- Keep the layers decoupled (import only downward): `api`/`webui` → `agent`/`chat`/`records_service`/`retrieval` → `records_repo`/`rag`/`embedding`/`taxonomy`/`hashing` → `db`/`config`/`ollama`.
- No placeholders (`TODO`/`FIXME`/stubs). PEP 8, full type hints, small functions.

---

## 1. Repository Layout (create exactly this)

```
super-fiffia/
├── AGENT.md  REQUIREMENTS.md  SPECIFICATIONS.md  CONTRACT.md
├── F&S_REQUIREMENTS.md  WEBUI.md  TODO.md  README.md
├── requirements.txt
├── .env.example
├── config/
│   ├── settings.yaml          # runtime config (exact schema in §3)
│   └── taxonomy.yaml          # category → product → article_numbers (exact schema in §4)
├── migrations/
│   ├── 0001_init.sql          # records, records_audit, rag_chunks, indexes, triggers
│   └── 0002_stats_role.sql    # read-only fs_stats_reader role + grants
├── sql/
│   └── stats.sql              # the statistics queries (for the external backend)
├── scripts/
│   └── ingest_rag.py          # CLI: chunk + embed + upsert RAG docs (wrapper around app.rag)
├── app/
│   ├── __init__.py
│   ├── config.py              # load settings.yaml + taxonomy.yaml into typed objects
│   ├── db.py                  # psycopg pool + run_migrations()
│   ├── taxonomy.py            # cascade helpers + validation
│   ├── hashing.py             # content_hash (MD5)
│   ├── ollama.py              # shared HTTP client + OLLAMA_LOCK (serializes all Ollama calls)
│   ├── embedding.py           # embed() + EMBED_MODEL/EMBED_DIM + EmbeddingError
│   ├── records_repo.py        # insert/dedup/get/list/update/archive/restore + audit
│   ├── records_service.py     # pipeline: validate → hash → embed → insert
│   ├── retrieval.py           # scoped semantic+lexical RRF (records + rag_chunks)
│   ├── rag.py                 # chunker + idempotent ingestion
│   ├── agent.py               # tools + turn budget (structured AgentAction loop)
│   ├── chat.py                # scope + messages → {answer, sources, turns_used}
│   ├── api.py                 # FastAPI app + /api/* routes
│   └── webui.py               # Jinja2 routes (/ingest, /chat, /lang)
├── templates/
│   ├── base.html  submit.html  records_list.html  edit.html  troubleshooting.html
├── static/
│   ├── app.css  app.js
├── docker/
│   ├── init_db.sql            # CREATE DATABASE fiffia_fs (+ role setup for compose)
│   └── ollama_init.sh         # pull AGENT model + EMBED model
├── Dockerfile
├── docker-compose.yml
└── tests/
    ├── conftest.py            # test-DB fixture (created in T0.2, extended later)
    ├── test_config.py  test_taxonomy.py  test_hashing.py  test_ollama.py
    ├── test_embedding.py  test_records_repo.py  test_records_service.py
    ├── test_retrieval_fs.py  test_rag_ingest.py  test_retrieval_rag.py
    ├── test_agent.py  test_chat.py  test_api.py
    └── ...
```

---

## 2. Dependencies (`requirements.txt`, exact)

```
fastapi
uvicorn[standard]
psycopg[binary]
pydantic
pyyaml
httpx
jinja2
python-multipart
```

Dev (separate or commented block): `pytest`, `ruff`.

- `httpx` is used for Ollama calls **and** `fastapi.testclient` in tests.
- `python-multipart` is required for HTMX form-encoded POSTs.

---

## 3. Configuration File Schema (exact) — `config/settings.yaml`

```yaml
db:
  dsn: "postgresql://fiffia:fiffia@localhost:5432/fiffia_fs"
  pool_min: 1
  pool_max: 5
ollama:
  base_url: "http://localhost:11434"
  embed_model: "snowflake-arctic-embed2:568m"  # must output 1024 dims
  llm_model: "gemma4:e4b"                       # 21B–35B instruct class
retrieval:
  pool: 30          # candidates per leg before RRF
  top_k_records: 10 # final record hits returned
  top_k_rag: 8      # final RAG chunk hits returned
  rrf_k: 60         # RRF constant
agent:
  max_turns: 5      # agent turn budget
ui:
  lang_default: "sv"  # sv | en
stats:
  role_name: "fs_stats_reader"
  role_password: "change_me"          # used by migrations/0002_stats_role.sql
```

- `app/config.py` loads this into **typed objects** (Pydantic models or frozen dataclasses) and exposes module-level `settings` and `taxonomy`.
- **Env overrides:** `FS_DSN` overrides `db.dsn`; `FS_OLLAMA_URL` overrides `ollama.base_url` (used by Docker Compose).
- `app/db.run_migrations()` performs a naive `{{ key.path }}` substitution in migration SQL from `settings` before execution (e.g. `{{ stats.role_name }}`). Substitution must be explicit and documented.

---

## 4. Taxonomy File Schema (exact) — `config/taxonomy.yaml`

```yaml
categories:
  - id: "hydraulics"
    label_sv: "Hydraulik"
    label_en: "Hydraulics"
    products:
      - id: "pump_a"
        label_sv: "Pump A"
        label_en: "Pump A"
        article_numbers: ["100-001", "100-002"]
      - id: "valve_b"
        label_sv: "Ventil B"
        label_en: "Valve B"
        article_numbers: ["200-010"]
```

**Rules:**
- `category` options = top-level `categories[].id`.
- `product` options = `categories[<cat>].products[].id` (depend on the selected category).
- `article_number` options = `categories[<cat>].products[<prod>].article_numbers[]` (depend on the selected product); default = none/empty.
- **Validation:** `product` must belong to the chosen `category`; `article_number` (if set) must be a member of the chosen `product`'s `article_numbers`. Violation → `422`.
- `article_number` is an **enumerator** — different sets per product, never free text.
- Changing this file changes the form options and validation **without code changes**.

---

## 5. Database DDL (exact)

One database `fiffia_fs` (created by `docker/init_db.sql` in compose, or manually for the venv path).

### `migrations/0001_init.sql`

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
CREATE UNIQUE INDEX IF NOT EXISTS uq_records_content_hash
    ON records (content_hash) WHERE status = 'active';

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

-- keep fts in sync (failure weighted A, solution weighted B)
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

-- append-only audit log (trace who changed what, when)
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
    content_hash   CHAR(64) NOT NULL,        -- sha256(source_file + "\x00" + chunk_text)
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

### `migrations/0002_stats_role.sql`

```sql
-- Read-only role for the external statistics backend (after {{ }} substitution).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{{ stats.role_name }}') THEN
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', '{{ stats.role_name }}', '{{ stats.role_password }}');
  END IF;
END $$;

-- CONNECT (idempotent; database name resolved at runtime so test DBs work too)
DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), '{{ stats.role_name }}');
END $$;

GRANT USAGE ON SCHEMA public TO {{ stats.role_name }};
GRANT SELECT ON records TO {{ stats.role_name }};
-- No other grants: the role cannot read records_audit / rag_chunks, and cannot write.
```

### `sql/stats.sql` (for the external backend — connect as the read-only role)

```sql
-- counts by category / product
SELECT category, product, COUNT(*) AS n
FROM records WHERE status = 'active'
GROUP BY category, product ORDER BY n DESC;

-- volume over time (monthly)
SELECT date_trunc('month', created_at) AS m, COUNT(*) AS n
FROM records WHERE status = 'active'
GROUP BY m ORDER BY m;

-- unique failures (dedup-aware)
SELECT COUNT(DISTINCT content_hash) AS unique_failures
FROM records WHERE status = 'active';

-- most common article numbers
SELECT article_number, COUNT(*) AS n
FROM records
WHERE status = 'active' AND article_number IS NOT NULL
GROUP BY article_number ORDER BY n DESC LIMIT 20;
```

**Migration runner contract (`app/db.py`):** `run_migrations()` creates `schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ)` if missing, applies `migrations/*.sql` in filename order inside a transaction, records each applied file, and is safe to run repeatedly. Perform the `{{ key.path }}` settings substitution before execution.

---

## 6. Pydantic Schemas (exact)

```python
from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field

Source = Literal["manual", "import", "api"]
Status = Literal["active", "archived"]

class Scope(BaseModel):
    category: Optional[str] = None
    product: Optional[str] = None
    article_number: Optional[str] = None

class RecordIn(BaseModel):
    category: str
    product: str
    article_number: Optional[str] = None
    failure_description: str = Field(min_length=1)
    solution_description: str = Field(min_length=1)
    ncr: Optional[str] = None
    bug_record_number: Optional[str] = None
    source: Source = "manual"

class RecordOut(BaseModel):
    id: UUID
    category: str
    product: str
    article_number: Optional[str]
    failure_description: str
    solution_description: str
    ncr: Optional[str]
    bug_record_number: Optional[str]
    content_hash: str
    source: Source
    status: Status
    embed_model: Optional[str]
    embed_dim: Optional[int]
    created_at: datetime
    updated_at: datetime

class RecordBulkItem(RecordIn):
    id: Optional[UUID] = None          # if set -> update (must exist); else insert

class BulkResult(BaseModel):
    created: int
    updated: int
    errors: list[dict] = []            # [{"index": int, "code": str, "message": str}]

class Hit(BaseModel):
    """Unified search hit. `source` discriminates records from RAG chunks."""
    id: UUID
    source: Literal["records", "rag"]
    score: float                        # RRF score
    # records fields (None for rag hits)
    category: Optional[str] = None
    product: Optional[str] = None
    article_number: Optional[str] = None
    failure_description: Optional[str] = None
    solution_description: Optional[str] = None
    ncr: Optional[str] = None
    bug_record_number: Optional[str] = None
    # rag fields (None for record hits)
    source_file: Optional[str] = None
    section_header: Optional[str] = None
    chunk_text: Optional[str] = None

class SearchRequest(BaseModel):
    q: str = Field(min_length=1)
    source: Literal["records", "rag", "both"] = "records"
    scope: Optional[Scope] = None       # applied only to the records leg
    top_k: Optional[int] = None         # default per settings

class SearchResponse(BaseModel):
    results: list[Hit]
    count: int

class AgentAction(BaseModel):
    thought: str = Field(description="Reasoning about context quality and the next step.")
    action: Literal["search_records", "search_rag", "ask_clarification", "final_answer"]
    query: Optional[str] = Field(default=None, description="Reformulated query for a search action.")
    filters: Optional[Scope] = Field(default=None, description="Optional metadata filters for search_records.")
    clarification: Optional[str] = Field(default=None, description="Question to the user (ask_clarification).")
    answer: Optional[str] = Field(default=None, description="Final answer (final_answer).")

class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatTurn]            # last message = the new user message
    scope: Optional[Scope] = None       # UI context header (category/product/article_number)
    lang: Literal["sv", "en"] = "sv"

class ChatResponse(BaseModel):
    answer: str                          # a clarifying question arrives here as the answer text
    sources: list[Hit] = []
    turns_used: int

class RagIngestResult(BaseModel):
    files: int
    chunks: int
    embedded: int                        # newly embedded (chunks whose hash was not already stored)
    upserted: int

class DuplicateInfo(BaseModel):
    existing_id: UUID
    created_at: datetime
```

---

## 7. Ollama Access (signatures + serialization) — `app/ollama.py`, `app/embedding.py`

```python
# app/ollama.py
OLLAMA_LOCK: Final[threading.Lock] = threading.Lock()   # single shared lock (NFR-2)

def ollama_post(path: str, payload: dict) -> dict:
    """POST {base_url}{path} via httpx, timeout-configured; raises OllamaError on
    connection failure or non-2xx. Callers MUST hold OLLAMA_LOCK (or use the
    wrappers below that acquire it)."""

def chat(messages: list[dict]) -> str:
    """Plain completion: POST /api/chat {model, messages, stream: False}. Under lock."""

def chat_structured(messages: list[dict], schema: dict) -> str:
    """Structured output: POST /api/chat {model, messages, format: schema, stream: False}.
    Returns the raw JSON string. Under lock. Raises LLMError on failure/invalid JSON."""

# app/embedding.py
def embed(texts: list[str]) -> list[list[float]]:
    """POST /api/embed {model, input: texts} -> embeddings, one per input, same order.
    Acquires OLLAMA_LOCK. shape (n, 1024). Raises EmbeddingError on failure."""

EMBED_MODEL: str    # from settings.ollama.embed_model
EMBED_DIM: int      # from settings (1024)
```

**Serialization requirement (mandatory, NFR-2):** every Ollama HTTP call executes under `with OLLAMA_LOCK:`. A small-team burst of chat/search requests queues instead of overloading the server; the WebUI shows the busy indicator meanwhile.

---

## 8. Retrieval (signatures + SQL) — `app/retrieval.py`

```python
def retrieve_fs(scope: Scope, query: str, top_k: int | None = None) -> list[Hit]: ...
    # scope.category + scope.product required (structured-first); article_number optional.
    # Only status='active' rows. Empty scope (no matches) -> [].

def retrieve_rag(query: str, top_k: int | None = None) -> list[Hit]: ...

def hybrid_search(req: SearchRequest) -> SearchResponse: ...
    # records leg -> retrieve_fs(req.scope, q); rag leg -> retrieve_rag(q);
    # source=both -> merge, sort by score desc, truncate to top_k total.
```

### Records hybrid RRF query (psycopg v3 named parameters; `qvec` is the query embedding)

```sql
WITH vec AS (
    SELECT id, failure_description, solution_description,
           category, product, article_number, ncr, bug_record_number,
           ROW_NUMBER() OVER (ORDER BY embedding <=> %(qvec)s::vector) AS rank
    FROM records
    WHERE status = 'active'
      AND (%(category)s::text IS NULL OR category       = %(category)s::text)
      AND (%(product)s::text  IS NULL OR product        = %(product)s::text)
      AND (%(article)s::text  IS NULL OR article_number = %(article)s::text)
    ORDER BY embedding <=> %(qvec)s::vector
    LIMIT %(pool)s
),
txt AS (
    SELECT id, failure_description, solution_description,
           category, product, article_number, ncr, bug_record_number,
           ROW_NUMBER() OVER (ORDER BY ts_rank(fts, plainto_tsquery('simple', %(q)s)) DESC) AS rank
    FROM records
    WHERE status = 'active'
      AND fts @@ plainto_tsquery('simple', %(q)s)
      AND (%(category)s::text IS NULL OR category       = %(category)s::text)
      AND (%(product)s::text  IS NULL OR product        = %(product)s::text)
      AND (%(article)s::text  IS NULL OR article_number = %(article)s::text)
    LIMIT %(pool)s
)
SELECT
    COALESCE(v.id, t.id)                                     AS id,
    COALESCE(v.failure_description,  t.failure_description)  AS failure_description,
    COALESCE(v.solution_description, t.solution_description) AS solution_description,
    COALESCE(v.category, t.category)                         AS category,
    COALESCE(v.product, t.product)                           AS product,
    COALESCE(v.article_number, t.article_number)             AS article_number,
    COALESCE(v.ncr, t.ncr)                                   AS ncr,
    COALESCE(v.bug_record_number, t.bug_record_number)       AS bug_record_number,
    (COALESCE(1.0/(60 + v.rank), 0.0) + COALESCE(1.0/(60 + t.rank), 0.0)) AS rrf_score
FROM vec v
FULL OUTER JOIN txt t ON v.id = t.id
ORDER BY rrf_score DESC
LIMIT %(topk)s;
```

- `qvec` is passed as a string `"[0.012, -0.033, ...]"` and cast `::vector` (psycopg adapts Python floats; build the string explicitly).
- Params always bound: `%(name)s` placeholders via `cursor.execute(sql, params)` — never string-formatted.
- **Fallback:** `qvec = embed([q])[0]`; if the embedding call fails, run the lexical leg only and log a warning (never fail the search).
- The RAG query is identical in shape over `rag_chunks` (no metadata filters; map `source_file`/`section_header`/`chunk_text` into the `Hit` rag fields).

---

## 9. Records Pipeline (behaviour contract) — `app/records_service.py`, `app/records_repo.py`

```python
# app/records_repo.py
def insert(conn, record: RecordIn, content_hash: str, embedding: list[float],
           embed_model: str, embed_dim: int, actor: str) -> UUID: ...
    # Single INSERT ... RETURNING id; on unique violation (psycopg errors.UniqueViolation)
    # raise DuplicateError(existing_id, created_at). Writes records_audit(action='create').

def get(conn, record_id) -> RecordOut | None
def list_records(conn, scope: Scope, status: Status, q: str | None, limit, offset) -> tuple[list[RecordOut], int]
    # q present -> hybrid ordering (retrieve_fs internals); else ORDER BY created_at DESC.
def update(conn, record_id, record: RecordIn, content_hash, embedding|None, actor) -> RecordOut
    # Re-validates upstream; re-embeds only when failure_description changed;
    # writes records_audit(action='update', changed={field:{old,new}}).
    # If the new content_hash collides with another active record -> DuplicateError.
def archive(conn, record_id, actor) -> RecordOut      # status='archived'; audit action='archive'
def restore(conn, record_id, actor) -> RecordOut      # status='active'; audit action='restore';
                                                      # DuplicateError if hash now collides.
def audit(conn, record_id, action, changed, actor) -> None

# app/records_service.py — THE pipeline (both WebUI and API call exactly this)
def submit(payload: RecordIn, actor: str) -> RecordOut: ...
    # 1. taxonomy.is_valid(category, product, article_number) else InvalidTaxonomyError
    # 2. content_hash = hashing.content_hash(failure, solution)
    # 3. embedding = embedding.embed([failure])[0]
    # 4. records_repo.insert(...)
def check_duplicate(failure: str, solution: str) -> DuplicateInfo | None
def update_record(record_id, payload: RecordIn, actor: str) -> RecordOut
def bulk(items: list[RecordBulkItem], actor: str) -> BulkResult
    # Per-item transaction: one bad item does not abort the batch.
```

**Dedup (locked):** `content_hash = MD5(norm(failure) + "\u0000" + norm(solution))` with `norm = strip → lower → collapse whitespace` (`app/hashing.py`). Hash covers **failure+solution only** — the same failure+solution is a duplicate even across products/categories. `block` is the default: the unique partial index is the authority and a duplicate becomes `409` (race-safe, no check-then-insert window). The optional `warn` UX is served by `POST /api/records/check-duplicate` — the index never weakens.

---

## 10. Backend API Contract (exact JSON) — `app/api.py`

All endpoints return JSON. Error envelope:

```json
{"error": {"code": "duplicate|invalid_taxonomy|not_found|validation|internal", "message": "..."}}
```

A duplicate error additionally carries `"existing_id": "<uuid>"` and `"created_at": "<ts>"` inside the error object.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | `200 {"status":"ok","db":"ok","ollama":"ok","llm_model":"...","embed_model":"..."}`; any dependency down → `503` with the failing component named. |
| `POST` | `/api/records` | Body `RecordIn`. `201` → `RecordOut`. Duplicate → `409` (+ existing id/created_at). Invalid taxonomy → `422`. |
| `POST` | `/api/records/check-duplicate` | Body `{failure_description, solution_description}`. `200 {"duplicate": true, "existing_id": "...", "created_at": "..."}` or `{"duplicate": false}`. |
| `GET` | `/api/records` | Query: `category`, `product`, `article_number`, `status=active`, `q`, `limit`, `offset`. `200 {"items": [RecordOut], "count": int}` (`count` = total matching). `q` triggers hybrid ordering; without `q`, plain filtered list (`created_at` desc). |
| `GET` | `/api/records/{id}` | `200` `RecordOut` | `404`. |
| `PUT` | `/api/records/{id}` | Body `RecordIn`. `200` `RecordOut` | `404` | `409` (new hash collides) | `422`. Re-embeds only when `failure_description` changed. |
| `POST` | `/api/records/{id}/archive` | `200` `RecordOut` (`status='archived'`) | `404`. |
| `POST` | `/api/records/{id}/restore` | `200` `RecordOut` (`status='active'`) | `404` | `409` (hash now collides). |
| `POST` | `/api/records/bulk` | Body `{"items": [RecordBulkItem]}`. `200` `BulkResult` — per-item transactional (one bad item does not abort the batch). |
| `POST` | `/api/search` | Body `SearchRequest`. `200` `SearchResponse` `{results: [Hit], count}`. |
| `POST` | `/api/chat` | Body `ChatRequest`. `200` `ChatResponse` `{answer, sources: [Hit], turns_used}`. Blocking; serialized via `OLLAMA_LOCK`. |
| `POST` | `/api/rag/ingest` | Runs `app.rag.ingest` over the configured source dir. `200` `RagIngestResult` `{files, chunks, embedded, upserted}`. |
| `GET` | `/api/taxonomy/products?category=<id>` | `200 {"items": [{"id": "pump_a", "label_sv": "Pump A", "label_en": "Pump A"}]}`. Unknown category → `404`. |
| `GET` | `/api/taxonomy/articles?category=<id>&product=<id>` | `200 {"items": ["100-001", "100-002"]}`. Unknown pair → `404`. |

**Submission paths (locked):** the WebUI submit and the REST API both call `records_service.submit` — one identical pipeline: validate → hash → embed → insert. API-created records default `source="api"` (override per `RecordIn.source`); the external script resolves `article_number → category+product` itself before posting.

---

## 11. Agent Loop (behaviour contract) — `app/agent.py`, `app/chat.py`

```python
# app/agent.py
def run_agent(scope: Scope | None, messages: list[ChatTurn],
              lang: str, budget: int) -> AgentOutcome: ...
# AgentOutcome = {answer: str, sources: list[Hit], turns_used: int,
#                 ended_with: Literal["final_answer", "clarification", "budget_exhausted"]}
```

**Required behaviour (pseudocode, not to be copied verbatim):**
1. `context = [system_prompt(lang, scope)] + history + [user_message]`.
2. For `turn in 1..budget`:
   - `raw = chat_structured(context, AgentAction.model_json_schema())`
   - `action = AgentAction.model_validate_json(raw)` (on parse failure: retry once, then force `final_answer`).
   - log the turn (action, query, filters, turn index) — NFR-8.
   - dispatch:
     - `search_records` → `hits = retrieve_fs(action.filters or scope, action.query)`; append a compact context block of hits to `context`; add hits to `collected`.
     - `search_rag` → `hits = retrieve_rag(action.query)`; append hits; add to `collected`.
     - `ask_clarification` → **return** (answer = clarification text, `ended_with="clarification"`).
     - `final_answer` → **return** (`ended_with="final_answer"`).
3. Budget exhausted → build a final answer via `chat()` from the gathered context (instructed to answer in `lang` and cite sources) → return it (`ended_with="budget_exhausted"`).
4. `collected` = union of all `Hit`s seen this turn (dedup by id).

**Guarantees:** always returns; never loops past `budget`; every non-final path either yields to the user or terminates.

```python
# app/chat.py
def chat(scope: Scope | None, messages: list[ChatTurn], lang: str) -> ChatResponse: ...
    # Wraps run_agent with settings.agent.max_turns; keeps per-session in-memory
    # history (dict[session_token, list[ChatTurn]]); /chat/clear resets it.
```

---

## 12. WebUI Route Contract — `app/webui.py` (Jinja2 + HTMX)

| Route | Method | Behaviour |
|---|---|---|
| `/` | GET | Redirect to `/ingest`. |
| `/ingest` | GET | Submit form (new) + record list. Cascade selects from `config/taxonomy.yaml` (labels in active lang). |
| `/ingest` | POST | Server-side form fallback → `records_service.submit` → re-render (HTMX swap). |
| `/ingest/list` | GET | HTMX partial: filtered list (`category`, `product`, `article_number`, `q`, `status`). |
| `/ingest/{id}/edit` | GET/POST | Edit form / save (calls `records_service.update_record`). |
| `/ingest/{id}/archive` | POST | Archive (soft delete) → re-render list. |
| `/chat` | GET | Render scope header (cascade + failure description) + conversation (in-memory) + busy indicator. |
| `/chat` | POST | Blocking: `chat.chat(...)` → swap in answer + sources. `hx-indicator="#chat-busy"`. |
| `/chat/clear` | POST | Reset in-memory history. |
| `/lang` | POST | Set session language (`sv`/`en`), full re-render. |

**Busy indicator (mandatory):** `<div id="chat-busy" class="htmx-indicator">…spinner + "Agenten tänker… / Agent is thinking…"</div>`; shown for the full request duration (no streaming). Same pattern for the submit form (`#submit-busy`).

**Cascade:** changing `category` → `hx-get /api/taxonomy/products?category=<id>` swaps the `product` select (reset to `——`); changing `product` → `hx-get /api/taxonomy/articles?...` swaps the `article_number` select; changing `category` also resets `article_number`. Full interaction detail: `WEBUI.md`.

**In-memory history:** `dict[session_token, list[ChatTurn]]`; session token from a cookie. Single user. Cleared by `/chat/clear` and on process restart.

---

## 13. RAG Ingest Contract — `app/rag.py`, `scripts/ingest_rag.py`

```python
# app/rag.py
def chunk(text: str) -> list[tuple[int, str | None, str]]: ...
    # -> [(chunk_index, section_header | None, chunk_text)]
    # Split by heading/paragraph; target ~2000 chars, small overlap; nearest heading captured.

def ingest(source_dir: Path) -> RagIngestResult: ...
    # 1. Walk source_dir for .md/.txt.
    # 2. chunk(); content_hash = sha256(source_file + "\x00" + chunk_text).
    # 3. Skip chunks whose (source_file, content_hash) is already stored.
    # 4. embed() only the new chunks, in batches (e.g. 32) under the Ollama lock.
    # 5. Upsert by (source_file, content_hash):
    #      INSERT ... ON CONFLICT (source_file, content_hash)
    #      DO UPDATE SET chunk_index, section_header = EXCLUDED...
    #    An existing row's embedding is never recomputed; removed/edited chunks
    #    are not auto-deleted (delete that file's rows to re-ingest it fresh).
    # 6. Return RagIngestResult. Re-running is idempotent (no duplicate rows).
```

CLI (`scripts/ingest_rag.py`, thin wrapper):

```
python scripts/ingest_rag.py --source rag_source [--chunk-chars 2000] [--overlap 200] [--dry-run]
```

`--dry-run` reports counts without writing. `POST /api/rag/ingest` calls the same `ingest()`.

---

## 14. Testing & Acceptance Checklist

pytest; a test Postgres with pgvector (fixture in `tests/conftest.py`, created in T0.2); Ollama calls are **mocked** in unit tests.

- [ ] `test_config.py`: settings/taxonomy load; env overrides (`FS_DSN`).
- [ ] `test_taxonomy.py`: valid/invalid combos; per-product enumerators; `article_number=None` valid.
- [ ] `test_hashing.py`: normalization; NUL separator avoids `(a+b,c)`/`(a,b+c)` collisions; same text under different products → same hash; 32-char hex.
- [ ] `test_ollama.py`: two concurrent calls do not overlap (mock with a sleep; assert serialization via `OLLAMA_LOCK`); `OllamaError` on non-2xx.
- [ ] `test_embedding.py`: shape `(n, EMBED_DIM)`; `EmbeddingError` on a 500.
- [ ] `test_records_repo.py`: integration — insert; second insert with same hash → `DuplicateError`; `records_audit` row exists; update writes audit with `{old,new}`; archive/restore cycle.
- [ ] `test_records_service.py`: valid submit → `RecordOut`; invalid taxonomy → `InvalidTaxonomyError`; duplicate → existing returned/raised consistently.
- [ ] `test_retrieval_fs.py`: seeded rows; scoped query returns expected rows ranked; empty scope → `[]`; lexical-only fallback when embedding fails.
- [ ] `test_rag_ingest.py`: ingest a doc → chunks present with embeddings; re-ingest → no duplicate rows and no new embedding calls.
- [ ] `test_retrieval_rag.py`: seeded chunks; query returns ranked hits.
- [ ] `test_agent.py`: scripted `AgentAction` sequence — search→final_answer, ask_clarification yields, budget exhaustion forces final answer, parse-failure retry; `retrieve_fs` called with the scope.
- [ ] `test_chat.py`: a turn returns `{answer, sources, turns_used}`; clear resets history.
- [ ] `test_api.py`: CRUD round-trip; bulk per-item transactional; 422s for bad taxonomy; 409 duplicate body carries `existing_id`; `/api/search` shape; `/api/health` reflects DB/Ollama.
- [ ] Config: changing `taxonomy.yaml` changes form options + validation with **no code change**.
- [ ] i18n: `lang=sv` vs `en` changes UI labels and the agent's response language.
- [ ] Concurrency: two simultaneous chat requests are serialized (no Ollama overload); busy indicator present in HTML.

**Definition of done:** `docker compose up` (or the documented venv path) brings up app + postgres + ollama; a record can be created via the form and retrieved via chat; the script can bulk-import and search via the API; `fs_stats_reader` can run `sql/stats.sql` read-only; all tests green.