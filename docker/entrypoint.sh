#!/usr/bin/env bash
# Container entrypoint for the ``app`` service.
#
# Waits for Postgres to accept connections, applies the migrations
# (idempotent) and then serves uvicorn. FS_DSN is supplied by docker-compose.
set -euo pipefail

: "${FS_DSN:?FS_DSN must be set}"
echo "[entrypoint] connecting to ${FS_DSN}"

# Wait for Postgres to be ready (docker-compose also gates on its healthcheck;
# this is a second guard so the migration step never runs against a cold DB).
export PGCONNECT_TIMEOUT=5
for _ in $(seq 1 30); do
  if python - <<'PY' 2>/dev/null
import os
import psycopg

psycopg.connect(os.environ["FS_DSN"], connect_timeout=5).close()
PY
  then
    break
  fi
  echo "[entrypoint] Postgres not ready; retrying..."
  sleep 2
done

# Apply migrations. A failure here aborts the container: the schema must exist
# before the app serves requests.
echo "[entrypoint] applying migrations..."
python - <<'PY'
from app.db import run_migrations

print("migrations applied:", run_migrations())
PY

# Serve the webui app (app.webui:app) when WEBUI=1, otherwise the MCP server
# when MCP=1, otherwise the API app. All three share the entrypoint and the
# same migration step; only the uvicorn module and port differ.
if [ "${WEBUI:-0}" = "1" ]; then
  PORT="${WEBUI_PORT:-8001}"
  echo "[entrypoint] serving webui on 0.0.0.0:${PORT}"
  exec uvicorn app.webui:app --host 0.0.0.0 --port "${PORT}"
fi

if [ "${MCP:-0}" = "1" ]; then
  PORT="${MCP_PORT:-9002}"
  echo "[entrypoint] serving MCP on 0.0.0.0:${PORT}"
  exec uvicorn scripts.run_mcp:app --host 0.0.0.0 --port "${PORT}"
fi

echo "[entrypoint] starting uvicorn on 0.0.0.0:8000"
exec uvicorn app.api:app --host 0.0.0.0 --port 8000
