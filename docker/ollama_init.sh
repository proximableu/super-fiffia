#!/usr/bin/env bash
# Pre-pull the Ollama models used by the F&S app so the first embed / chat
# request does not block on a download.
#
# Run it inside the ollama container, e.g.:
#
#   docker compose run --rm ollama sh /init/ollama_init.sh
#
set -euo pipefail

EMBED_MODEL="${FS_EMBED_MODEL:-nomic-embed-text}"
LLM_MODEL="${FS_LLM_MODEL:-qwen2.5:32b-instruct}"

echo "[ollama_init] pulling embedding model '${EMBED_MODEL}'"
ollama pull "${EMBED_MODEL}"

echo "[ollama_init] pulling LLM model '${LLM_MODEL}'"
ollama pull "${LLM_MODEL}"

echo "[ollama_init] done. Local models:"
ollama list
