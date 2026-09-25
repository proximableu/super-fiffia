# REQUIREMENTS — Troubleshooting Assistant ("super-fiffia")

> Status: **Approved baseline** (reconciled with `AGENT.md` — the operative implementation contract).
> Companion docs: `SPECIFICATIONS.md` (how), `CONTRACT.md` (exact schemas/DDL/JSON), `F&S_REQUIREMENTS.md` (F&S database design), `WEBUI.md` (interaction), `TODO.md` (roadmap), `AGENT.md` (task breakdown for the coding model).
> This document states **what** the system must do and the constraints it must satisfy. It is the source of truth for scope.

---

## 1. Purpose & Scope

A web-based troubleshooting assistant for a small technical team. It stores **failure → solution** knowledge records plus a corpus of **technical documentation (RAG)**, and provides an **agentic chat** that retrieves from both sources to help a user diagnose and resolve a failure.

Three distinct usage stages:

1. **Ingest record** — create / edit / archive / list knowledge records (failure description + solution description + metadata).
2. **Chat with records** — a back-and-forth agentic conversation that retrieves, infers, and concludes a solution.
3. **Statistics (external)** — an external backend runs aggregate queries over the same records, read-only, directly against the DB. See `F&S_REQUIREMENTS.md`.

A **backend API** exposes the same data operations (CRUD + retrieval) for use **outside** the WebUI (consumed by a script).

---

## 2. Actors

| Actor | Description |
|---|---|
| **Technician (user)** | Single trusted user, read-write. Uses the WebUI to ingest records and chat with the agent. |
| **Script (external)** | Consumes the backend REST API for bulk ingest and programmatic retrieval. |
| **Agent** | The LLM-driven loop that decides what to retrieve and how to answer. Not a human actor, but a first-class component. |
| **Statistics backend (external)** | Connects to the Postgres database **directly** as a read-only role and runs aggregate queries. Never writes. |

---

## 3. Functional Requirements

### 3.1 Knowledge records (Ingest stage)

- **FR-1.1** The system shall store a record consisting of: `failure_description` (text), `solution_description` (text), and `metadata`.
- **FR-1.2** A record is a **1:1** failure↔solution pair. Multiple solutions to the *same* failure are stored as **separate records**, created at different times. (No parent/child link is required in v1.)
- **FR-1.3** Metadata shall include, at minimum: `category`, `product`, `article_number`, `ncr`, `bug_record_number`.
- **FR-1.4** The taxonomy **category → product → article_number** shall be defined in an **external configuration file** (`config/taxonomy.yaml`), not hard-coded, so the vocabulary can change without code changes.
- **FR-1.5** The WebUI shall provide a **form** to create a record with a **dependent cascade**: `category` (required) → `product` (required, populated by category) → `article_number` (optional, populated by product, default none), all sourced from configuration.
- **FR-1.6** The WebUI shall allow **listing** records with filtering by `category`, `product`, `article_number`, status, and free-text query, plus **editing** and **archiving/restoring** a record (soft delete — see FR-1.11).
- **FR-1.7** The system shall compute a **content hash** (MD5) of the normalized `failure_description` + `solution_description` (failure+solution only) and **block duplicate** submissions by default (`409` + existing id). See `F&S_REQUIREMENTS.md` §5–6.
- **FR-1.8** Records may be submitted **manually** (WebUI) or **automatically** (REST API, e.g., a script reading an external DB); **both paths run the identical pipeline** (validate → dedup → embed → insert).
- **FR-1.9** On create/update, the system shall compute and store the **embedding** of the `failure_description` (768 dimensions), together with embedding provenance (`embed_model`, `embed_dim`).
- **FR-1.10** The backend API shall support **bulk** create/upsert of records (used by the external script), per-item transactional.
- **FR-1.11** Deletion shall be **soft only**: a record is archived (`status='archived'`) or restored (`status='active'`); the system shall never hard-delete a record row.

### 3.2 Agentic chat (Chat stage)

- **FR-2.1** The system shall accept a free-text user message (a question or a failure description) and run an **agentic loop** that retrieves, infers, and concludes a solution.
- **FR-2.2** On each turn the agent shall **decide** whether to search the **records (F&S) data**, the **RAG documentation data**, ask the user a **clarifying question**, or produce a **final answer**.
- **FR-2.3** The agent shall support **back-and-forth** conversation: it may ask clarifying questions and incorporate the user's follow-up before concluding.
- **FR-2.4** The agent's decision shall be produced as a **structured output** (a typed `AgentAction`), not free text, to keep the loop deterministic.
- **FR-2.5** The agent loop shall be bounded by a **maximum turn budget** (default **5**, configurable). When the budget is exhausted, the agent shall produce a best-effort final answer from the context gathered.
- **FR-2.6** The agent shall present the **sources** it relied on (record ids / RAG chunks with their relevance scores) alongside the answer.
- **FR-2.7** The agent's **response language** shall match the user's selected UI language (Swedish or English).
- **FR-2.8** Conversation history is held **in memory only** (per session); it is **not** persisted to the database. A "clear conversation" action shall be available.

### 3.3 Retrieval (hybrid search)

- **FR-3.1** Retrieval precision shall combine **semantic** search (vector embeddings, cosine distance) and **lexical** search ("bag of words" / keyword matching) over the same corpus.
- **FR-3.2** The two ranked result sets shall be fused with **Reciprocal Rank Fusion (RRF)** and truncated to a configurable **top-k**.
- **FR-3.3** Retrieval shall be available against **both** the records table and the RAG chunks table, which live in **separate tables of the same Postgres database** (`fiffia_fs`), independent of each other.
- **FR-3.4** Lexical search shall be **language-robust** (content is mixed Swedish/English and contains technical tokens such as part numbers and error codes).
- **FR-3.5** Retrieval shall **first** apply a structured query on `category` and `product` (and optionally `article_number`) to scope the candidate set, **then** rank semantically/lexically within it.
- **FR-3.6** The F&S database shall be **universal**: the same `records` table serves agent retrieval **and** external statistics (counts by category/product/article_number, over time, unique-failure counts) via a read-only role. See `F&S_REQUIREMENTS.md`.

### 3.4 RAG documentation corpus

- **FR-4.1** The system shall maintain a **separate table** (`rag_chunks`) for the RAG documentation corpus, distinct from the `records` table, in the same Postgres database.
- **FR-4.2** The RAG corpus consists of **chunks of technical documentation and instructions**.
- **FR-4.3** A **simple ingestion path** (library function + CLI script + API trigger) shall read raw documents, split them into chunks, compute 768-dim embeddings, and store them (idempotently) in the RAG table.
- **FR-4.4** Each RAG chunk shall retain provenance (source file and section header) for citation.

### 3.5 Backend API

- **FR-5.1** The system shall expose a **REST/JSON API** for: create, read, update, archive/restore, list (with filters), bulk upsert, hybrid search, and RAG ingestion — usable independently of the WebUI.
- **FR-5.2** The API shall operate on **both** the records table and the RAG chunks table.
- **FR-5.3** The API shall be consumable by a **script** (HTTP client). No authentication is required in v1 (single trusted user, internal network).

### 3.6 Configuration

- **FR-6.1** `category`, `product`, and `article_number` vocabularies/rules shall be loaded from **external configuration files** at startup.
- **FR-6.2** Configuration changes to the vocabulary shall not require code changes.

### 3.7 Internationalization

- **FR-7.1** The UI shall support **Swedish (default)** and **English**, selectable by the user.
- **FR-7.2** The language selection shall drive both **UI labels** and the **agent's response language**.

### 3.8 LLM / embedding provider

- **FR-8.1** The system shall use a (local or remote) **Ollama server** for both the LLM and embeddings, called over its HTTP API.
- **FR-8.2** Embedding dimension is fixed at **768**.
- **FR-8.3** The LLM shall be a **21B–35B** class model. An **instruct** model is the default; the architecture shall allow swapping in a different (e.g. reasoning) model without structural change.
- **FR-8.4** The LLM/embedding access shall be **abstracted behind a provider interface** so that an **OpenAI-compatible (cloud) endpoint** can be used in the future without changing the rest of the system.

---

## 4. Non-Functional Requirements

- **NFR-1 (Scale)** — Designed for **tens of thousands** of records and a comparable RAG corpus. Indexing and retrieval must remain fast at this scale; statistics queries must stay fast up to ~1 M rows.
- **NFR-2 (Concurrency)** — A **small team** of concurrent users. The Ollama server processes **one request at a time**; the application shall **serialize all LLM/embedding calls** (single shared lock) and surface a **"busy / thinking" indicator** rather than failing or dropping requests.
- **NFR-3 (Latency)** — No streaming. Each chat turn is a blocking request; the UI must show a clear in-progress indicator for the duration.
- **NFR-4 (Reliability)** — Durable storage in PostgreSQL. Embedding computation failures on ingest shall not corrupt already-stored records (per-record transactional safety).
- **NFR-5 (Portability)** — Deployable via **Docker Compose** (app + Postgres + Ollama) and runnable directly with a venv + local Postgres. No hard dependency on a specific host.
- **NFR-6 (Maintainability)** — Python 3.12, typed, modular. Clear separation: WebUI / API / agent / retrieval / provider / db.
- **NFR-7 (Security)** — No auth in v1, but the app shall bind sensibly and treat all inputs as untrusted (parameterized queries only, no SQL string concatenation). External statistics access uses a dedicated read-only role. Document the exposure risk if run beyond a trusted network.
- **NFR-8 (Observability)** — Structured logging of agent decisions (action taken, queries issued, sources selected, turn count) for debugging retrieval quality.

---

## 5. Constraints & Locked Decisions

| # | Decision | Value |
|---|---|---|
| C-1 | Record shape | 1:1 failure:solution; multiple solutions = separate records |
| C-2 | Metadata core | `category`, `product`, `article_number` (+ `ncr`, `bug_record_number`) |
| C-3 | Vocabulary source | Nested taxonomy (category → product → article_number) in external config (`config/taxonomy.yaml`), not code |
| C-4 | Content language | Mixed Swedish/English |
| C-5 | Data topology | **One Postgres database `fiffia_fs`**, separate tables: `records` (F&S), `records_audit`, `rag_chunks` (RAG) |
| C-6 | Scale | Tens of thousands of records; small team |
| C-7 | Ollama | Local/remote server; **one request at a time** (serialize via shared lock) |
| C-8 | Future LLM path | OpenAI-compatible (cloud) via provider abstraction |
| C-9 | Embedding dim | **768** |
| C-10 | LLM size | 21B–35B; instruct default |
| C-11 | Agent decision | Structured output (`AgentAction`) |
| C-12 | Reranking | **None** in v1 — RRF + top-k only |
| C-13 | Turn budget | Default **5** |
| C-14 | Streaming | **None**; require busy/thinking indicator |
| C-15 | Ingest UX | Form first; bulk via backend API |
| C-16 | Response language | Matches selected UI language |
| C-17 | Users | Single user, read-write, no auth |
| C-18 | API consumer | Script |
| C-19 | Chat history | In-memory only |
| C-20 | Deployment | Docker Compose provided; plain venv also supported |
| C-21 | WebUI stack | **FastAPI + Jinja2 templates + HTMX** (server-rendered, no SPA) |
| C-22 | Deletion | **Soft delete only** (`status` active/archived); no hard DELETE; every change written to `records_audit` |
| C-23 | Dedup hash | MD5 of normalized failure+solution **only**; **block** default (unique index → 409); `warn` UX via pre-check endpoint |
| C-24 | Stats delivery | External backend **hits the DB directly** via read-only role `fs_stats_reader` |

---

## 6. Out of Scope (v1)

- Cross-encoder **reranking** (deferred; RRF + top-k is sufficient now).
- **Authentication / multi-user roles / RBAC.**
- **Streaming** token output.
- **Parent/child** hierarchical RAG chunking (flat chunks with provenance are sufficient).
- **Automatic** RAG re-ingestion / watch mode (manual script/API run is sufficient).
- **Persistence** of chat history.
- **GPU-specific** tuning (compose supports optional GPU passthrough but it is not required).
- `solution_embedding` (second vector column) — search targets the failure description; trivial to add later.

---

## 7. High-Level Acceptance Criteria

1. A technician can create a record via the form; it is stored with a 768-dim embedding and is immediately retrievable.
2. Given a failure description, the agent retrieves relevant records **and/or** RAG chunks, may ask a clarifying question, and returns a solution in the selected language with cited sources.
3. The agent respects the turn budget and always terminates with an answer.
4. The external script can bulk-import records and run a hybrid search purely via the REST API.
5. Changing the `product` vocabulary in the config file (no code change) is reflected in the form and validation.
6. The whole system runs from `docker compose up` (or a documented venv path) with an Ollama server.
7. With Ollama busy, a second request is queued and the UI shows a "thinking" indicator rather than erroring.
8. An external backend can run the `F&S_REQUIREMENTS.md` §8 statistics queries read-only as `fs_stats_reader`, and writes are denied.