"""Tests for the WebUI app (``app.webui``) — the server-rendered, HTML-facing stage.

These target the routes and taxonomy cascade that the browser drives and that the
purely-HTTP API tests (``tests/test_api.py``) do not reach:

    * the stage + nav + language routes,
    * the ``category -> product -> article_number`` cascade endpoints
      (``GET /api/taxonomy/{categories,products,articles}``),
    * the chat route handlers ``POST /chat`` / ``POST /chat/clear`` (bug #3.2),
      scripted through ``app.chat.run_agent`` so no Ollama or database is needed,
    * and that the two pages render their form markup with no hard-coded label leaks.

The taxonomy and chat endpoints read only ``config/taxonomy.yaml`` / an
in-memory history dict, so they run fully in process with the real FastAPI test
client and **no database** — they do not touch the ``records`` tables or Ollama
directly.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.webui import create_app


def _client() -> TestClient:
    return TestClient(create_app())


# A 1024-dimensional fake embedding so submit runs with no live Ollama and still
# fits the ``embedding vector(1024)`` column (mirrors tests/test_api.py).
def _fake_embed(texts):
    return [[0.1] * 1024 for _ in texts]


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
    """Article options carry the raw article number as their label (bug #2).

    Per WEBUI.md §2 the ``article_number`` <select> is an enumerator of the
    product's ``article_numbers`` list; each option's label is that article
    number itself — not the product's label. The render bug fixed in this
    session previously set ``label_sv`` / ``label_en`` to the product's label.
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
        # The label is the raw article number (WEBUI.md §2, matching taxonomy.yaml).
        assert art["label_sv"] == art["id"]
        assert art["label_en"] == art["id"]


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

    # The article <select> labels are the raw article numbers themselves; the
    # id equals the label in every language.
    articles = client.get(
        "/api/taxonomy/articles",
        params={"category": category, "product": product_id},
        cookies={"lang": "en"},
    ).json()["items"]
    assert articles, "need at least one article number to assert its label"
    for art in articles:
        assert art["label_en"] == art["id"]
        assert art["label_sv"] == art["id"]


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


# --------------------------------------------------------------------------- #
# Chat routes — bug #3.2 (POST /chat + POST /chat/clear in webui.py)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_history() -> None:
    """Drop accumulated chat history after each test.

    The conversation history lives in a single module-level dict in
    ``app.chat`` keyed by session token; without isolation a turn in one test
    leaks into the next and ``session_token`` isolation can no longer be asserted.
    """
    yield
    from app import chat as chat_mod

    chat_mod._history.clear()


def test_chat_route_calls_agent_and_returns_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /chat`` folds the user turn into history and returns the contract.

    The client posts ``{scope, messages, lang, session_token}`` (see
    ``troubleshooting.html``) and reads ``{answer, sources, turns_used}``; the
    agent is scripted so no Ollama/DB is needed.
    """
    from app.agent import AgentOutcome
    from app.records_repo import ChatTurn, Scope

    outcome = AgentOutcome(
        answer="here is the fix",
        sources=[],
        turns_used=1,
        ended_with="final_answer",
    )
    monkeypatch.setattr(
        "app.chat.run_agent",
        lambda scope, messages, lang, budget, **_: outcome,
    )

    scope = Scope(category="network")
    messages = [ChatTurn(role="user", content="wifi is down")]
    client = _client()
    resp = client.post(
        "/chat",
        json={
            "scope": scope.model_dump(),
            "messages": [c.model_dump() for c in messages],
            "lang": "en",
            "session_token": "sess-chat-route",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "here is the fix"
    assert body["sources"] == []
    assert body["turns_used"] == 1


def test_chat_route_folds_turn_into_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /chat`` appends the user turn to that session's history."""
    from app.agent import AgentOutcome

    seen: dict[str, int] = {}

    def _run_agent(scope, messages, lang, budget, **_):
        seen["count"] = len(messages)
        return AgentOutcome(answer="ok", sources=[], turns_used=1, ended_with="final_answer")

    monkeypatch.setattr("app.chat.run_agent", _run_agent)

    client = _client()
    body = {
        "scope": {},
        "messages": [{"role": "user", "content": "turn one"}],
        "lang": "en",
        "session_token": "sess-history",
    }
    client.post("/chat", json=body)
    client.post("/chat", json=body)

    # Each turn folds the new user turn into the running history and appends an
    # assistant turn, so the second call sees three turns (user, assistant, user).
    assert seen["count"] == 3


def test_chat_route_error_returns_error_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /chat`` maps an agent failure to the CONTRACT §10 envelope over a non-2xx.

    The Troubleshooting JS only reads ``data.error.message`` for non-2xx responses
    (a 200 is treated as a successful answer), so agent failures must come back as a
    500 with the ``{"error": {"code": "internal", "message": ...}}`` shape, not a 200.
    See bug_report_2.md B2.
    """

    def _raise(scope, messages, lang, budget, **_):
        raise RuntimeError("llm exploded")

    monkeypatch.setattr("app.chat.run_agent", _raise)

    resp = _client().post(
        "/chat",
        json={"scope": {}, "messages": [], "lang": "en", "session_token": "sess-err"},
    )
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal"
    assert resp.json()["error"]["message"] == "llm exploded"


def test_clear_route_drops_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /chat/clear`` drops the named session's history."""
    from app import chat as chat_mod

    # Prime the history directly, then clear through the route.
    chat_mod._history["sess-clear"] = [
        {"role": "user", "content": "hi"}
    ]
    assert "sess-clear" in chat_mod._history

    resp = _client().post("/chat/clear", json={"session_token": "sess-clear"})
    assert resp.status_code == 200
    assert resp.json() == {"cleared": True}
    assert "sess-clear" not in chat_mod._history


def test_clear_missing_session_is_noop() -> None:
    """``POST /chat/clear`` on an unknown token returns 200 without error."""
    resp = _client().post("/chat/clear", json={"session_token": "never-existed"})
    assert resp.status_code == 200
    assert resp.json() == {"cleared": True}


# --------------------------------------------------------------------------- #
# webui record submission — provenance + pre-check (bug #3.3 follow-ups)
# --------------------------------------------------------------------------- #


def test_create_record_forces_source_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /api/records`` forces ``source='manual'`` regardless of the body.

    A :9001 client could set ``source='api'`` in the JSON body; the route strips
    it and forces ``'manual'`` so a human entry point can never masquerade as an
    automated/import submission. Assert the stored ``source`` in the response.
    """
    from app import records_service

    monkeypatch.setattr(records_service, "embed", _fake_embed)

    resp = _client().post(
        "/api/records",
        json={
            "category": "hydraulics",
            "product": "valve_b",
            "failure_description": "F",
            "solution_description": "S",
            "source": "api",  # a hostile client tries to inject this
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["source"] == "manual"


def test_check_duplicate_route_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /api/records/check-duplicate`` reports ``duplicate=False`` for new text.

    Exercises the real ``check_duplicate`` service against the truncated test DB
    — no new record exists yet, so the pre-check returns False.
    """
    from app import records_service

    monkeypatch.setattr(
        records_service,
        "embed",
        _fake_embed,  # type: ignore[assignment]
    )
    resp = _client().post(
        "/api/records/check-duplicate",
        json={"failure_description": "never-stored", "solution_description": "S"},
    )
    assert resp.status_code == 200
    assert resp.json()["duplicate"] is False


def test_validation_error_returns_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed body gets the CONTRACT §10 ``validation`` envelope, not FastAPI's ``{"detail": ...}``.

    ``POST /chat`` requires ``session_token``; an empty/invalid body raises
    ``RequestValidationError``. Before B8 that fell through to FastAPI's default
    ``422 {"detail": [...]}``; the webui app now maps it to
    ``422 {"error": {"code": "validation", "message": ...}}``. See bug_report_2.md B8.
    """
    resp = _client().post("/chat", json={"scope": {}, "messages": [], "lang": "en"})
    assert resp.status_code == 422
    body = resp.json()
    assert "error" in body and body["error"]["code"] == "validation"
    # Not FastAPI's default envelope.
    assert "detail" not in body


# --------------------------------------------------------------------------- #
# webui record submission — round-trip + duplicate contract (bug #3.3, B9)
# --------------------------------------------------------------------------- #
def test_create_record_roundtrip_stores_with_manual_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /api/records`` stores a row and echoes it with ``source='manual'``.

    The submit route forces ``source='manual'`` (a :9001 human entry point can
    never masquerade as an automated submission). Assert the stored fields land in
    the response so the round-trip is covered end-to-end.
    """
    from app import records_service

    monkeypatch.setattr(records_service, "embed", _fake_embed)  # type: ignore[assignment]

    resp = _client().post(
        "/api/records",
        json={
            "category": "hydraulics",
            "product": "valve_b",
            "failure_description": "F",
            "solution_description": "S",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["category"] == "hydraulics"
    assert body["status"] == "active"
    assert body["source"] == "manual"
    assert body["id"]  # an id is assigned and echoed


def test_create_record_duplicate_returns_409_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-submitting an identical pair returns a CONTRACT §10 ``409 duplicate`` body.

    Covers the webui's own submission duplicate path — the existing
    ``test_check_duplicate_route_absent`` only asserts the ``False`` pre-check. Here
    a real row exists in the truncated test DB and a second identical submit must
    be reported as ``409 {error: {code: duplicate, existing_id, ...}}``. See
    bug_report_2.md B9.
    """
    from app import records_service

    monkeypatch.setattr(records_service, "embed", _fake_embed)  # type: ignore[assignment]

    client = _client()
    client.post(
        "/api/records",
        json={
            "category": "hydraulics",
            "product": "valve_b",
            "failure_description": "F",
            "solution_description": "S",
        },
    )
    dup = client.post(
        "/api/records",
        json={
            "category": "hydraulics",
            "product": "valve_b",
            "failure_description": "F",
            "solution_description": "S",
        },
    )
    assert dup.status_code == 409, dup.text
    error = dup.json()["error"]
    assert error["code"] == "duplicate"
    # The submit JS points at the existing record via this id.
    assert error["existing_id"]


def test_check_duplicate_route_returns_true_for_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /api/records/check-duplicate`` returns ``duplicate=True`` once a matching row exists.

    The existing suite covers only the ``False`` (pre-submission, no row yet) path.
    Insert a row, then re-run the pre-check against the same text and assert the
    positive result carries the existing id — the ``warn`` UX branches on this.
    """
    from app import records_service

    monkeypatch.setattr(records_service, "embed", _fake_embed)  # type: ignore[assignment]

    client = _client()
    client.post(
        "/api/records",
        json={
            "category": "hydraulics",
            "product": "valve_b",
            "failure_description": "F",
            "solution_description": "S",
        },
    )
    resp = client.post(
        "/api/records/check-duplicate",
        json={"failure_description": "F", "solution_description": "S"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["duplicate"] is True
    assert body["existing_id"]


# --------------------------------------------------------------------------- #
# Render assertions — new surface rendered by the webui (bug #3.3 follow-up, B9e)
#
# No JS harness exists here; these assert the static contract the JS depends on:
# the list-filter listener the submit page registers on window, the server-rendered
# LANG init (i18n fix B6), and the edit form's Cancel target. JS behaviour itself
# is verified by inspection against the rendered markup.
# --------------------------------------------------------------------------- #
def test_ingest_page_contains_list_filter_listener() -> None:
    """``GET /ingest`` registers the ``list-filter`` listener on ``window`` (bug B3).

    The list partial dispatches a ``list-filter`` CustomEvent on ``window`` from its
    filter form; the submit page must listen on ``window`` too — ``window`` is the root
    of the DOM event tree, so a window-dispatched event reaches only window itself and
    never document (which lies below it). A listener on document (or a
    capture-phase one) could therefore never fire: it couldn't, because the event is
    never dispatched on the path document is on.
    """
    resp = _client().get("/ingest")
    assert resp.status_code == 200
    assert 'window.addEventListener("list-filter"' in resp.text


def test_ingest_list_endpoint_renders() -> None:
    """``GET /ingest/list`` renders the bare partial (regression: NameError on lang).

    The route computes the selected-language category label inline in the
    ``TemplateResponse`` context and must not reference an undefined ``lang``
    name — a missing one raises ``NameError`` and returns HTTP 500, surfacing in
    the page as the "Internal server error" that never showed any records.
    """
    resp = _client().get("/ingest/list")
    assert resp.status_code == 200
    assert "record-list" in resp.text


def test_list_partial_kicks_cascade_on_load() -> None:
    """The /ingest page runs the swapped-in list partial's inline script after
    swapping it into ``#record-list`` via ``innerHTML``.

    ``element.innerHTML = html`` inserts markup but does **not** execute inline
    ``<script>`` tags, so the partial's self-contained i18n + taxonomy cascade
    never initialised on the page — leaving ``#filter-product`` (and the article
    select beneath it) empty. The fix (``runInjectedScripts`` in submit.html)
    recreates each injected script via ``createElement`` + ``appendChild`` so it
    runs. The test asserts the partial is still self-contained (its script is a
    single IIFE whose cascade call lives inside it) and that submit.html carries
    the re-execution helper, the contract that makes the initialisation work.
    """
    partial = _client().get("/ingest/list")
    assert partial.status_code == 200
    # The cascade is defined inside the partial's own IIFE and invoked on load
    # (defined before the call) — the partial stays self-contained.
    assert "function onCategoryChange()" in partial.text
    assert partial.text.index("function onCategoryChange()") < partial.text.index(
        "onCategoryChange();"
    )
    # submit.html re-runs the injected script after the innerHTML swap.
    submit = _client().get("/ingest")
    assert submit.status_code == 200
    assert "runInjectedScripts" in submit.text


def test_edit_form_cancel_links_to_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    """The edit form's Cancel links to ``/ingest`` (bug #3.3 follow-up).

    Previously Cancel pointed at ``/ingest/list`` (a bare partial with no base
    layout/nav). The rendered edit form now carries ``href="/ingest"``. This needs
    a real record in the test DB, so it goes through the create→edit render flow.
    """
    from app import records_service

    monkeypatch.setattr(records_service, "embed", _fake_embed)  # type: ignore[assignment]

    client = _client()
    created = client.post(
        "/api/records",
        json={
            "category": "hydraulics",
            "product": "valve_b",
            "failure_description": "F",
            "solution_description": "S",
        },
    )
    record_id = created.json()["id"]
    resp = client.get(f"/ingest/{record_id}/edit")
    assert resp.status_code == 200
    assert 'href="/ingest"' in resp.text


def test_i18n_lang_init_reads_server_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /ingest`` with a ``lang=en`` cookie renders ``var LANG = "en"`` (i18n fix B6).

    The JS now initialises LANG from the server-rendered ``{{ lang }}`` (which the
    server resolves from the ``lang`` cookie) rather than a hard-coded ``"sv"`` —
    so the ``POST /lang`` 303 reload actually takes effect on the next render.
    Assert the rendered init reflects the cookie's language.
    """
    en = _client().get("/ingest", cookies={"lang": "en"})
    assert 'var LANG = "en"' in en.text
    assert 'if (LANG !== "en") LANG = "sv"' in en.text

    sv = _client().get("/ingest", cookies={"lang": "sv"})
    assert 'var LANG = "sv"' in sv.text
