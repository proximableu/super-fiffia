-- docker/init_db.sql
--
-- Bootstraps the single-node local stack. The official postgres image runs the
-- .sql files in /docker-entrypoint-initdb.d against the maintenance database on
-- first volume initialisation only, as the bootstrap superuser.
--
-- It creates the application database and the application role used by the app
-- and by the (idempotent) migration runner.
--
-- Note: CREATE DATABASE cannot run inside a transaction or DO block, so the
-- database creation below uses the psql \gexec meta-command; the role creation
-- is wrapped in a DO block (which permits IF NOT EXISTS).

-- The application database (idempotent via \gexec).
SELECT 'CREATE DATABASE fiffia_fs'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'fiffia_fs')
\gexec

-- The application role (login). SUPERUSER keeps this single-node local
-- deployment simple: it owns and manages the database, installs pgvector and
-- pgcrypto, and runs the migrations -- matching the role's standing on the
-- production database.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fiffia') THEN
        CREATE ROLE fiffia LOGIN PASSWORD 'fiffia' SUPERUSER;
    END IF;
END $$;

GRANT ALL PRIVILEGES ON DATABASE fiffia_fs TO fiffia;
