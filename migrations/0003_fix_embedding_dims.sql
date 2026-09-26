-- Fix embedding dimension from 768 to 1024 to match the configured
-- snowflake-arctic-embed2:568m model (1024-dim embeddings).
--
-- Both HNSW indexes were built for vector(768); pgvector cannot resize a column
-- out from under those indexes, so both indexes are dropped and rebuilt around
-- the ALTER. Rows whose embedding is not exactly 1024 wide are handled per-table:
-- RAG chunks are deleted (the original text is not recoverable from the chunk),
-- the RAG ingestion path re-seeds them idempotently from the source documents;
-- records, however, are re-embeddable from the row text (the failure description
-- is right there), so their stale vectors are nulled rather than lost — a later
-- ingest re-computes them.
--
-- run_migrations() wraps each file in its own transaction, so this file contains
-- only DDL — no BEGIN/COMMIT/ROLLBACK.

DELETE FROM rag_chunks WHERE array_length(embedding::real[], 1) != 1024;

-- Nuke stale (768-wide) record embeddings before the column is widened; they are
-- re-embeddable from the row text, so NULL (not DELETE) is the right reset.
UPDATE records SET embedding = NULL
    WHERE embedding IS NOT NULL AND array_length(embedding::real[], 1) <> 1024;

DROP INDEX IF EXISTS ix_records_embedding_hnsw;
DROP INDEX IF EXISTS ix_rag_hnsw;

ALTER TABLE records ALTER COLUMN embedding TYPE vector(1024);
ALTER TABLE rag_chunks ALTER COLUMN embedding TYPE vector(1024);

CREATE INDEX IF NOT EXISTS ix_records_embedding_hnsw
    ON records USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_rag_hnsw
    ON rag_chunks USING hnsw (embedding vector_cosine_ops);
