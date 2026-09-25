-- Read-only role for the external statistics backend (after {{ }} substitution).
--
-- The external statistics backend (consumer B in F&S_REQUIREMENTS.md §8 /
-- CONTRACT.md §5) connects to ``fiffia_fs`` directly and runs GROUP BY queries.
-- It must never write or read the audit / RAG tables, so it runs as this
-- dedicated read-only role. Idempotent: the migration runner re-runs every
-- migration, so this must be safe to apply repeatedly.

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
