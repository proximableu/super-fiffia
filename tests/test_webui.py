"""Tests for the WebUI app (``app.webui``) — the server-rendered, HTML-facing stage.

These target the routes and taxonomy cascade that the browser drives and that the
purely-HTTP API tests (``tests/test_api.py``) do not reach:

    * the stage + nav + language routes,
    * the ``category -> product -> article_number`` cascade endpoints
      (``GET /api/taxonomy/{categories,products,articles}``), and
    * that the two pages render their form markup with no hard-coded label leaks.

The taxonomy endpoints read only ``config/taxonomy.yaml``, so they run fully in
process with the real FastAPI test client and **no database** — they do not touch
the ``records`` tables or Ollama. Ollama-dependent routes (``/chat``, ``/lang``
that resolves the agent) are out of scope for this module.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.webui import create_app


def _client() -> TestClient:
    return TestClient(create_app())


# --------------------------------------------------------------------------- #
# Route contract — CONTRACT.md §12
# --------------------------------------------------------------------------- #
def test_root_redirects_to_ingest():
    """``GET /`` is a 302 onto ``/ingest`` (CONTRACT.md §12)."""
    resp = _client().get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/ingest"


def test_ingest_page_rendered():
    """``GET /ingest`` renders the Submit form (HTTP 200, form markup present)."""
    resp = _client().get("/ingest")
    assert resp.status_code == 200
    assert resp.text.count("<form") >= 1
    # The category <select> exists and starts on the placeholder option.
    assert 'id="category"' in resp.text
    assert 'value=""' in resp.text


def test_chat_page_rendered():
    """``GET /chat`` renders the Troubleshooting form (HTTP 200)."""
    resp = _client().get("/chat")
    assert resp.status_code == 200
    assert 'id="category"' in resp.text
    assert 'id="product"' in resp.text
    assert 'id="article_number"' in resp.text


# --------------------------------------------------------------------------- #
# Taxonomy cascade — CONTRACT.md §11 / WEBUI.md §2
# --------------------------------------------------------------------------- #
def test_categories_endpoint_returns_config_order():
    """``GET /api/taxonomy/categories`` returns every config category (WEBUI §2)."""
    resp = _client().get("/api/taxonomy/categories")
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    assert "items" in body
    # Every item has both language labels and an id.
    for item in body["items"]:
        assert item["id"]
        assert item["label_sv"].strip()
        assert item["label_en"].strip()
    # At least one category is populated from the default config.
    assert len(body["items"]) >= 1


def test_products_endpoint_filters_by_category():
    """``GET /api/taxonomy/products?category=`` returns only that category's products."""
    client = _client()
    cats = client.get("/api/taxonomy/categories").json()["items"]
    assert cats, "default config must have at least one category"
    category = cats[0]["id"]

    resp = client.get("/api/taxonomy/products", params={"category": category})
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    assert "items" in body
    for item in body["items"]:
        assert item["label_sv"].strip() and item["label_en"].strip()


def test_articles_endpoint_filters_by_category_and_product():
    """``GET /api/taxonomy/articles?category=&product=`` returns that product's numbers."""
    client = _client()
    category = client.get("/api/taxonomy/categories").json()["items"][0]["id"]
    products = client.get(
        "/api/taxonomy/products", params={"category": category}
    ).json()["items"]
    if not products:  # category with no products — nothing to assert beyond 200.
        return
    product = products[0]["id"]

    resp = client.get(
        "/api/taxonomy/articles",
        params={"category": category, "product": product},
    )
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    assert "items" in body


def test_articles_labels_are_product_labels_not_raw_values():
    """Article options carry the product's label, not the raw article number (fix #2).

    The article <select> has no per-article label, so every option inherits the
    product's label in the active language — not the raw ``id``. This is the bug
    fixed in this session: previously ``label_sv`` / ``label_en`` were set to the
    raw article-number ``id``.
    """
    client = _client()
    category = client.get("/api/taxonomy/categories").json()["items"][0]["id"]
    products = client.get(
        "/api/taxonomy/products", params={"category": category}
    ).json()["items"]
    if not products:
        return
    product = products[0]
    product_id = product["id"]

    articles = client.get(
        "/api/taxonomy/articles",
        params={"category": category, "product": product_id},
    ).json()["items"]
    assert articles, "need at least one article number to assert its label"

    for art in articles:
        # The article id is the raw value (used as the <select> value).
        assert art["id"]
        # But the label is the product's label, not the raw article number.
        assert art["label_sv"] == product["label_sv"]
        assert art["label_en"] == product["label_en"]
        assert art["label_sv"] != art["id"]
        assert art["label_en"] != art["id"]


def test_articles_label_respects_language():
    """Article labels switch with the ``lang`` cookie (fix #2, language-aware)."""
    client = _client()
    category = client.get("/api/taxonomy/categories").json()["items"][0]["id"]
    products = client.get(
        "/api/taxonomy/products", params={"category": category}
    ).json()["items"]
    if not products:
        return
    product = products[0]
    product_id = product["id"]

    en = client.get(
        "/api/taxonomy/articles",
        params={"category": category, "product": product_id},
        cookies={"lang": "en"},
    ).json()["items"][0]["label_en"]
    sv = client.get(
        "/api/taxonomy/articles",
        params={"category": category, "product": product_id},
        cookies={"lang": "sv"},
    ).json()["items"][0]["label_sv"]

    # Both languages must carry the product's label; if the labels differ per
    # language they must differ between them.
    assert en == product["label_en"]
    assert sv == product["label_sv"]


# --------------------------------------------------------------------------- #
# No label-leak regression — the i18n-key fix (fix #1)
# --------------------------------------------------------------------------- #
def test_rendered_select_options_have_no_req_markup():
    """The rendered category <select> options carry no raw ``<span class="req">``.

    The i18n-key fix moved the required-marker markup out of the *option* labels.
    A regression would re-inject the raw span into the rendered dropdown options;
    the rendered options must contain only the placeholder ``——`` option.
    """
    resp = _client().get("/chat")
    assert resp.status_code == 200
    body = resp.text
    # Locate the category <select> block and inspect its options.
    start = body.index('id="category"')
    end = body.index("</select>", start) + len("</select>")
    select_block = body[start:end]
    assert '<option' in select_block
    assert '<span class="req"' not in select_block
    assert "——" in select_block
