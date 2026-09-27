"""Turn-level routing of the chat turn between ``records`` (fs) and RAG docs.

The operator steers a turn with a marker:

* :data:`MARKER` (``#docs``) routes the turn ``rag_only`` — RAG is queried and
  stored records are never searched.
* :data:`FAILS_MARKER` (``#fails``) routes the turn ``records`` — records-first,
  both routing flags off. It takes precedence over ``#docs`` when both appear.

Without a marker the turn is records-first on the first question and
``rag_first`` (RAG, then records fallback) on a follow-up — that "follow-up"
logic lives in :func:`chat`. The policy enforced inside :func:`run_agent` lives
in ``app/agent.py``; this module only turns an incoming user turn into routing
decisions.
"""

from __future__ import annotations

# Operator trigger for RAG-only documentation routing.
MARKER = "#docs"

# Operator trigger for the records-first switch-back.
FAILS_MARKER = "#fails"


def resolve_rag_first(turn: str) -> bool:
    """Return ``True`` when the turn should route RAG-only (``#docs`` tag).

    The operator forces documentation retrieval by tagging the message with
    :data:`MARKER` (``#docs``). Without it, records are not suppressed.
    Detection is case-insensitive so the tag is forgiving to type.

    Args:
        turn: The operator's latest chat message text.

    Returns:
        ``True`` if only RAG should be consulted; ``False`` otherwise.
    """
    lowered = turn.lower()
    return MARKER in lowered


def resolve_records_first(turn: str) -> bool:
    """Return ``True`` when the turn should force records-first (``#fails`` tag).

    Takes precedence over :func:`resolve_rag_first`: when both markers appear in
    a turn, records come first. Detection is case-insensitive.

    Args:
        turn: The operator's latest chat message text.

    Returns:
        ``True`` if records should be consulted first; ``False`` otherwise.
    """
    lowered = turn.lower()
    return FAILS_MARKER in lowered
