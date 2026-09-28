# Running the F&S Knowledge Base with Docker

A complete single-node stack runs via `docker compose` in the repo root:

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  app  :9000 │────▶│ postgres 5432 │◀────│  webui :9001 │
│ (FastAPI)   │     │ pgvector 17   │     │ (Jinja2 HTML)│
└──────┬──────┘     └──────────────┘     └──────────────┘
       │
       │  HTTP embed + LLM
       ▼
┌──────────────┐
│  ollama      │  models on :11434 (container) or host
└──────────────┘
```

The four services are defined in [`docker-compose.yml`](docker-compose.yml):
`postgres`, `ollama`, `app` (REST API, port 9000), and `webui` (server-rendered
Jinja2 HTML, port 9001). Two named volumes, `postgres_data` and `ollama_data`,
persist the database and models across `docker compose down`.

---

## 1. Prerequisites

- **Docker** with **Compose v2** installed (Docker Desktop, or Docker Engine +
  the `docker compose` plugin). Verify with:
  ```bash
  docker --version
  docker compose version
  ```
- **Ports** free on the host: `9000` (API), `9001` (WebUI), `5432` (Postgres),
  and `11434` (Ollama) **if you run Ollama as a container** — see §3.
- **~8 GB RAM** available to Docker for the two FastAPI services, Postgres, and
  a pulled Ollama model (LLM weights are pulled at first run; the embedding model
  is a few hundred MB).
- No local virtualenv is needed. The image installs `requirements.txt` itself.

## 2. Clone and configure

```bash
git clone <your repo> .cd into the repo
cp .env.example .env          # only needed when running directly against a
                             # local Postgres/Ollama (the venv path); see §5
```

`docker-compose.yml` injects every value the services need, so for the
container stack you normally **don't** need `.env` at all. You only touch it
when running the app *directly* in a local Python venv against a host Postgres
+ Ollama (§5).

## 3. Choose an Ollama backend (important)

Two options are supported. Pick **one** and set `FS_OLLAMA_URL` accordingly.

### Option A — Ollama as a container (recommended, self-contained)

With no override, `FS_OLLAMA_URL` defaults to `http://192.168.0.14:11434`,
which is a **host-specific IP that will not work on another machine**. Either
delete that override from `docker-compose.yml` so the app talks to the in-cluster
`ollama` service, or point it at your host's Ollama.

The in-cluster `ollama` service is simplest: it exposes Ollama on the compose
internal network, and the app reaches it by service name (`http://ollama:11434`).
To use it, ensure `FS_OLLAMA_URL` is **unset or** `http://ollama:11434`.

> Note: the `ollama` service mounts `./docker` as `/init:ro`. The
> `docker/models/` path in `.dockerignore` is intentionally excluded, so the
> model store is the named `ollama_data` volume — not a host directory.

### Option B — a remote Ollama server

You can point the stack at **any** Ollama server on the network — the host
machine or a separate box on your LAN. This is the common case; the compose
default `192.168.0.14:11434` is *supposed* to be a remote Ollama on your LAN,
just with a placeholder IP.

**On the remote server**, Ollama must be listening on an external interface —
a stock install only binds `localhost`, which the container can't reach.
Start it with:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

…or for a systemd-style install, edit the `ollama.service` unit to add
`Environment="OLLAMA_HOST=0.0.0.0"`, then `systemctl restart ollama` and open
firewall port `11434`. Confirm you can reach it from your dev machine before
touching Docker:

```bash
curl http://192.168.0.14:11434/api/tags    # substitute the server's IP
```

**From Docker**, set `FS_OLLAMA_URL` to the server's reachable address:

```bash
# Ollama on another machine on the same LAN (replace the IP)
FS_OLLAMA_URL=http://192.168.0.14:11434

# Ollama inside another Docker host, reached from your compose network
FS_OLLAMA_URL=http://172.17.0.1:11434

# host.docker.internal resolves to the machine running Docker (Docker Desktop)
FS_OLLAMA_URL=http://host.docker.internal:11434
```

Whichever backend you pick, make sure the **model is pulled** before the first
embed/chat call (§4).

## 4. Bring the stack up

```bash
# Build + start; compose waits for the postgres and ollama healthchecks first.
docker compose up --build

# In a second terminal, pre-pull the models so the first request doesn't block
# on a multi-gigabyte download.
docker compose run --rm ollama sh /init/ollama_init.sh
```

`docker compose up --build` blocks showing logs. Run it with `-d` to detach,
then follow logs with `docker compose logs -f`.

Services become healthy once:
- `postgres` → `pg_isready` succeeds,
- `ollama` → `ollama list` succeeds (no model required for readiness),
- `app` / `webui` → their `/health` route returns 200 **and** both Postgres
  and Ollama are reachable (compose starts them only after the other two are
  healthy).

Check status:

```bash
docker compose ps
curl -s localhost:9000/api/health
```

## 5. First-run smoke test

```bash
# API health (200 only when DB + Ollama are both reachable)
curl -s localhost:9000/api/health

# WebUI is at :9001 — open http://localhost:9001 in a browser
```

Expected: `app/` serves the REST API on `http://localhost:9000`, and
`webui/` serves the Jinja2 HTML interface on `http://localhost:9001`.

### Running directly in a venv (no Docker)

If you'd rather run without Docker, use the local venv. The app defaults to a
Postgres on `localhost:5999` and Ollama on `localhost:11434`; override with the
two env vars below (see `.env.example`).

```bash
python -m uvicorn app.api:app --host 0.0.0.0 --port 9000
# and, in another terminal, the WebUI:
WEBUI=1 WEBUI_PORT=9001 python -m uvicorn app.webui:app --host 0.0.0.0 --port 9001
```

## 6. Ingest documents (with the new tagging flags)

Once Ollama's embedding model is pulled, ingest markdown/text documents into
`rag_chunks`:

```bash
# Tag every ingested chunk with a taxonomy category + product so RAG retrieval
# (which now scopes to the product in chat) only pulls that product's docs.
python scripts/ingest_rag.py --source ./docs \
  --category sensor --product sensor_manual.md

# Preview the work without writing or embedding:
python scripts/ingest_rag.py --source ./docs --dry-run
```

- `--category` and `--product` are optional and default to `None`; omit them for
  an **unscoped ingest**. Untagged chunks are only reachable under an empty
  scope — see the migration notes below.
- Ingestion is idempotent by `(source_file, content_hash)`; re-running a
  directory reuses existing chunks rather than duplicating them.
- See `scripts/ingest_rag.py --help` for all flags (`--chunk-chars`, `--overlap`,
  `--dry-run`).

## 7. Notes on the data model

- **Postgres** uses the `pgvector` extension for the `embedding` column and a
  GIN `fts` index for lexical retrieval. The schema is created by
  `docker/init_db.sql` (database + role) and extended by the idempotent
  migrations in `migrations/` (auto-applied by `docker/entrypoint.sh` on first
  boot). `0004_rag_chunk_scope.sql` adds the optional `category`/`product`
  columns that the scope feature relies on.
- **Untagged chunks**: a chunk ingested with no `--category`/`--product` has
  NULL tags. Under a scoped query these are excluded by design; under an empty
  scope (no scope at all) they remain reachable. This preserves untagged data
  while still filtering unrelated RAG chunks out of a scoped chat.
- **Ollama models**: the embedding model (default `snowflake-arctic-embed2:568m`)
  and the LLM (default `gemma4:e4b`) are the values in `docker/ollama_init.sh`;
  override with `FS_EMBED_MODEL` / `FS_LLM_MODEL`.

## 8. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `app` health never turns green | The Ollama URL is unreachable from the container. Verify the backend in §3 and that the model is pulled. Check `docker compose logs app`. |
| `webui` on :9001 unreachable | The WebUI depends on Postgres + Ollama too; wait for the other services to be healthy, or `docker compose logs webui`. |
| Embedding errors, 404 on `/api/embed` | Ollama has no embedding model loaded, or the wrong model name is configured. Pull it (`docker compose run --rm ollama ollama pull <model>`) and confirm `embed_model` in `docker/ollama_init.sh`. |
| `psycopg` / `pgvector` errors | The migrations failed. `docker compose down -v` removes the named volumes; recreate with `docker compose up --build` (a fresh Postgres). |
| Can't reach remote/host Ollama | The server must listen on a reachable interface. On the remote machine start Ollama with `OLLAMA_HOST=0.0.0.0 ollama serve` and open firewall port 11434 (see §3). From a container, the host is `host.docker.internal:11434` (Docker Desktop) or the bridge gateway `172.17.0.1:11434` (Linux). Verify the server with `curl http://<server-ip>:11434/api/tags` from your dev machine first. |
| Postgres "database already exists" | `init_db.sql` is idempotent via `\gexec`; this should not happen on a fresh volume. If it does on a reused volume, `docker compose down -v`. |

## 9. Common commands

```bash
docker compose up --build            # build + start (foreground)
docker compose up -d --build         # detached
docker compose logs -f app           # tail app logs
docker compose run --rm ollama sh /init/ollama_init.sh   # pre-pull models
docker compose down                  # stop services, keep volumes
docker compose down -v               # stop and delete volumes (fresh DB)
docker compose ps                    # service + health status
```
