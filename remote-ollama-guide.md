# Ollama setup for a machine with a remote server + a local instance

> Self-contained reference. Copy this into any project that shares one of these
> situations. Replace the placeholder IP / model names as needed.

## The situation

You have **two** Ollama servers on the same machine:

| Backend | Address | Reachable by |
|---------|---------|--------------|
| **Remote** Ollama (the "coding" server) | `192.168.0.14:11434` | the `ollama` **CLI**, via `OLLAMA_HOST` |
| **Local** Ollama (the "test" server) | `127.0.0.1:11434` | the app / venv |

You pull and manage models on the remote, but you want the app to run against
the local instance so you don't saturate the shared remote.

## Why `ollama list` and the app point at two different servers

This is the core gotcha:

- The **`ollama` CLI** picks its server from the `OLLAMA_HOST` environment
  variable. Anything that sets it (your shell, a `.zshrc`, a profile) makes
  `ollama list` / `ollama pull` talk to the remote.
- The **app** reads its backend from `FS_OLLAMA_URL`, and when that variable is
  unset it falls back to the `base_url` in `config/settings.yaml`
  (default `http://localhost:11434`). The app **never reads `OLLAMA_HOST`**.

Consequences:

- `ollama list` (remote) and the running app (local) can point at **different**
  servers.
- Models pulled on the remote are **invisible** to the local instance — the app
  will fail on an embed/LLM call because the local server has no model.
- `http://127.0.0.1:11434` and `http://localhost:11434` are the **same** local
  server; either works.

## Diagnose

```bash
# What does the CLI think? (uses OLLAMA_HOST)
env | grep OLLAMA_HOST                 # e.g. http://192.168.0.14:11434
ollama list

# What can the app actually reach?
curl http://127.0.0.1:11434/api/tags   # models the LOCAL instance serves

# What backend does the app resolve to?
FS_OLLAMA_URL="" python -c "from app.config import settings; print(settings.ollama.base_url)"
```

If `ollama list` shows models but `curl 127.0.0.1:11434/api/tags` is empty,
they're on different servers.

## Make them line up

### 1. Point the app at the local instance

Leave `FS_OLLAMA_URL` unset so the `settings.yaml` default applies
(`http://localhost:11434`), or set it explicitly:

```bash
FS_OLLAMA_URL=http://127.0.0.1:11434 uvicorn app.api:app
```

No `OLLAMA_HOST` shim is needed — the app ignores it.

### 2. Pull models against the local instance

The CLI honors `OLLAMA_HOST`, so override just the pull command to hit the
local server from a shell that has `OLLAMA_HOST` pointing at the remote:

```bash
OLLAMA_HOST=http://127.0.0.1:11434 \
  ollama pull snowflake-arctic-embed2:568m   # embed model
OLLAMA_HOST=http://127.0.0.1:11434 \
  ollama pull gemma4:e4b                       # LLM
```

### 3. Verify the app talks to the local server

```bash
FS_OLLAMA_URL=http://127.0.0.1:11434 python - <<'PY'
import time
from app.config import settings
from app.embedding import embed, EmbeddingError
from app.ollama import chat, OllamaError, LLMError

print("base_url =", settings.ollama.base_url)

t = time.time()
print("embed:", len(embed(["example"])[0]), "dim", round(time.time() - t, 1), "s")

t = time.time()
print("llm :", chat([{"role": "user", "content": "Answer with one word: ok"}]))
print("elapsed:", round(time.time() - t, 1), "s")
PY
```

A successful run means the app uses the local instance with the pulled models.

## Notes

- The **test suite mocks Ollama** and never hits a real server, so tests stay
  isolated. Use the smoke test above only to exercise the real (CPU-only) path.
- Ollama processes **one request at a time**, so a single inference call can take
  many seconds on CPU. Call sites already serialize access and use a ~300s
  timeout — don't "fix" slow responses by lowering the timeout.
