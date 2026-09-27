-- 0004_rag_chunk_scope.sql
-- Add optional category/product columns to rag_chunks so RAG retrieval can be
-- scoped by the chat question's taxonomy, mirroring the records leg. Existing
-- rows keep nullable columns; the empty-scope path (both NULL) matches everything.
-- Idempotent: safe to re-run.

ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS product TEXT;

CREATE INDEX IF NOT EXISTS ix_rag_scope
    ON rag_chunks (category, product)
    WHERE category IS NOT NULL AND product IS NOT NULL;
