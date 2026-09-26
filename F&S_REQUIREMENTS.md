# F&S Database — Requirements & Design

> The **"Failures & Solutions" (F&S)** database. It serves **two consumers from one table**:
> - **A. Agent retrieval** — scoped semantic + lexical search for the troubleshooting agent.
> - **B. External statistics backend** — aggregate / analytics queries over a **read-only** connection.
>
> Companion docs: `REQUIREMENTS.md`, `SPECIFICATIONS.md`, `CONTRACT.md`, `WEBUI.md`.

---

## 1. Purpose

Store failure/solution knowledge — **deduplicated** — and expose it for retrieval (A) and statistics (B) **without maintaining separate copies**. The same rows, the same indexes, two query patterns.

---

## 2. The "universal" goal

One table `records` in database `fiffia_fs`, used by both consumers:

| Consumer | Query pattern | Relies on |
|---|---|---|
| **A. Agent retrieval** | scoped `WHERE` + HNSW (embedding) + GIN (fts) → RRF top-k | `embedding`, `fts`, btree taxonomy columns |
| **B. Statistics backend** | `GROUP BY` / `COUNT` / time-series | denormalized taxonomy columns, `created_at`, `content_hash`, `status` |

**Core design principle — denormalize the taxonomy.** `category`, `product`, `article_number` are stored as plain columns (not joined to a taxonomy table). The *vocabulary* still lives in `config/taxonomy.yaml` (source of truth for the form + validation), but the *chosen values* are columns in the row. This makes both **scoping** (retrieval) and **aggregation** (statistics) index-only, with no joins.

---

## 3. Functional requirements

- **FR-FS-1** Store per record: `category`, `product`, `article_number`, `failure_description`, `solution_description`, `ncr`, `bug_record_number`.
- **FR-FS-2** Compute a **`content_hash`** = MD5 of the normalized `failure_description` + `solution_description` (**failure+solution only**, regardless of category/product); use it to **prevent duplicates** (fast, indexed).
- **FR-FS-3** Dedup mode **`block`** is the **default** — a duplicate insert is rejected with `409` + the existing id (there is no such thing as the same solution to the same failure under a different product). The **`warn`** UX is a pre-submission convenience via the check-duplicate endpoint — the unique index never weakens (see §6).
- **FR-FS-4** Support **scoped retrieval**: `WHERE category=? AND product=? [AND article_number=?] AND status='active'`, then semantic + lexical RRF ranking within that set.
- **FR-FS-5** Support **statistics** for an external backend: counts by `category`/`product`/`article_number`, counts over time, and **unique-failure** counts (dedup-aware).
- **FR-FS-6** **Soft delete** via `status` (`active`/`archived`) so historical statistics are preserved.
- **FR-FS-7** Store **embedding provenance** (`embed_model`, `embed_dim`) to enable safe re-embedding when the model changes.
- **FR-FS-8** **Audit**: `created_at` (immutable), `updated_at`, `created_by`, `source` (`manual`/`import`/`api`).

---

## 4. Non-functional requirements

- **NFR-FS-1** Dedup lookup is **O(log n)** (B-tree index on `content_hash`).
- **NFR-FS-2** Scoped retrieval p95 **< 300 ms** (excluding LLM time).
- **NFR-FS-3** Statistics `GROUP BY` over ≤ 1 M rows **< 200 ms** (covered by btree indexes).
- **NFR-FS-4** External statistics access is **read-only** (dedicated Postgres role).
- **NFR-FS-5** Scale path: **monthly partitioning** by `created_at` when the table exceeds ~5 M rows.
- **NFR-FS-6** Backups: standard Postgres PITR; the F&S DB is the system of record.

---

## 5. Schema (best practices)

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;          -- gen_random_uuid()

CREATE TABLE records (
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
CREATE UNIQUE INDEX uq_records_content_hash ON records (content_hash) WHERE status = 'active';
-- (for `warn` mode you may instead use a plain non-unique index for pre-checks)

-- scoping + statistics
CREATE INDEX ix_records_category         ON records (category);
CREATE INDEX ix_records_category_product ON records (category, product);
CREATE INDEX ix_records_product          ON records (product);
CREATE INDEX ix_records_article          ON records (article_number);
CREATE INDEX ix_records_status           ON records (status);
CREATE INDEX ix_records_created_at       ON records (created_at);

-- retrieval
CREATE INDEX ix_records_embedding_hnsw   ON records USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ix_records_fts              ON records USING gin (fts);

-- keep fts in sync
CREATE FUNCTION records_fts_trigger() RETURNS trigger AS $$
BEGIN
  NEW.fts := setweight(to_tsvector('simple', coalesce(NEW.failure_description,'')), 'A')
          || setweight(to_tsvector('simple', coalesce(NEW.solution_description,'')), 'B');
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_records_fts BEFORE INSERT OR UPDATE ON records
  FOR EACH ROW EXECUTE FUNCTION records_fts_trigger();

-- keep updated_at in sync
CREATE FUNCTION records_touch() RETURNS trigger AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_records_touch BEFORE UPDATE ON records
  FOR EACH ROW EXECUTE FUNCTION records_touch();

-- append-only audit log (helps troubleshoot code / trace who changed what, when)
CREATE TABLE records_audit (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id     UUID NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    action        TEXT NOT NULL,              -- create | update | archive | restore
    changed       JSONB,                      -- {field: {old, new}}
    actor         TEXT,                       -- created_by / api client
    at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_records_audit_record ON records_audit (record_id, at DESC);
```

### 5.1 `content_hash` definition

Computed **in the app layer** (Python), stored as a 32-char hex string:

```python
import re, hashlib
def content_hash(failure: str, solution: str) -> str:
    def norm(s: str) -> str:
        s = s.strip().lower()
        s = re.sub(r"\s+", " ", s)          # collapse whitespace
        return s
    payload = norm(failure) + "\u0000" + norm(solution)   # NUL separator avoids (a+b,c)/(a,b+c) collisions
    return hashlib.md5(payload.encode("utf-8")).hexdigest()
```

- **Normalization** (trim, lowercase, collapse whitespace) makes trivially-different text still dedupe.
- **MD5** is the algorithm (per decision). Collision risk is negligible for this use (dedup, not security).
- **Scope (locked):** the hash is over `failure_description` + `solution_description` **only** — the same failure+solution is a duplicate **even across different products/categories**.

### 5.2 Indexes — rationale

| Index | Serves |
|---|---|
| `uq_records_content_hash` (unique) | dedup authority — `block` default (FR-FS-2/3) |
| `ix_records_category_product` | retrieval scoping + `GROUP BY category, product` stats |
| `ix_records_category` / `_product` / `_article` | single-dimension stats + scoping |
| `ix_records_status` | active-only filtering in both paths |
| `ix_records_created_at` | time-series statistics |
| `ix_records_embedding_hnsw` | semantic retrieval |
| `ix_records_fts` | lexical retrieval |

---

## 6. Dedup design

**`block` mode (default):**
- The **unique partial index** `uq_records_content_hash` is the authority.
- On Submit the app computes `content_hash` and inserts; a duplicate raises a unique violation → app returns **`409`** with the existing `id` + `created_at`.
- Race-safe by construction (no check-then-insert window).

**`warn` UX (via the pre-check endpoint — the unique index never weakens):**

```
pre-check  → POST /api/records/check-duplicate {failure_description, solution_description}
             (app computes content_hash, SELECT id, created_at FROM records
              WHERE content_hash=? AND status='active' LIMIT 1)
              ├─ found  → UI: "Already exists (id …, created …)" → [Skip] [View] [Create anyway]
              │           ("Create anyway" posts normally; the unique index is still the
              │            authority — if a race slips in, the insert fails with 409)
              └─ none   → proceed to embed + insert
```

The **block** default is always in force: the unique partial index `uq_records_content_hash` is the authority for every insert path. `warn` is purely a **pre-submission convenience** served by the check-duplicate endpoint — there is no separate index mode to toggle.

**UI integration:** see `WEBUI.md` §3 (Submit) — the duplicate indicator is a distinct state alongside progress / success / error.

---

## 7. Agent retrieval interface (consumer A)

```
1. scope   : WHERE category=? AND product=? [AND article_number=?] AND status='active'
2. semantic: HNSW cosine on embedding  → top POOL
3. lexical : GIN fts (ts_rank)         → top POOL
4. fuse    : Reciprocal Rank Fusion → top TOP_K
5. return  : id, category, product, article_number,
             failure_description, solution_description,
             ncr, bug_record_number, score, created_at
```

- The **scoped set** is the hard filter (structured-first, per `WEBUI.md` §4.3).
- If the scoped set is empty → the agent says so and may broaden (clarify / RAG).

---

## 8. Statistics interface (consumer B — external backend)

**Read-only role** (security best practice — the external backend never writes; idempotent, name/password injected from `settings.stats` by the migration runner):

```sql
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs_stats_reader') THEN
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', 'fs_stats_reader', 'change_me');
  END IF;
END $$;
DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), 'fs_stats_reader');
END $$;
GRANT USAGE ON SCHEMA public TO fs_stats_reader;
GRANT SELECT ON records TO fs_stats_reader;
-- No other grants: the role cannot read records_audit / rag_chunks, and cannot write.
```

**Representative queries** (all index-friendly):
```sql
-- counts by category / product
SELECT category, product, COUNT(*) AS n
FROM records WHERE status='active'
GROUP BY category, product ORDER BY n DESC;

-- volume over time (monthly)
SELECT date_trunc('month', created_at) AS m, COUNT(*) AS n
FROM records WHERE status='active'
GROUP BY m ORDER BY m;

-- unique failures (dedup-aware)
SELECT COUNT(DISTINCT content_hash) AS unique_failures
FROM records WHERE status='active';

-- most common article numbers
SELECT article_number, COUNT(*) AS n
FROM records WHERE status='active' AND article_number IS NOT NULL
GROUP BY article_number ORDER BY n DESC LIMIT 20;
```

**Delivery (locked):** the external backend **hits the DB directly** using the `fs_stats_reader` read-only role above. (A `/api/stats/*` wrapper on the app is possible later but not required.)

---

## 9. Data lifecycle

```
create (dedup check → embed → insert)
   │
   ├─ update  (re-embed if failure_description changes; updated_at auto)
   ├─ archive (status='archived'; excluded from retrieval + active stats; kept for history)
   └─ re-embed (maintenance script when embed_model changes: rows where
                embed_model != current → re-embed; not part of the v1 API tasks)
```
Restore (`status` back to 'active') must re-check the unique `content_hash` index — if another active record now holds the same hash, the restore is rejected with `409`.

---

## 10. Best-practices checklist

- [x] Denormalized taxonomy columns (fast scoping **and** aggregation, no joins).
- [x] `content_hash` for O(log n) dedup + unique-count statistics.
- [x] Soft delete (`status`) — preserves statistical history.
- [x] Immutable `created_at` + auto `updated_at` + `created_by` + `source` (audit).
- [x] Append-only `records_audit` log (who changed what, when) for code troubleshooting.
- [x] Embedding provenance (`embed_model`, `embed_dim`) for safe model upgrades.
- [x] Read-only role for the external statistics backend.
- [x] Indexes covering both retrieval and statistics paths.
- [x] Scale path (partitioning) + PITR backups documented.

---

## 11. Decisions (locked)

1. **Hash scope** — failure+solution **only**; a duplicate is a duplicate even across different products/categories.
2. **Dedup default** — **`block`** (unique index; `409` on duplicate). `warn` available as an option.
3. **Hash algorithm** — **MD5** (`char(32)`).
4. **Audit log** — **yes**, append-only `records_audit` table (helps troubleshoot code).
5. **Partitioning threshold** — ~5 M rows / monthly (scale path; not enabled by default).
6. **Stats delivery** — external backend **hits the DB directly** via the read-only role.
