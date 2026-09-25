# WEBUI — User Interaction Specification

> Describes **how the user interacts** with the two WebUI stages: **Submit** (ingest a record) and **Troubleshooting** (chat with the agent), plus the record list / edit / archive views.
> Stack: **FastAPI + Jinja2 templates + HTMX** (server-rendered, no SPA). Language: **Swedish (default) / English**.
> Companion docs: `REQUIREMENTS.md`, `SPECIFICATIONS.md`, `CONTRACT.md`, `F&S_REQUIREMENTS.md`.

---

## 1. Principles

- **Two stages**, one page each: `/ingest` (Submit + record list) and `/chat` (Troubleshooting). A top nav switches between them.
- **Shared cascade** — both stages use the same `category → product → article_number` selector, driven entirely by an external config file (no code change to add values).
- **Three indicator states** everywhere a server call is made: **progress** (busy), **acknowledge** (success), **error**.
- **Blocking requests, no streaming.** While the server works (embed+insert, or an agent turn), a progress indicator is shown; then the result swaps in.
- **Defaults:** every field starts at `——` / empty.
- **No hard deletes** — the list offers Archive/Restore; archived records stay queryable (`status=archived`).

---

## 2. Shared component — the taxonomy cascade

A three-level dependent dropdown, identical in both stages.

```
category  ──►  product  ──►  article_number
(required)      (required)     (optional, default "——")
```

- **`category`** — `<select>`, required. Options from config. Default `——`.
- **`product`** — `<select>`, required. **Re-populated** when `category` changes (only the products belonging to that category). Default `——`.
- **`article_number`** — `<select>`, optional. **Re-populated** when `product` changes (only the article numbers belonging to that product). Default `——` (= none / empty).

**Behaviour (HTMX):**
- Changing `category` → `hx-get /api/taxonomy/products?category=<id>` → swap the `product` `<select>` options (reset to `——`).
- Changing `product` → `hx-get /api/taxonomy/articles?category=<id>&product=<id>` → swap the `article_number` `<select>` options (reset to `——`).
- Changing `category` also resets `product` **and** `article_number`.

**Source:** `config/taxonomy.yaml` (nested: category → products → article_numbers). See `CONTRACT.md` §4.

> Note: `article_number` is an **enumerator** — a member of the selected product's list — not free text.

---

## 3. Stage A — Submit Form (Ingest)

### 3.1 Fields

| Field | Control | Required | Default | Notes |
|---|---|---|---|---|
| `category` | select (cascade) | **Yes** | `——` | Primary. |
| `product` | select (cascade) | **Yes** | `——` | Populated by category. |
| `failure_description` | textarea | **Yes** | empty | The searchable failure. |
| `solution_description` | textarea | **Yes** | empty | |
| `article_number` | select (cascade) | No | `——` (none) | Optional group; populated by product. |
| `ncr` | text | No | empty | Optional group. |
| `bug_record_number` | text | No | empty | Optional group. |

**Buttons:** `Submit` · `Clear/Cancel`.
**Indicators:** progress spinner · success acknowledge · error message.

**Layout:** the four required fields are the primary block. `article_number`, `ncr`, `bug_record_number` live in a collapsible **"Valfrihetsfält / Optional fields"** group (de-emphasized, can be hidden) — `article_number` is still a dropdown per §2.

### 3.2 Interaction flow

```
[load]  all fields = —— / empty
   │
   ▼
pick category ──► product options populate (reset to ——)
   │
   ▼
pick product  ──► article_number options populate (default ——)
   │
   ▼
fill failure_description + solution_description
   │
   ▼
[Submit]
   ├─ client validation: category, product, failure, solution present?
   │      └─ no  → error indicator, name the missing field(s), stay on form
   ├─ yes → progress indicator ON (blocking: embed + insert)
   │      ├─ server 201 → success acknowledge ("Sparad ✓ / Saved ✓")
   │      │                → reset form to defaults (or show the created record)
   │      ├─ server 409 → duplicate indicator: "Finns redan (id …, skapad …) / Already exists"
   │      └─ server 4xx → error indicator with the server message
   └─ [Clear/Cancel] → reset all fields to —— / empty, clear all indicators
```

### 3.3 Validation rules

- **Required:** `category`, `product`, `failure_description`, `solution_description`.
- **Optional:** `article_number` (default none), `ncr`, `bug_record_number`.
- **Server re-validates** against the taxonomy: `product` must belong to the chosen `category`; `article_number` (if set) must belong to the chosen `product`. Violation → `422` with a clear message.
- **Duplicate → `409`** — the server message carries the existing record's `id` + `created_at` (dedup is `block` by default; see `F&S_REQUIREMENTS.md` §6).

### 3.4 Indicator catalogue (Submit)

| State | Trigger | SV text | EN text |
|---|---|---|---|
| idle | default | — | — |
| progress | Submit clicked, awaiting server | "Sparar…" | "Saving…" |
| success | `201` | "Sparad ✓" | "Saved ✓" |
| duplicate | `409` | "Finns redan (id {id}, skapad {created_at})" | "Already exists (id {id}, created {created_at})" |
| error (client) | missing required field | "Vänligen fyll i: {fält}" | "Please fill in: {field}" |
| error (server) | other `4xx` | server message | server message |

### 3.5 Record list, edit & archive

The submit page also hosts the record list (FR-1.6):

- **List partial** (`GET /ingest/list`): filterable by `category`, `product`, `article_number`, `status` (`active`/`archived`/all), and free-text `q`. Rendered as record cards (category, product, article_number, failure excerpt, created_at).
- **Edit** (`GET /ingest/{id}/edit` → `POST`): same form, pre-filled; save calls `PUT /api/records/{id}` semantics; validation identical to §3.3; a text change re-embeds automatically.
- **Archive / Restore** (`POST /ingest/{id}/archive` | `/restore`): soft delete button with a confirm step; archived records disappear from the default list (`status=active`) but remain via the status filter.
- **Free-text `q`** reorders the list via hybrid search (`GET /api/records?q=…`).

---

## 4. Stage B — Troubleshooting (Chat)

### 4.1 Layout

```
┌──────────────────────────────────────────────────────────────┐
│  Kontext (scoping)                                            │
│  category [——▾]   product [——▾]   article_number [——▾]       │
│  failure description: [____________________________]          │
├──────────────────────────────────────────────────────────────┤
│  Samtal (conversation)                                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ assistant: …answer + cited sources…                    │  │
│  │ user:        …follow-up…                               │  │
│  └────────────────────────────────────────────────────────┘  │
│  [ user message input __________________ ]  [ Skicka / Send ] │
│  (busy indicator: "Agenten tänker… / Agent is thinking…")     │
└──────────────────────────────────────────────────────────────┘
```

- **Context header** — the same `category → product → article_number` cascade (§2) plus a `failure_description` field. These **scope** the retrieval.
- **Chat subform** — a live back-and-forth: a conversation area (assistant answers + user messages) and a `user message` input with a **Send** button.

> **Chat subform feasibility:** straightforward with HTMX (each Send is a blocking POST that swaps in the new assistant turn). It is the **preferred** mode. The fallback (a single "assistant answer" box + "user message" + Send, no scrolling history) is also supported and is what you get if the conversation area is disabled.

### 4.2 Interaction flow

```
[load]  context = —— / empty, conversation empty
   │
   ▼
select category → product → (article_number)   [scopes retrieval]
   │
   ▼
type failure_description (this is the first query)
   │
   ▼
[Send]  ──► progress indicator ON (blocking agent turn)
   │        1. structured query: WHERE category=? AND product=? [AND article_number=?]
   │        2. semantic + lexical RRF ranking within that scoped set
   │        3. agent loop: may query records (scoped) and/or RAG (semantic) as it decides
   │        4. assistant answer + cited sources swap into the conversation
   │
   ▼
user may continue (clarification / follow-up)  ──► repeat [Send]
   │        (history kept in memory; "Rensa / Clear" resets it)
   └─ agent may ask a clarifying question instead of answering
```

### 4.3 Retrieval scoping (the key rule)

Retrieval is **structured-first, then semantic**:

1. **Structured (first):** a plain indexed query restricts the candidate set to the selected `category` **and** `product` (and `article_number` if set). This is the primary filter.
2. **Semantic + lexical:** within that scoped set, rank by hybrid RRF (embedding cosine + `tsvector`) → top-k.
3. **Agent discretion:** the agentic loop may then query the **records** table (still scoped) and/or the **RAG** documentation table (semantic, not scoped by category/product) as it judges best, within the turn budget.

**Empty scoped set:** if the structured query returns nothing, the agent says so and either asks a clarifying question or falls back to a semantic/RAG search — it does **not** hallucinate a solution.

### 4.4 Indicator catalogue (Troubleshooting)

| State | Trigger | SV text | EN text |
|---|---|---|---|
| idle | default | — | — |
| progress | Send clicked, agent running | "Agenten tänker…" | "Agent is thinking…" |
| success | answer rendered | (answer + sources shown) | (answer + sources shown) |
| error | provider/DB failure | "Något gick fel. Försök igen." | "Something went wrong. Please retry." |

---

## 5. Language & i18n

- A **language selector** (SV / EN) sets the session language.
- Drives: all UI labels, the cascade option labels (`label_sv`/`label_en`), indicator texts, and the **agent's response language**.
- Default: **Swedish**.

---

## 6. HTMX wiring summary

| Action | Endpoint | HTMX |
|---|---|---|
| Load product options | `GET /api/taxonomy/products?category=` | `hx-get` on category change → swap product `<select>` |
| Load article options | `GET /api/taxonomy/articles?category=&product=` | `hx-get` on product change → swap article `<select>` |
| Submit record | `POST /api/records` | `hx-post`, `hx-indicator="#submit-busy"`, swap result/ack |
| Clear form | client-side reset | `hx-get /ingest` (re-render defaults) |
| Refresh record list | `GET /ingest/list` | `hx-get` with filter params → swap list partial |
| Edit record | `GET/POST /ingest/{id}/edit` | navigation / `hx-post` → swap form or list |
| Archive/Restore | `POST /ingest/{id}/archive` \| `/restore` | `hx-post` (confirm step) → swap list |
| Send chat turn | `POST /chat` | `hx-post`, `hx-indicator="#chat-busy"`, swap conversation |
| Clear conversation | `POST /chat/clear` | `hx-post`, swap conversation |
| Set language | `POST /lang` | `hx-post`, full re-render |

---

## 7. Edge cases

| Case | Behaviour |
|---|---|
| Required field empty on Submit | Client blocks; error indicator names the field. |
| `product` not in chosen `category` (e.g. stale state) | Server `422`; error indicator. |
| `article_number` not in chosen `product` | Server `422`; error indicator. |
| Duplicate failure+solution on Submit | Server `409`; duplicate indicator with existing id/created_at. |
| Scoped record set empty (Troubleshooting) | Agent states no exact match; asks clarification or searches RAG. |
| Ollama busy / slow | Progress indicator stays up (calls are serialized); no dropped request. |
| Agent budget exhausted | Best-effort final answer from gathered context + sources. |
| No sources at all | Agent says it found no matching record/doc; suggests refining category/product or the description. |
| Restoring an archived record whose twin is now active | Server `409`; message explains the conflict. |

---

## 8. Assumptions (confirmed)

1. **Troubleshooting context is required** — `category` and `product` must be selected to scope retrieval (same as Submit). `article_number` optional. ✓
2. **`article_number` is an enumerator** (member of the product's list), **different sets per product** — in both the WebUI dropdown and API validation. ✓
3. **Taxonomy lives in one nested file** `config/taxonomy.yaml` (category → products → article_numbers). ✓
4. **Optional fields** (`article_number`, `ncr`, `bug_record_number`) are shown in a collapsible group, not literally hidden. ✓
5. **Two submission paths, one pipeline** — records can be submitted **manually** (this form) or **automatically** via `POST /api/records` (e.g., a script reading an external DB). Both run the **same** pipeline: validate → dedup (MD5, block) → embed → insert. API submits may carry only `article_number` (the external script resolves it to `category`+`product` before posting). ✓
6. **Deletion is archiving** — the list offers Archive/Restore (soft delete); no record row is ever hard-deleted. ✓