# Design — super-fiffia

This document describes **how** super-fiffia is designed: its two-process
architecture, the data model, the agentic retrieval loop, and the operator-driven
RAG-routing feature. It is the "how" companion to `CONTRACT.md` (what the system
must do), `SPECIFICATIONS.md` (interface specs), `F&S_REQUIREMENTS.md`
(requirements), and `WEBUI.md` (UI spec). Unlike those, it is prose-first: read
this for the mental model, open the linked source for the mechanics.

> Design decisions that are **not yet in the code** still live in `FUTURE.md` — see
> the end of this file.

---

## 1. Big picture

super-fiffia is a small **Failures & Solutions** knowledge base for F&S (a
manufacturer of washing machines). You store a *problem / fix* pair once; the same
row is retrieved either by **structured scope** (category → product → article
number) or by an **agent** that iterates several times, citing sources.

```
                        ┌───────────────────────────────┐
                        │          PostgreSQL             │
                        │   fiffia_fs  (pgvector, :5999)  │
                        │  records · records_audit · rag   │
                        └───────────────┬─────────────────┘
                                        │ psycopg v3 pool
                    ┌───────────────────┼────────────────────┐
                    │                   │                     │
          ┌─────────▼─────────┐  ┌──────▼───────────┐  ┌───────▼───────────┐
          │     api.py :9000   │  │   webui.py :9001  │  │ stats reader role │
          │  REST API (headless)│  │ Jinja2 web pages  │  │ (external backend)│
          └─────────┬─────────┘  └──────┬───────────┘  └───────────────────┘
                    │                    │
                    └─────────┐   ┌──────┘
                              ▼   ▼
                        app/  service + data + agent layers
                              │
                        ┌─────▼─────┐
                        │  Ollama    │  /api/chat + /api/embed
                        │  (:11434)  │  serialised behind OLLAMA_LOCK
                        └───────────┘
```

Two HTTP services, on the **same data**:

- **`app/api.py`** — a headless REST API on port **:9000**. Records CRUD, dedup
  check, scoped search, the agent, taxonomy, and a dependency **health** probe.
  This is the primary integration surface.
- **`app/webui.py`** — server-rendered Jinja2 pages on port **:9001** for humans:
  the Submit form, the record list, the Troubleshooting chat. It talks to the API
  and also closes the :9000/9001 seam by serving taxonomy/records routes itself.

Both talk to one PostgreSQL database (`fiffia_fs`, **pgvector**, at
`127.0.0.1:5999`) and to one Ollama server (embeddings + LLM, behind
`OLLAMA_LOCK`).

### The three processes in one breath

| Process | Port | Role |
| --- | --- | --- |
| API (`app/api.py`) | :9000 | REST API + health |
| WebUI (`app/webui.py`) | :9001 | Jinja2 pages |
| PostgreSQL + pgvector | :5999 | data + vector index |

(An external statistics backend additionally connects to `fiffia_fs` as a
read-only role and runs `GROUP BY` queries on `records` — see `F&S_REQUIREMENTS.md`
§8 / `CONTRACT.md` §5.)

---

## 2. Repository layout

```
app/
  api.py            FastAPI app factory + /api/* routes, health, error envelope
  webui.py          Jinja2 web routes (submit / list / edit / troubleshoot)
  agent.py          Structured-output agent (iterative retrieval tool loop)
  chat.py           One orchestrator turn + session history + RAG routing
  retrieval.py      Scoped semantic + lexical RRF over records & rag_chunks
  records_repo.py   DB layer + Pydantic models (RecordIn/Out, Hit, Scope, ChatTurn)
  records_service.py validate -> hash -> embed -> insert pipeline
  rag.py            Chunk + embed + idempotent upsert of RAG docs
  taxonomy.py       Category -> product -> article numbers cascade
  hashing.py        content_hash (MD5) for dedup
  embedding.py      embed(text) + EmbeddingError
  ollama.py         Shared Ollama client, serialised under OLLAMA_LOCK
  config.py         Loads settings.yaml + taxonomy.yaml into typed objects
  db.py             psycopg pool + idempotent migration runner
  logging.py        Structured JSON logging (NFR-8)
config/
  settings.yaml     Runtime config (DSN, Ollama URLs, tuning)
  taxonomy.yaml     Vocabulary (category -> product -> article numbers)
migrations/         Idempotent schema migrations
sql/                sql/stats.sql — the external statistics queries
tests/              pytest suite
```

The vertical stack is: **HTTP layers** (`api.py`, `webui.py`) → **orchestration**
(`chat.py`, `agent.py`) → **service logic** (`records_service.py`, `rag.py`,
`taxonomy.py`, `hashing.py`) → **data** (`records_repo.py`, `retrieval.py`,
`db.py`) → **platform** (`embedding.py`, `ollama.py`, `config.py`).

---

## 3. The database model

One database, `fiffia_fs`, with three tables (`records`, `records_audit`,
`rag_chunks`). Rows are **never hard-deleted** — archiving flips `status` to
`archived`, and every write is appended to `records_audit`. Migrations are
idempotent (the runner re-applies every migration at boot) and guarded by a
session-scoped `pg_advisory_lock` so concurrent boots can't race.

### `records`

| Column | Type | Purpose |
| --- | --- | --- |
| `id` | UUID | Primary key, `gen_random_uuid()` |
| `category` / `product` / `article_number` | TEXT | Denormalised taxonomy (indexed for scoping + stats) |
| `failure_description` / `solution_description` | TEXT | The stored problem/fix |
| `content_hash` | CHAR(32) | MD5 of normalised `failure + solution` (dedup) |
| `ncr` / `bug_record_number` | TEXT | Provenance |
| `source` | TEXT | `manual` \| `import` \| `api` |
| `created_by` | TEXT | Writer |
| `status` | TEXT | `active` \| `archived` |
| `embedding` | `vector(1024)` | Embedding (HNSW, cosine) |
| `fts` | `tsvector` | FTS index over `failure` (A) + `solution` (B) |
| `embed_model` / `embed_dim` | TEXT / INT | Embedding provenance, for safe model upgrades |
| `created_at` / `updated_at` | TIMESTAMPTZ | Audit timestamps |

Two indexes define the two retrieval legs: an **HNSW** index on `embedding`
(cosine) for the semantic leg, a **GIN** index on `fts` for the lexical leg. Two
triggers keep the `fts` column in sync on write and keep `updated_at` fresh.

### `rag_chunks`

The same shape, over documentation chunks instead of records: `(id, embedding::vector(1024), fts::tsvector, source_file, section_header, chunk_text, content_hash)`. A unique index on `(source_file, content_hash)` dedups chunks.

### `records_audit`

Append-only log: `id, record_id, event, created_at, created_by`. Every insert/
update/archive/restore is recorded here.

### Dedup is a DB-level invariant

The unique partial index
`CREATE UNIQUE INDEX uq_records_content_hash ON records (content_hash) WHERE status = 'active'`
is the authority for dedup. `records_service.submit()` also does an optimistic
`SELECT ... FOR UPDATE` check and raises `DuplicateError` before the insert, so
the failure is caught before the unique-constraint abort (cleaner envelope). The
hash depends on `failure + solution` **only**, so the same problem recorded under
different products still collides.

---

## 4. Retrieval: RRF hybrid

`app/retrieval.py` owns retrieval. Two entry points:

- **`retrieve_fs(scope, query, top_k=None)`** — records-first, scoped hybrid over
  `records`. The `scope` (category/product/article_number, all optional) is applied
  as a `WHERE` clause to **both** the vector and the lexical leg.
- **`retrieve_rag(query, top_k=None)`** — unscoped hybrid over `rag_chunks`.

Both embed the query and then run the **same** reciprocal-rank-fusion (RRF) merge:

```sql
SELECT
    id, source, score, ...
FROM (
    (SELECT id, 'records' AS source,
            1.0 / (rrf_k + rank) AS score
     FROM records
     WHERE embedding IS NOT NULL AND [scope] AND [query]
     ORDER BY embedding <-> %s::vector LIMIT 200)   -- vector leg
    UNION ALL
    (SELECT id, 'rag' AS source,
            1.0 / (rrf_k + rank) AS score
     FROM rag_chunks
     WHERE [query]
     ORDER BY fts plainto_tsquery(%s) LIMIT 200)    -- lexical leg
) ranked
GROUP BY id, source, score, ...
ORDER BY SUM(score) DESC
LIMIT %s;
```

Both legs keep a window of 200 candidates, their per-leg ranks are inverted and
summed by `id`, and the fused ranking is truncated to `top_k`. `rrf_k=60`
(`retrieval.rrf_k`) is the ranking-smoothing constant.

**Vector leg:** embed the query → `ORDER BY embedding <-> %s::vector`.
**Lexical leg:** `ORDER BY fts plainto_tsquery(%s)`.
**Fallback:** on `EmbeddingError` (e.g. the embedder is down) both fall back to
the lexical leg so search still works.

`Hit` (returned by both) is the single discriminated union over the two sources:

```python
@dataclass
class Hit:
    id: UUID
    source: Literal["records", "rag"]          # discriminates the two rows
    score: float
    # records fields:
    category: str
    product: str
    article_number: str
    failure_description: str
    solution_description: str
    ncr: str
    bug_record_number: str
    # rag fields:
    source_file: str
    section_header: str
    chunk_text: str
```

`retrieval.py:retrieve_fs` renders embeddings via `_as_vector()`: it stringifies a
float list as `"[0.1,-0.2]"` and binds it cast as `::vector`, so the bound value is
plain text.

---

## 5. The agent: an iterative retrieval loop

`app/agent.py` implements the structured-output agent (no native tool calling).
Every step is a single Ollama `chat_structured` call whose raw JSON is parsed into
an `AgentAction`, then dispatched to a retrieval tool:

```python
@dataclass
class AgentAction:
    thought: str
    action: Literal["search_records", "search_rag", "ask_clarification", "final_answer"]
    query: Optional[str]
    filters: Optional[Scope]        # model-supplied record scope
    clarification: Optional[str]
    answer: Optional[str]
```

### `run_agent(scope, messages, lang, budget=5, *, rag_first=False, rag_only=False)`

`messages` is the conversation history; the **last** message is the new user
turn. The loop:

1. **Build context** — a language-aware system prompt + prior turns + latest user
   message.
2. **Loop up to `budget` turns** (default 5, from `settings.agent.max_turns`):
   - call `chat_structured` → parse into an `AgentAction` (a parse failure is
     retried once; a second failure forces a best-effort `final_answer` so the loop
     always returns — NFR-6);
   - **dispatch** the action:
     - `search_records` → `retrieve_fs(action.filters or scope, query)`;
     - `search_rag` → `retrieve_rag(query)`;
     - `ask_clarification` → return with `ended_with='clarification'` (the
       clarification text is the answer);
     - `final_answer` → return with `ended_with='final_answer'`.
   - **Dedup retrieved `Hit`s by id** across turns (`collected` dict).
   - Feed the results back into the prompt as a `(no results)`-aware block
     (`_format_hits`), with the record/RAG details truncated.
3. **Budget exhausted without a terminal action** → force a best-effort final
   answer through `chat()` and return with `ended_with='budget_exhausted'`
   (NFR-6).

Turn decisions are logged per turn (action, query, filters, source ids, scores —
NFR-8). `AgentOutcome` carries `answer`, deduped `sources`, `turns_used`, and
`ended_with`.

### Retrieval order — `rag_first` / `rag_only`

The two flags are the seam through which the operator (see §6) steers retrieval:

- **`rag_only`** (highest precedence) — only `rag_chunks` is queried; stored
  records are **never** touched (operator tagged `#docs`).
- **`rag_first`** — RAG is queried first and records are the **fallback**: RAG
  only if RAG returns nothing.
- **neither** — the default `search_records` → records-first path.

The system prompt mirrors this so the model's ordering matches: when `rag_only`,
"Cite only documentation. When `rag_first`, "Cite docs then records. Otherwise
"cite records then docs." `lang='sv'` is Swedish (the default); `en` is the
fallback, and the agent answers in whatever language is selected.

---

## 6. The chat turn: RAG routing by operator intent

`app/chat.py` runs **one orchestrator turn**:

1. Fold the incoming user turn(s) into the per-session in-memory history
   (`_history.setdefault(session_token, [])`);
2. Compute retrieval flags from the **latest** turn's text (see below);
3. Call `run_agent(scope, list(history), lang, budget=..., rag_first=...,
   rag_only=...)`;
4. Append the returned answer back, trim history, return `ChatResponse`.

History is trimmed to the last `MAX_HISTORY_TURNS` (20) turns.

### The routing policy

The operator steers a turn with a **marker** in the message text. Resolution is
substring, case-insensitive, in `app/rag_routing.py`:

| Marker | Effect |
| --- | --- |
| `#fails` (`FAILS_MARKER`) | Force records-first (both flags off). Takes precedence over `#docs`. |
| `#docs` (`MARKER`) | `rag_only` — query RAG, never touch stored records. |
| *(no marker)* | Records-first on the first question, RAG-first on follow-ups. |

Precedence: **`#fails` > `#docs` > follow-up > default.**

```python
def resolve_records_first(turn: str) -> bool:
    """True when the turn should force records-first (#fails)."""
    return FAILS_MARKER in turn.lower()

def resolve_rag_first(turn: str) -> bool:
    """True when the turn should route RAG-only (#docs)."""
    return MARKER in turn.lower()
```

`chat.py` resolves them in precedence order:

```python
latest = messages[-1].content if messages else ""
fails_tagged = resolve_records_first(latest)
docs_tagged = resolve_rag_first(latest)
if fails_tagged:
    rag_first, rag_only = False, False          # #fails → force records-first
elif docs_tagged:
    rag_first, rag_only = False, True           # #docs   → RAG only
else:
    rag_first, rag_only = not is_first_turn, False  # follow-up → RAG first
```

The four-way outcome:

| Situation | `rag_first` | `rag_only` | Effective route |
| --- | --- | --- | --- |
| `#fails` present | off | off | records-first (fallback forced) |
| `#docs` present | off | **on** | RAG-only |
| first question, no marker | off | off | records-first |
| follow-up, no marker | **on** | off | RAG-first, records fallback |

RAG routing only fires for **follow-up** turns — the very first question stays
records-first so a brand-new session isn't surprised by a doc-heavy answer. The
state lives only in the per-session in-memory history (no server-side per-session
DB rows).

---

## 7. RAG ingest: chunk → embed → upsert

`app/rag.py` splits documents into overlapping text blocks and idempotently
upserts them into `rag_chunks`.

**`chunk(text, *, chunk_chars=8000, overlap=800)`** → `list[tuple[int, str | None,
str]]` of `(chunk_index, section_header, chunk_text)`:

- Text is grouped by the most recent **Markdown heading** (`^#{1,6}\s+(.+?)\s*$`);
- Paragraphs accumulate into chunks up to `chunk_chars` characters;
- Every chunk **except the last** reuses `overlap` characters from the end of the
  previous chunk so no context is lost across a boundary;
- A chunk **never crosses a section boundary** (a new heading flushes the buffer
  first), and a body longer than `chunk_chars` splits into successive chunk-sized
  pieces.

Sizes are **characters**, not model tokens — the project has no tokenizer for the
embedder's BPE, so `8000` / `800` approximate a ~2k-token chunk with a ~1k-token
overlap.

**`ingest(source_dir, *, chunk_chars=8000, overlap=800, dry_run=False)`** walks
`source_dir` for `*.md` / `*.txt` files, chunks each, embeds (behind
`OLLAMA_LOCK`, width-checked against `EMBED_DIM=1024`), and **upserts** keyed on a
SHA-256 of `(source_file, chunk_text)`. Re-ingesting an unchanged document
re-embeds nothing and upserts nothing. `RagIngestResult` reports
`(files, chunks, embedded, upserted)` (CONTRACT.md §6).

### Overridable at ingest time

The API `/api/rag/ingest` accepts the chunk parameters so they can be tuned per
ingest without touching defaults:

```python
@dataclass
class RagIngestRequest:
    source_dir: str = ""
    chunk_chars: int = 8000   # target chunk size, characters
    overlap: int = 800        # overlap carried across chunk boundaries, characters
```

These values (`chunk_chars=8000`, `overlap=800`) are the agreed-upon defaults and
must not be reverted.

---

## 8. Config

`app/config.py` loads two YAML files into typed dataclasses; the `settings` and
`taxonomy` singletons are the single source of truth.

| Section | Fields |
| --- | --- |
| `db` | `dsn`, `pool_min`, `pool_max` |
| `ollama` | `base_url`, `embed_model`, `llm_model` |
| `retrieval` | `pool`, `top_k_records`, `top_k_rag`, `rrf_k` |
| `agent` | `max_turns` |
| `ui` | `lang_default` |
| `stats` | `role_name`, `role_password` |

**Intentional, non-revertible config:**

- Embedder: `snowflake-arctic-embed2:568m`
- LLM: `gemma4:e4b`
- `EMBED_DIM`: `1024`
- FS uses `FS_OLLAMA_URL` for the Ollama base URL (override).

The Ollama base URL defaults to `http://localhost:11434` but is overridden by the
`FS_OLLAMA_URL` environment variable at boot.

### Constants

`EMBED_MODEL` = `settings.ollama.embed_model`, `EMBED_DIM = 1024`. The `embed()`
call validates the returned width against `EMBED_DIM` and raises `EmbeddingError`
on mismatch.

---

## 9. Concurrency & reliability

- **Ollama serialisation (NFR-2):** every Ollama call (embed + LLM chat) funnels
  through a single module-level `OLLAMA_LOCK` (a `threading.Lock`). Ollama handles
  one request at a time; the lock keeps concurrent requests from overloading it —
  callers queue. A single `httpx.Client` is reused for connection pooling.
- **Ollama failure surface:** non-2xx or a transport error is re-raised as
  `OllamaError`; a missing/empty content string as `LLMError`. Callers keep a
  single typed surface.
- **Budget (NFR-6):** `chat()` → `run_agent(..., budget=settings.agent.max_turns)`.
- **Turn logging (NFR-8):** structured JSON logs for each agent turn.

---

## 10. Data flow: submit and chat

**Submit a record:**
```
POST /api/records (RecordIn)
  → records_service.submit(payload, actor="web")
    → taxonomy.is_valid(cat, prod, art)          # config-driven vocab
    → hashing.content_hash(failure, solution)    # MD5 over failure+solution only
    → validate → hash → embed (behind OLLAMA_LOCK) → insert (idempotent dedup)
    → DuplicateError / InvalidTaxonomyError
```

**Ask a question:**
```
POST /api/chat (ChatRequest) or webui /chat POST
  → chat.chat(scope, messages, lang, session_token)
    → fold into per-session history
    → resolve #docs / #fails / follow-up → rag_first, rag_only
    → run_agent(..., rag_first, rag_only)
      → loop: chat_structured → AgentAction → _dispatch → retrieve_fs/retrieve_rag (RRF)
      → dedup hits → feed back → repeat (budget)
    → ChatResponse {answer, sources, turns_used, ended_with}
```

---

## 11. Design decisions not yet in code

Still-live open items live in `FUTURE.md` — this file does not pretend they are
implemented. The authoritative "what the code should look like" for pending work
stays there.
