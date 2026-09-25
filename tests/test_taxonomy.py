"""Tests for the config-driven taxonomy cascade helpers and validation."""

from __future__ import annotations

from app import taxonomy
from app.config import Product


def test_products_for_category_returns_all():
    products = taxonomy.products_for_category("hydraulics")
    assert [p.id for p in products] == ["pump_a", "valve_b"]
    # A product carries its own labels and article-number list.
    pump = next(p for p in products if p.id == "pump_a")
    assert isinstance(pump, Product)
    assert pump.article_numbers == ["100-001", "100-002"]


def test_products_for_unknown_category_is_empty():
    assert taxonomy.products_for_category("does-not-exist") == []


def test_article_numbers_are_per_product():
    # Different products have different article-number sets.
    pump_articles = taxonomy.article_numbers_for_product("hydraulics", "pump_a")
    valve_articles = taxonomy.article_numbers_for_product("hydraulics", "valve_b")
    assert pump_articles == ["100-001", "100-002"]
    assert valve_articles == ["200-010"]
    assert pump_articles != valve_articles


def test_article_numbers_for_unknown_product_is_empty():
    assert taxonomy.article_numbers_for_product("hydraulics", "no_such_product") == []
    assert taxonomy.article_numbers_for_product("unknown_cat", "pump_a") == []


def test_is_valid_full_triple():
    assert taxonomy.is_valid("hydraulics", "pump_a", "100-001") is True
    assert taxonomy.is_valid("hydraulics", "pump_a", "100-002") is True
    assert taxonomy.is_valid("hydraulics", "valve_b", "200-010") is True


def test_is_valid_article_number_none():
    # A missing article_number is always valid given a valid category/product.
    assert taxonomy.is_valid("hydraulics", "pump_a", None) is True


def test_is_valid_rejects_bad_article_number():
    assert taxonomy.is_valid("hydraulics", "pump_a", "999-999") is False
    assert taxonomy.is_valid("hydraulics", "pump_a", "200-010") is False


def test_is_valid_rejects_bad_product_for_category():
    assert taxonomy.is_valid("hydraulics", "no_such_product", None) is False


def test_is_valid_rejects_unknown_category():
    assert taxonomy.is_valid("unknown_cat", "pump_a", "100-001") is False
    assert taxonomy.is_valid("unknown_cat", "pump_a", None) is False
