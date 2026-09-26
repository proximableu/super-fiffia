# super-fiffia — F&S knowledge base

A small **Failures & Solutions** knowledge base. You store a *problem* / *fix*
pair once (as a code fix, a support note, an NCR, or a bug report); the same row
is retrieved either by **structured scope** (category → product → article
number, then semantic + lexical RRF) or answered over by an **agent** that can
iterate several times, citing sources.

The system stores deduplicated, soft-deleted rows in one PostgreSQL database and
serves two interfaces over the **same** data:

- a **REST API** (`app/api.py`) — records CRUD, dedup check, scoped search, the
  agent, taxonomy, and a dependency **health** probe;
- a **web UI** (`app/webui.py`) — Jinja2 templates that wrap the API.

---

## Stack

- **Python 3.12** · **FastAPI** · **Jinja2** (server-rendered templates)
- **PostgreSQL + pgvector** · **psycopg v3** (sync connection pool)
- **Ollama** for embeddings + the LLM
- Config in **YAML** (`config/settings.yaml`, `config/taxonomy.yaml`)

There is exactly **one** database, `fiffia_fs`, with three tables
(`records`, `records_audit`, `rag_chunks`). Rows are **never hard-deleted** —
archiving just flips `status` to `archived`, and every write is appended to
`records_audit`.

---

## Repository layout

```
app/
  api.py          FastAPI app factory + /api/* routes, health, error envelope
  webui.py        Jinja2 web routes (submit / list / edit / troubleshoot)
  agent.py        Structured-output agent (iterative retrieval tool loop)
  chat.py         One orchestrator turn + session history
  retrieval.py    Scoped semantic + lexical RRF over records & rag_chunks
  records_repo.py DB layer + Pydantic models (RecordIn/Out, Hit, Scope, ...)
  records_service.py  validate -> hash -> embed -> insert pipeline
  rag.py          Chunk + embed + idempotent upsert of RAG docs
  taxonomy.py     Category -> product -> article numbers cascade
  hashing.py      content_hash (MD5) for dedup
  embedding.py    embed(text) + EmbeddingError
  ollama.py       Shared Ollama client, serialised under OLLAMA_LOCK
  config.py       Loads settings.yaml + taxonomy.yaml into typed objects
  db.py           psycopg pool + idempotent migration runner
  logging.py      Structured JSON logging (NFR-8)
config/
  settings.yaml   Runtime config (DSN, Ollama URLs, tuning)
  taxonomy.yaml   Vocabulary (category -> product -> article numbers)
migrations/       Idempotent schema migrations (0001_init, 0002_stats_role)
sql/              sql/stats.sql — the external statistics queries
tests/            pytest suite (122 tests)
```

---

## Prerequisites

- Python 3.12
- A running PostgreSQL with the **pgvector** extension
- **Ollama** running with the configured models
  (`embed_model`, `llm_model` in `config/settings.yaml`)

The models are, by default:

- **embed:** `snowflake-arctic-embed2:568m`
- **LLM:** `gemma4:e4b`

Pull them into a local Ollama before starting the app:

```bash
ollama pull snowflake-arctic-embed2:568m
ollama pull gemma4:e4b
```

---

## Setup (venv)

```bash
# 1. Create and activate a Python 3.12 virtualenv
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create the database and role
#    (config/settings.yaml uses postgresql://fiffia:fiffia@localhost:5999/fiffia_fs
#     by default — adjust to match your deployment)
createdb fiffia_fs
psql -d fiffia_fs -c "CREATE ROLE fiffia WITH LOGIN PASSWORD 'fiffia' SUPERUSER;"

# 4. Apply the schema migrations
python -c "from app.db import run_migrations; print(run_migrations())"
```

`run_migrations()` is idempotent — safe to run repeatedly. It creates the three
tables, the pgvector / FTS indexes, the audit trigger, and the read-only
`fs_stats_reader` role.

### Configure

Edit `config/settings.yaml`:

- `db.dsn` — PostgreSQL connection string.
- `ollama.base_url` — e.g. `http://localhost:11434`.
- `ollama.embed_model` / `llm_model` — the two models.

Two environment overrides are honoured (used by Docker Compose — see below):

| Env var           | Overrides                          |
|-------------------|------------------------------------|
| `FS_DSN`          | `db.dsn`                           |
| `FS_OLLAMA_URL`   | `ollama.base_url`                  |

---

## Run

Start the API server (default port 8000):

```bash
uvicorn app.api:app
```

Start the web UI server:

```bash
uvicorn app.webui:app
```

The API is documented interactively at `http://localhost:8000/docs` and
`/redoc`.

### Health

```bash
curl localhost:8000/api/health
```

Returns `200` with `db` and `ollama` both `"ok"` when PostgreSQL and Ollama are
reachable:

```json
{"status":"ok","db":"ok","ollama":"ok","llm_model":"gemma4:e4b",
 "embed_model":"snowflake-arctic-embed2:568m"}
```

A missing dependency returns `503` with that dependency marked.

---

## Test

The suite is run against a dedicated **test** database (independent of any
running app). Point `TEST_DATABASE_DSN` at a Postgres database the test role can
write to; the fixture runs the migrations and truncates `records` /
`records_audit` before each test.

```bash
export TEST_DATABASE_DSN="postgresql://fiffia:local@127.0.0.1:5999/fiffia_fs"
pytest
```

> The test DSN in the example points at a local Postgres on port **5999** (not
> the default 5432). Adjust the host/port/credentials to your environment.

All Ollama calls (embeddings + LLM) go through the shared `OLLAMA_LOCK` in
`app/ollama.py`, so the tests serialize LLM access and never need a live
endpoint for the code paths that hit it.

---

## Structured logging (NFR-8)

The app logs **structured JSON**, one record per line, on the root logger. Every
`INFO` record (and above) looks like:

```json
{"ts":"2026-01-01T12:00:00.123456+00:00","levelname":"INFO","logger":"app.agent",
 "message":"agent turn 1: action=search_records ...","stage":"retrieval",
 "turn":1,"action":"search_records","query":"motor won't start",
 "filters":{"category":"engine"},"source_ids":["..."],"scores":[0.94],"latency_ms":12.3}
```

The schema covers the observability requirements in `CONTRACT.md`:

| Field        | Meaning                                                        |
|--------------|----------------------------------------------------------------|
| `ts`         | UTC timestamp (ISO-8601)                                       |
| `levelname`  | `DEBUG` / `INFO` / `WARNING` / `ERROR`                         |
| `logger`     | the logger name (e.g. `app.agent`)                            |
| `message`    | the human-readable log message                                |
| `stage`      | coarse bucket (`retrieval`, `agent`, `app`, ...)              |
| `turn`       | 1-based agent turn index                                      |
| `action`     | agent action (`search_records`, `search_rag`, ...)           |
| `query`      | the search / LLM query                                       |
| `filters`    | structured scope (category / product / article_number)        |
| `source_ids` | ids of the selected sources                                   |
| `scores`     | fused RRF scores of the selected sources                      |
| `latency_ms` | request latency (app-level) or per-stage latency              |

Per-request `request_id` is generated by the app middleware and attached to
every log record emitted while the request is in flight, plus the response
header `X-Request-Id`.

Override the JSON layout by subclassing `app.logging.JsonFormatter` and setting
it on a handler attached to the root logger; `setup()` installs the default one.

---

## Docker

A `docker-compose.yml`, `Dockerfile`, and `.env.example` for a full-stack
deployment (app + pgvector PostgreSQL + Ollama with models pre-pulled) are
provided in a later task. The containerised app reads the same
`FS_DSN` / `FS_OLLAMA_URL` environment overrides.

---

## API reference

Base URL: `http://localhost:8000`

| Method | Path                              | Purpose                                        |
|--------|-----------------------------------|------------------------------------------------|
| GET    | `/api/health`                     | Dependency probe (`db`, `ollama`)              |
| POST   | `/api/records`                    | Create a record (`source='api'`)               |
| POST   | `/api/records/check-duplicate`    | Pre-submit dedup check (the `warn` UX)         |
| GET    | `/api/records`                    | List / scoped search over records              |
| GET    | `/api/records/{record_id}`        | Fetch a single record                          |
| PUT    | `/api/records/{record_id}`        | Update a record (re-embeds if the text changed)|
| POST   | `/api/records/{record_id}/archive`| Soft-delete (sets `status=archived`)           |
| POST   | `/api/records/{record_id}/restore`| Restore (`409` if the hash now collides)       |
| POST   | `/api/records/bulk`               | Bulk create / update                           |
| POST   | `/api/search`                     | Semantic + lexical RRF over records + RAG      |
| POST   | `/api/chat`                       | One agent turn (iterative, cites sources)      |
| POST   | `/api/rag/ingest`                 | Chunk + embed + upsert RAG documents           |
| GET    | `/api/taxonomy/products`          | Products under a category (cascade)            |
| GET    | `/api/taxonomy/articles`          | Article numbers for a product (cascade)        |

### Error envelope

All failures return a JSON envelope:

```json
{"error": {"code": "duplicate", "message": "..."}}
```

Codes: `duplicate` | `invalid_taxonomy` | `not_found` | `validation` |
`internal`. A `duplicate` response also carries `existing_id` + `created_at`.

### Statistics backend

The `fs_stats_reader` role (created by `migrations/0002_stats_role.sql`) has
`SELECT` on `records` only — no write access, no access to `records_audit`.
Run the statistics queries directly against Postgres with that role:

```bash
PGPASSWORD=change_me psql -U fs_stats_reader -d fiffia_fs -f sql/stats.sql
```

See `sql/stats.sql` for the exact queries (counts by category/product, volume
over time, unique-failure counts, top article numbers).

---

## License

See the repository root for the applicable license.
