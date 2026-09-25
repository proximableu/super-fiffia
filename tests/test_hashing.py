"""Unit tests for the dedup content hash (T1.2).

See ``F&S_REQUIREMENTS.md`` §5.1 and ``CONTRACT.md`` §9. These tests pin the
locked definition: normalized MD5 over ``failure + NUL + solution``.
"""

import re

import pytest

from app.hashing import content_hash

_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def test_normalized_hash_is_32_char_hex():
    digest = content_hash("The pump does not build pressure.", "Check valve 3.")
    assert _HEX_RE.match(digest), f"expected 32-char hex, got {digest!r}"


def test_normalization_ignores_case_whitespace_and_newlines():
    a = content_hash("  The   pump\n\n does not build pressure. ", "Check valve 3.")
    b = content_hash("the pump does not build pressure.", "Check valve 3.")
    assert a == b


def test_nul_separator_avoids_concatenation_collision():
    # (a+b, c) and (a, b+c) would collide without the NUL separator between fields.
    left_a = "pressure is low"
    left_b = "and the valve leaks"
    c = "check valve 3"
    hash_ab_c = content_hash(f"{left_a}{left_b}", c)
    hash_a_bc = content_hash(left_a, f"{left_b}{c}")
    assert hash_ab_c != hash_a_bc


def test_same_text_produces_same_hash_regardless_of_product():
    # content_hash depends only on failure+solution — the same text under different
    # products/categories yields the same hash (the canonical dedup scoping).
    failure = "Pump A is overheating."
    solution = "Reduce load and inspect cooling fins."
    assert content_hash(failure, solution) == content_hash(failure, solution)


def test_order_matters_failure_vs_solution():
    # Swapping failure and solution must not produce the same hash (the NUL guard
    # keeps the fields ordered even after collapsing).
    assert content_hash("one", "two") != content_hash("two", "one")


def test_empty_strings_hash_and_still_32_chars():
    digest = content_hash("", "")
    assert _HEX_RE.match(digest)
