"""Dedup content hash for failure/solution knowledge.

The hash is computed purely from the **failure and solution text** (never from
``category`` / ``product`` / ``article_number``) — see ``CONTRACT.md`` §9 and
``F&S_REQUIREMENTS.md`` §5.1. A record is a duplicate of another whenever their
normalised ``failure + solution`` match, even across different products.

The definition is locked (decision 3 in ``F&S_REQUIREMENTS.md`` §11): a normalised
MD5 over the two fields, joined by a NUL byte so that ``(a+b, c)`` and
``(a, b+c)`` cannot collide after whitespace collapsing.
"""

from __future__ import annotations

import hashlib
import re

# Collapse runs of whitespace (incl. newlines) into a single space after trimming.
_WS_RE = re.compile(r"\s+")

# NUL byte separates the two fields so concatenation order matters: a boundary
# between failure/solution can never be inferred from the surrounding text.
_NUL = "\u0000"


def _normalize(text: str) -> str:
    """Normalize a field for hashing: trim, lowercase, collapse whitespace."""
    collapsed = _WS_RE.sub(" ", text.strip().lower())
    return collapsed


def content_hash(failure: str, solution: str) -> str:
    """Return the 32-char hex MD5 dedup hash of ``(failure, solution)``.

    Both fields are normalized (trim → lower → collapse whitespace) before they
    are joined with a NUL separator and hashed. Because only these two fields feed
    the hash, identical failure+solution text produces the same hash regardless of
    the record's ``category`` / ``product`` / ``article_number``.

    :param failure:  the failure description text.
    :param solution: the solution description text.
    :return: the 32-character lowercase hex MD5 digest.
    """
    payload = _normalize(failure) + _NUL + _normalize(solution)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()
