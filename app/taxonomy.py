"""Taxonomy cascade helpers and validation.

The F&S vocabulary is config-driven: ``category -> product -> article_numbers``
lives entirely in ``config/taxonomy.yaml`` (see ``CONTRACT.md`` §4). Nothing here
hardcodes vocabulary values; every lookup goes through the module-level
``taxonomy`` object loaded by :mod:`app.config`.

Cascade helpers (used by the WebUI dropdowns and the ``/api/taxonomy/*`` endpoints):

    * :func:`products_for_category`
    * :func:`article_numbers_for_product`

Validation (used by the submit pipeline): a ``product`` must belong to the chosen
``category``; an ``article_number`` (when given) must be a member of that product's
list. :func:`is_valid` encodes exactly that.
"""

from __future__ import annotations

from typing import Optional

from app.config import Category, Product, taxonomy


def _find_category(cat: str) -> Optional[Category]:
    """Return the :class:`Category` for ``cat`` (``None`` if unknown)."""
    for category in taxonomy.categories:
        if category.id == cat:
            return category
    return None


def _find_product(category: Optional[Category], prod: str) -> Optional[Product]:
    """Return the :class:`Product` for ``prod`` within ``category`` (``None`` if absent)."""
    if category is None:
        return None
    for product in category.products:
        if product.id == prod:
            return product
    return None


def products_for_category(cat: str) -> list[Product]:
    """All products belonging to ``cat`` (empty list if the category is unknown)."""
    category = _find_category(cat)
    return list(category.products) if category is not None else []


def article_numbers_for_product(cat: str, prod: str) -> list[str]:
    """Article numbers for ``prod`` in ``cat`` (empty list if the pair is unknown)."""
    category = _find_category(cat)
    product = _find_product(category, prod)
    return list(product.article_numbers) if product is not None else []


def is_valid(cat: str, prod: str, art: Optional[str] = None) -> bool:
    """Validate a ``(category, product, article_number)`` triple.

    ``product`` must belong to ``cat``; ``art`` (when given) must be a member of
    that product's ``article_numbers``. ``article_number=None`` is valid. An
    unknown category or product is invalid.
    """
    category = _find_category(cat)
    product = _find_product(category, prod)
    if product is None:
        return False
    if art is None:
        return True
    return art in product.article_numbers
