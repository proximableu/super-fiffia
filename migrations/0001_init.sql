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
