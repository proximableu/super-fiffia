"""WebUI routes (T5.1 + T5.4).

Jinja2 server rendering for the two WebUI stages (WEBUI.md):

    * ``/ingest`` — the Submit form (§3) and, below it, the record list (§3.5).
    * ``/chat``   — the Troubleshooting form (§4).

and the shared top-level chrome:

    * the top navigation that switches between the two stages (§1);
    * the session language selector (§5) — ``POST /lang`` sets ``sv`` / ``en``;
    * the static assets in ``static/`` (CSS/JS), served under ``/static``.

Each stage page renders on top of the shared ``base.html`` layout: the nav and
the language toggle live in the base template and are therefore visible on both
pages. The per-stage interaction (the cascade, indicators, blocking POSTs) stays
in the page's own ``<script>`` — see ``submit.html``, ``troubleshooting.html``
and the edit form in ``edit.html``.

Routes
------

The list (``GET /ingest/list``) renders a bare partial
(``records_list.html``, no base layout) with the filter form and cards, so it
can be re-rendered by both the list form and the edit page after an action.
The list form's filters (category / product / article_number / status / q)
drive a re-render of this same partial; ``status=all`` shows every row
(``status=archived`` included) via the optional status filter in the repo.

The edit page (``GET / POST /ingest/{id}/edit``) is a full page: its GET returns
the pre-filled edit form and its POST calls the service layer, then redirects
back to the edit page with the result
(``?edit=ok`` / ``?edit=duplicate`` / ``?edit=not_found`` / ``?edit=invalid`` /
``?edit=error``) so the page can surface the message. Archive / Restore
(``POST /ingest/{id}/archive|restore``) call the service layer and redirect back
to the current page; they return ``404`` when the record is missing, matching
the API.
"""

from __future__ import annotations

import logging
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.chat import chat, clear_session
from app.config import settings, taxonomy
from app.db import _checkout, _release
from app.records_repo import (
    ChatTurn,
    DuplicateError,
    NotFoundError,
    RecordIn,
    RecordOut,
    Scope,
    get,
    list_records,
)
from app.records_service import (
    InvalidTaxonomyError,
    archive_record,
    check_duplicate,
    restore_record,
    submit,
    update_record,
)
from app.taxonomy import (
    article_numbers_for_product,
    products_for_category,
)

# Templates live directly under ``templates/``.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
"""The shared Jinja2 template engine (``templates/``)."""

logger = logging.getLogger(__name__)

# ``lang`` cookie name — the session language lives in a cookie so the server can
# server-render the active-language labels and the JS can read it on load.
_LANG_COOKIE = "lang"


def _lang_from_request(request: Request) -> str:
    """Return the session language (``sv`` by default), from cookie then settings."""
    cookie = request.cookies.get(_LANG_COOKIE)
    if cookie in ("sv", "en"):
        return cookie
    default = settings.ui.lang_default
    return default if default in ("sv", "en") else "sv"


class TaxonomyItem(BaseModel):
    """A single taxonomy member — one product or one article number."""

    id: str
    label_sv: str
    label_en: str


class TaxonomyProductsResult(BaseModel):
    """Body of ``GET /api/taxonomy/products?category=<id>`` (empty for unknown category)."""

    items: list[TaxonomyItem] = []


class TaxonomyArticlesResult(BaseModel):
    """Body of ``GET /api/taxonomy/articles?category=&product=<id>`` (empty for unknown pair)."""

    items: list[TaxonomyItem] = []


class TaxonomyCategoriesResult(BaseModel):
    """Body of ``GET /api/taxonomy/categories`` — all top-level categories in config order."""

    items: list[TaxonomyItem] = []


class CheckDuplicateRequest(BaseModel):
    """Body of ``POST /api/records/check-duplicate``.

    Carries only the two hash fields: the pre-check compares ``failure_description`` /
    ``solution_description`` text, never a taxonomy triple, so the unique index is never
    weakened by a pre-submission query.
    """

    failure_description: str = Field(min_length=1)
    solution_description: str = Field(min_length=1)


class CheckDuplicateResult(BaseModel):
    """Result of the pre-submission duplicate pre-check (CONTRACT.md §10)."""

    duplicate: bool
    existing_id: Optional[str] = None
    created_at: Optional[str] = None


class ChatRequest(BaseModel):
    """Body of ``POST /chat`` (the Troubleshooting JS contract).

    Mirrors the REST ``ChatRequest`` with the UI-selected ``scope`` header
    (category / product / article_number / failure_description) and the session
    token the client mints into ``localStorage`` and posts in the body.
    """

    messages: list[ChatTurn]
    scope: Scope | None = None
    lang: Literal["sv", "en"] = "sv"
    session_token: str


class ChatClearRequest(BaseModel):
    """Body of ``POST /chat/clear``."""

    session_token: str


def create_app() -> FastAPI:
    """Build and return the WebUI application (``uvicorn app.webui:app``).

    Mounts the static assets, the Jinja2 templates and the stage/nav/lang routes
    (see the CONTRACT.md §12 route table — only the T5.1 surface is mounted here).
    """
    app = FastAPI(title="F&S WebUI", version="0.1.0")
    app.exception_handler(RequestValidationError)(_on_validation_error)
    app.exception_handler(NotFoundError)(_on_not_found)
    app.exception_handler(InvalidTaxonomyError)(_on_invalid_taxonomy)
    app.exception_handler(DuplicateError)(_on_duplicate)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    def root() -> RedirectResponse:
        """Bare root lands on the Submit stage."""
        return RedirectResponse(url="/ingest", status_code=302)

    @app.get("/health")
    def health() -> JSONResponse:
        """Liveness probe for the compose healthcheck (CONTRACT.md §12 surface).

        A bare 200 is sufficient: the webui is a server-rendered HTML app and,
        like the API's ``/api/health``, its dependencies are gate-kept by the
        compose ``depends_on: service_healthy`` clauses.
        """
        return JSONResponse(status_code=200, content={"status": "ok"})

    @app.get("/ingest", response_class=HTMLResponse)
    def ingest(request: Request) -> HTMLResponse:
        """Render the Submit form on the shared base layout (§3, §1)."""
        return templates.TemplateResponse(
            request,
            "submit.html",
            {"lang": _lang_from_request(request)},
        )

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request) -> HTMLResponse:
        """Render the Troubleshooting form on the shared base layout (§4, §1)."""
        return templates.TemplateResponse(
            request,
            "troubleshooting.html",
            {"lang": _lang_from_request(request)},
        )

    @app.post("/chat", status_code=200, response_model=None)
    def chat_endpoint(body: ChatRequest) -> dict[str, Any]:
        """One orchestrator turn for the Troubleshooting form (bug #3.2).

        The client posts ``{scope, messages, lang, session_token}`` (see
        ``troubleshooting.html``) and reads ``{answer, sources, turns_used}``; on
        failure it reads ``{error: {message}}`` so this mirrors the REST
        ``/api/chat`` contract with an added per-session ``session_token``.
        ``response_model=None`` (and the ``dict`` return type) let this route return
        either the chat contract or the ``{"error": ...}`` shape without FastAPI
        trying to validate both through a single model.
        """
        try:
            return chat(body.scope, body.messages, body.lang, body.session_token).model_dump()
        except Exception as exc:  # noqa: BLE001 - surface any agent failure as JSON
            # Agent failures must reach the Troubleshooting JS as a non-2xx
            # (its else-branch reads `data.error.message` on non-2xx; a 200 is
            # treated as a successful answer and rendered as an empty bubble).
            # See bug_report_2.md B2: the previous 200-shape made that branch
            # unreachable. Log the traceback; surface only the operator-visible
            # message. ``exc_info`` is passed as the live tuple here because the
            # package's structured-logger override does not resolve a bare
            # ``exc_info=True`` (as ``logging._log`` normally does) before storing
            # it on the record, so ``logger.exception`` would crash the formatter.
            logger.error(
                "chat endpoint agent failure for session %r",
                body.session_token,
                exc_info=sys.exc_info(),
            )
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "internal", "message": str(exc)}},
            )

    @app.post("/chat/clear", status_code=200)
    def chat_clear_endpoint(body: ChatClearRequest) -> dict[str, bool]:
        """Drop the named conversation's history (bug #3.2).

        The client posts ``{session_token}`` and expects a 200 JSON body; the call
        is a no-op when the token is unknown, matching the client's optimistic UI.
        """
        clear_session(body.session_token)
        return {"cleared": True}

    @app.post("/lang")
    def set_lang(request: Request) -> RedirectResponse:
        """Set the session language (``sv``/``en``) and re-render the current page.

        The language is read from the request body (``{"lang": "en"}``), from the
        cookie, or falls back to the configured default (§5).
        """
        lang = _lang_from_post(request)
        response = RedirectResponse(
            url=request.url.path, status_code=303
        )
        response.set_cookie(_LANG_COOKIE, lang, max_age=60 * 60 * 24 * 365, path="/")
        return response

    # --- Record list (T5.4: /ingest/list) ---------------------------------- #

    @app.get("/ingest/list", response_class=HTMLResponse)
    def record_list(
        request: Request,
        category: str = "",
        product: str = "",
        article_number: str = "",
        status: str = "active",
        q: str = "",
    ) -> HTMLResponse:
        """Render the record list partial (filters + cards).

        Filters drive a re-render of this partial. ``category`` / ``product`` /
        ``article_number`` are ``""`` (sent as ``None`` to the repo) unless the
        user picks a value; ``status`` is ``active`` / ``archived`` / ``all``.
        """
        conn = _checkout()
        try:
            items, count = list_records(
                conn,
                Scope(
                    category=category or None,
                    product=product or None,
                    article_number=article_number or None,
                ),
                status if status in ("active", "archived") else "",
                q or None,
                limit=200,
                offset=0,
            )
        finally:
            _release(conn)
        return templates.TemplateResponse(
            request,
            "records_list.html",
            {
                "lang": _lang_from_request(request),
                "items": items,
                "count": count,
                "filters": {
                    "category": category or None,
                    "product": product or None,
                    "article_number": article_number or None,
                    "status": status,
                    "q": q,
                },
                "categories": _categories_for_select(taxonomy, _lang_from_request(request)),
            },
        )

    # --- Edit / archive / restore (T5.4) ------------------------------------ #

    @app.get("/ingest/{record_id}/edit", response_class=HTMLResponse)
    def edit_form(
        request: Request, record_id: UUID, lang: str = ""
    ) -> HTMLResponse:
        """Render the pre-filled edit form; 404 when the record is missing."""
        conn = _checkout()
        try:
            record = get(conn, record_id)
            if record is None:
                conn.rollback()
                raise NotFoundError(str(record_id))
        finally:
            _release(conn)
        return templates.TemplateResponse(
            request,
            "edit.html",
            {
                "lang": lang or _lang_from_request(request),
                "record": record,
                "categories": _categories_for_select(taxonomy, _lang_from_request(request)),
            },
        )

    @app.post("/ingest/{record_id}/edit")
    async def edit_save(request: Request, record_id: UUID) -> RedirectResponse:
        """Save an edited record; redirect back to the edit page with the result.

        A valid edit redirects ``?edit=ok``; a duplicate, missing record, invalid
        taxonomy or failure is surfaced as ``?edit=duplicate`` / ``?edit=not_found``
        / ``?edit=invalid`` / ``?edit=error``. The edit page renders the message
        and re-pre-fills the form from the saved value where possible.
        """
        # Re-fetch the record (with rollback safety) before mutating so a
        # missing id always redirects to ?edit=not_found rather than 500.
        try:
            body = await request.form()
        except Exception:  # noqa: BLE001 - fall through to the 422/invalid path
            return _edit_redirect(record_id, "invalid")

        try:
            payload = RecordIn(
                category=str(body.get("category", "")),
                product=str(body.get("product", "")),
                article_number=body.get("article_number") or None,
                failure_description=str(body.get("failure_description", "")),
                solution_description=str(body.get("solution_description", "")),
                ncr=body.get("ncr") or None,
                bug_record_number=body.get("bug_record_number") or None,
                source="manual",
            )
        except Exception:  # noqa: BLE001 - malformed -> invalid
            return _edit_redirect(record_id, "invalid")

        try:
            updated = update_record(record_id, payload, actor="web")
            return RedirectResponse(
                url=f"/ingest/{updated.id}/edit?edit=ok", status_code=303
            )
        except DuplicateError:
            return _edit_redirect(record_id, "duplicate")
        except InvalidTaxonomyError:
            return _edit_redirect(record_id, "invalid")
        except FileNotFoundError:
            return _edit_redirect(record_id, "not_found")
        except Exception:  # noqa: BLE001 - surface as a generic edit error
            return _edit_redirect(record_id, "error")

    @app.post("/ingest/{record_id}/archive")
    def save_archive(request: Request, record_id: UUID) -> RedirectResponse:
        """Soft-archive a record; redirect back to the caller. 404 when missing."""
        try:
            archive_record(record_id, actor="web")
        except FileNotFoundError:
            raise NotFoundError(str(record_id))
        return _back_redirect(request)

    @app.post("/ingest/{record_id}/restore")
    def save_restore(request: Request, record_id: UUID) -> RedirectResponse:
        """Restore an archived record; redirect back to the caller. 404 when missing.

        A restore that now collides with another active record is reported as
        ``?restore=collided`` so the caller can explain the conflict; anything
        else surfaces as ``?restore=error``.
        """
        try:
            restore_record(record_id, actor="web")
        except FileNotFoundError:
            raise NotFoundError(str(record_id))
        except DuplicateError:
            return RedirectResponse(
                url=_referer(request) + "?restore=collided", status_code=303
            )
        except Exception:  # noqa: BLE001
            return RedirectResponse(
                url=_referer(request) + "?restore=error", status_code=303
            )
        return _back_redirect(request)

    # --- Taxonomy cascade (WEBUI.md §2) ---
    # These JSON endpoints feed the category→product→article_number selects on the
    # submit/chat pages. They mirror the same surfaces the API exposes on :9000, so
    # the WebUI app on :9001 can populate its own selects without a cross-service call.

    @app.get(
        "/api/taxonomy/categories",
        status_code=200,
        responses={200: {"model": "TaxonomyCategoriesResult"}},
    )
    def taxonomy_categories() -> TaxonomyCategoriesResult:
        """All top-level categories (WebUI cascade) — config order."""
        return TaxonomyCategoriesResult(
            items=[
                TaxonomyItem(
                    id=category.id,
                    label_sv=category.label_sv,
                    label_en=category.label_en,
                )
                for category in taxonomy.categories
            ]
        )

    @app.get(
        "/api/taxonomy/products",
        status_code=200,
        responses={200: {"model": "TaxonomyProductsResult"}},
    )
    def taxonomy_products(category: str = Query(...)) -> TaxonomyProductsResult:
        """Products for ``category`` (WebUI cascade) — config order, empty if unknown."""
        return TaxonomyProductsResult(
            items=[
                TaxonomyItem(
                    id=product.id,
                    label_sv=product.label_sv,
                    label_en=product.label_en,
                )
                for product in products_for_category(category)
            ]
        )

    @app.get(
        "/api/taxonomy/articles",
        status_code=200,
        responses={200: {"model": "TaxonomyArticlesResult"}},
    )
    def taxonomy_articles(
        request: Request,
        category: str = Query(...),
        product: str = Query(...),
    ) -> TaxonomyArticlesResult:
        """Article numbers for ``product`` (WebUI cascade) — empty if unknown.

        Each option's label is the raw article number itself (WEBUI §2: the
        ``article_number`` <select> enumerates the product's ``article_numbers``).
        """
        return TaxonomyArticlesResult(
            items=[
                TaxonomyItem(id=art, label_sv=art, label_en=art)
                for art in article_numbers_for_product(category, product)
            ]
        )

    # --- Record submission (§3.3) ---
    # The submit form (submit.html) posts a relative `/api/records` body and the
    # `warn` UX pre-checks via `/api/records/check-duplicate`. On :9001 the API
    # lives in the *other* process (:9000), so those calls would hit 404 unless
    # this app serves them. Mounting the two JSON routes here closes the seam
    # without a proxy: both UIs share the one `records_service` pipeline. The web
    # actor uses `source="manual"` (the form never carries an API source) and the
    # `web` actor id, matching the in-page edit path; the unique dedup index stays
    # untouched, so a check-duplicate pre-check weakens nothing.

    @app.post(
        "/api/records",
        status_code=201,
        responses={409: {"model": "Error"}, 422: {"model": "Error"}},
    )
    def create_record(body: RecordIn) -> RecordOut:
        """Submit a record (``source='manual'``, ``actor='web'``) via the pipeline.

        The form is a human entry point, never an API/import, so ``source`` is
        stripped and forced to ``'manual'`` before the pipeline — a :9001 client
        can otherwise set ``source='api'``/``'import'``, which the API route
        prevents by re-wrapping in ``ApiRecordIn``.
        """
        payload = {k: v for k, v in body.model_dump().items() if k != "source"}
        return submit(RecordIn(**payload, source="manual"), actor="web")

    @app.post(
        "/api/records/check-duplicate",
        responses={200: {"model": "CheckDuplicateResult"}},
    )
    def check_duplicate_record(body: CheckDuplicateRequest) -> CheckDuplicateResult:
        """Pre-submission dedup check (the ``warn`` UX) — the index never weakens."""
        existing = check_duplicate(body.failure_description, body.solution_description)
        if existing is None:
            return CheckDuplicateResult(duplicate=False)
        return CheckDuplicateResult(
            duplicate=True,
            existing_id=str(existing.existing_id),
            created_at=existing.created_at.isoformat(),
        )

    return app


def _lang_from_post(request: Request) -> str:
    """Resolve the requested language from a JSON body, a cookie, or the default.

    ``POST /lang`` may be called with a JSON ``{"lang": ...}`` body; when the body
    is absent (e.g. the client only set the cookie) the cookie value is used.
    """
    body = None
    try:
        body = request.json()
    except Exception:  # noqa: BLE001 - non-JSON body, fall through to cookie
        pass
    if isinstance(body, dict):
        candidate = body.get("lang")
        if candidate in ("sv", "en"):
            return candidate
    return _lang_from_request(request)


def _categories_for_select(
    taxonomy: object, lang: str
) -> list[dict]:
    """Return the taxonomy categories as ``{id, label}`` (language-selected).

    Used to populate the category <select> in both the list filter and the edit
    form. Labels are chosen from the active session language so the dropdowns
    render in the user's language without a client-side i18n table.
    """
    return [
        {"id": category.id, "label": getattr(category, f"label_{lang}")}
        for category in taxonomy.categories
    ]


def _back_redirect(request: Request) -> RedirectResponse:
    """Redirect back to the referring page (the list filter form) after an action."""
    return RedirectResponse(url=_referer(request) or "/ingest/list", status_code=303)


def _referer(request: Request) -> str:
    """Return the referring page's path + query (filters intact), or the list.

    Archive/Restore redirect back to the caller. The referer is a full URL in
    the ``Referer`` header; we parse out its path + query so the list filter
    form (which carries its filters as query params) re-renders with the same
    filters. Any ``?edit=`` / ``?restore=`` status flag left on the query is
    dropped so a successful action is clean.
    """
    ref = request.headers.get("referer")
    if not ref:
        return "/ingest/list"
    try:
        parts = urllib.parse.urlparse(ref)
    except ValueError:
        return "/ingest/list"
    path = parts.path or "/ingest/list"
    flags = {"edit", "restore"}
    query = urllib.parse.parse_qs(parts.query)
    query = {k: v for k, v in query.items() if k not in flags}
    rebuilt = urllib.parse.urlencode(query, doseq=True)
    return f"{path}?{rebuilt}" if rebuilt else path


def _edit_redirect(record_id: UUID, outcome: str) -> RedirectResponse:
    """Redirect to the edit page with the given ``?edit=`` outcome flag.

    Success and every error surface here so the edit page renders the message and
    re-pre-fills the form; never the list, which would drop the flag.
    """
    return RedirectResponse(
        url=f"/ingest/{record_id}/edit?edit={outcome}", status_code=303
    )




def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Map a Pydantic / body validation failure to ``422 validation``.

    Malformed JSON on the mounted JSON routes (e.g. the missing ``session_token``
    on ``POST /chat``) otherwise falls through to FastAPI's default
    ``422 {"detail": ...}``; this reports the first offending field with the
    same §10 envelope the REST API uses.
    """
    first = exc.errors()[0]
    field = ".".join(str(loc) for loc in first["loc"]) or "body"
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation",
                "message": f"{field}: {first['msg']}",
            }
        },
    )


def _on_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    """Map a missing record to ``404 not_found`` (archive/restore 404s)."""
    return JSONResponse(status_code=404, content={"error": {"code": "not_found"}})


def _on_invalid_taxonomy(
    request: Request, exc: InvalidTaxonomyError
) -> JSONResponse:
    """Map an out-of-vocabulary taxonomy triple to ``422 invalid_taxonomy``.

    The submit form routes record submission through here so an invalid triple is
    reported with the same envelope as the REST API, letting the client surface the
    cause instead of a generic error.
    """
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "invalid_taxonomy", "message": str(exc)}},
    )


def _on_duplicate(request: Request, exc: DuplicateError) -> JSONResponse:
    """Map a duplicate record to ``409 duplicate`` plus the existing record.

    Mirrors the REST API envelope (CONTRACT.md §10): a 409 carrying ``existing_id``
    and ``created_at`` inside the error object so the submit form can point at the
    record that already exists.
    """
    body = jsonable_encoder(
        {
            "error": {
                "code": "duplicate",
                "message": "a record with identical failure and solution exists",
                "existing_id": str(exc.existing_id),
                "created_at": exc.created_at,
            }
        }
    )
    return JSONResponse(status_code=409, content=body)


app = create_app()
