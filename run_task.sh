#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_task.sh — Assemble a ready-to-paste prompt for one AGENT.md task.
#
# Usage:
#   ./run_task.sh T0.1
#   ./run_task.sh T1.3 | pbcopy          # copy to clipboard (macOS)
#   ./run_task.sh T2.1 > prompt.txt      # save to file
#
# The script prints the full prompt to stdout.  The agentic model reads
# AGENT.md and the spec files itself via its tool harness; this prompt
# inlines only the task block + critical invariants so they survive
# context compaction.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── 1. Validate arguments ───────────────────────────────────────────────────
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <TASK_ID>   (e.g. T0.1, T1.3, T4.2)" >&2
  exit 1
fi

TASK_ID="$1"

# Accept T0.1, T1.10, T12.3, etc.
if ! [[ "$TASK_ID" =~ ^T[0-9]+\.[0-9]+$ ]]; then
  echo "Error: invalid task ID '$TASK_ID'. Expected format: T<phase>.<n>  (e.g. T0.1)" >&2
  exit 1
fi

# ── 2. Locate AGENT.md ──────────────────────────────────────────────────────
AGENT_MD="AGENT.md"
if [[ ! -f "$AGENT_MD" ]]; then
  echo "Error: $AGENT_MD not found in $(pwd). Run from the repo root." >&2
  exit 1
fi

# ── 3. Extract the task block from §4 ───────────────────────────────────────
# Tasks are headed like:  ### T0.1 — Project skeleton + config loader
# We grab from that heading to the next "### T" heading (or "## " section break).
TASK_BLOCK="$(awk -v id="$TASK_ID" '
  BEGIN { in_block=0 }
  # Match the exact task heading
  $0 ~ "^### " id " " { in_block=1; print; next }
  # Stop at the next task heading or a level-2 section
  in_block && /^### T[0-9]+\./ { in_block=0 }
  in_block && /^## /         { in_block=0 }
  in_block { print }
' "$AGENT_MD")"

if [[ -z "$TASK_BLOCK" ]]; then
  echo "Error: task '$TASK_ID' not found in $AGENT_MD §4." >&2
  echo "Available tasks:" >&2
  grep -oP '^### T[0-9]+\.[0-9]+' "$AGENT_MD" | sed 's/^### /  /' >&2
  exit 1
fi

# ── 4. Extract the Depends line (for a friendly warning) ────────────────────
DEPENDS_LINE="$(grep -oP '^\- \*\*Depends:\*\*.*' <<< "$TASK_BLOCK" || true)"
if [[ -n "$DEPENDS_LINE" ]]; then
  DEPS="$(sed 's/.*\*\*Depends:\*\* *//' <<< "$DEPENDS_LINE")"
  if [[ "$DEPS" != "none" && "$DEPS" != "—" && -n "$DEPS" ]]; then
    echo "⚠  Note: $TASK_ID depends on: $DEPS  (ensure those are done first)" >&2
  fi
fi

# ── 5. Print the prompt ─────────────────────────────────────────────────────
cat <<PROMPT
# ROLE
You are a CLI coding agent on Linux (bash, Python 3.12). You have a tool harness:
read files, write/edit files, and run shell commands. Work in the repository root.

# MISSION
Complete exactly ONE task: ${TASK_ID}.
Do not start any other task. Do not refactor beyond this task.

# HOW TO WORK
1. Read AGENT.md (repo root). It is your source of truth:
   - §1 Rules of engagement — follow ALL of them.
   - §2 System overview, §3 Canonical context (schema, API, taxonomy, conventions).
   - §4 Tasks — your task is ${TASK_ID}.
2. Read the files named in ${TASK_ID}'s "Context" list.
   REQUIREMENTS.md, CONTRACT.md, F&S_REQUIREMENTS.md, WEBUI.md, and TODO.md are the
   authoritative specs — consult them whenever a detail is unclear.
3. Implement exactly ${TASK_ID}'s "Deliverable".
4. Run ${TASK_ID}'s "Acceptance" command and paste its output.
5. If blocked, STOP and state exactly what is missing. Do not invent.

# TASK ${TASK_ID} (from AGENT.md §4)
${TASK_BLOCK}

# CRITICAL INVARIANTS (keep these even after context compaction)
- Stack: Python 3.12 · FastAPI · HTMX · PostgreSQL + pgvector · Ollama (embed + LLM) · YAML config · psycopg v3.
- One database (fiffia_fs): tables records, records_audit, rag_chunks. Soft delete only (archive/restore via status); every change audited (records_audit).
- All Ollama calls (embed + LLM) go through app/ollama.py under the shared OLLAMA_LOCK — one request at a time.
- Field name is \`article_number\` (NEVER \`article_nr\`).
- Taxonomy is config-driven: category → product → article_numbers, read from config/taxonomy.yaml. Never hardcode values.
- Enums: status ∈ {active, archived}; source ∈ {manual, import, api}.
- Dedup: content_hash = MD5(normalize(failure) + "\\u0000" + normalize(solution)); block default (unique index → 409).
- Both submission paths (WebUI + API) run the SAME pipeline: validate → dedup → embed → insert.
- Retrieval is structured-first: scope by category+product(+article_number), then semantic+lexical RRF.

# CODE QUALITY
- No placeholders (no TODO/FIXME/stubs). Fully implement.
- PEP 8, full type hints, small functions, no dead code.

# DEFINITION OF DONE
- Acceptance command passes (output pasted).
- Only ${TASK_ID} files created/modified.
- No new lint/type errors.

# ESCALATION
If you cannot complete ${TASK_ID} with the available files, STOP and list exactly what is
missing (a file, a symbol, a config key, or a decision).
PROMPT
