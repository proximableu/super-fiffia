"""FastAPI application: the app factory, `/api/health`, and the §3.2 error envelope.

This module wires the service / retrieval / chat layers to the web surface
(``CONTRACT.md`` §10). At this stage it deliberately contains **only** the pieces
called for by this task:

    * :func:`create_app` — the FastAPI app factory (``uvicorn app.api:app``);
    * ``GET /api/health`` — a dependency probe (DB + Ollama);
    * the error envelope — every failure the app raises is translated here into
      ``{"error": {"code", "message"}}`` so the outside world never sees a raw
      traceback or a framework-shaped error body.

Record / search / chat / taxonomy endpoints (T4.2–T4.4, T5.2) are mounted here by
those tasks.

Error codes (``CONTRACT.md`` §10): ``duplicate`` | ``invalid_taxonomy`` |
``not_found`` | ``validation`` | ``internal``. A ``duplicate`` error additionally
carries ``existing_id`` + ``created_at`` in the error object.
"""

from __future__ import annotations

import httpx
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional
from typing import Awaitable
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.requests import Request
from pydantic import BaseModel, Field

from app.config import settings, taxonomy
from app.db import _checkout, _release
from app.ollama import OLLAMA_LOCK, OllamaError
from app.records_repo import (
    ChatTurn,
    DuplicateError,
    Hit,
    NotFoundError,
    RecordIn,
    RecordOut,
    Scope,
    Source,
    get,
    list_records,
)
from app.records_service import (
    InvalidTaxonomyError,
    archive_record,
    bulk,
    check_duplicate,
    restore_record,
    submit,
    update_record,
)
from app.taxonomy import article_numbers_for_product, products_for_category
from app.retrieval import retrieve_fs, retrieve_rag
from app.chat import ChatResponse, chat
from app.rag import ingest
from app.logging import LogContext

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Request / response models (CONTRACT.md §10)
# --------------------------------------------------------------------------- #
class ApiRecordIn(RecordIn):
    """A :class:`RecordIn` that defaults ``source`` to ``"api"``.

    External (REST) submits are automated, so a record created through the API
    is tagged ``source='api'`` unless the caller overrides it explicitly
    (CONTRACT.md §10). ``article_number`` remains optional; an explicit value
    must still belong to the product's list — :func:`records_service.submit`
    raises :exc:`InvalidTaxonomyError` otherwise.
    """

    source: Source = "api"


class CheckDuplicateRequest(BaseModel):
    """Body of ``POST /api/records/check-duplicate``.

    Carries only the two hash fields: the pre-check compares ``failure`` /
    ``solution`` text, never a taxonomy triple, so the unique index is never
    weakened by a pre-submission query.
    """

    failure_description: str = Field(min_length=1)
    solution_description: str = Field(min_length=1)


class CheckDuplicateResult(BaseModel):
    """Result of the pre-submission duplicate pre-check (CONTRACT.md §10)."""

    duplicate: bool
    existing_id: Optional[str] = None
    created_at: Optional[str] = None


class ListResult(BaseModel):
    """Body of ``GET /api/records`` (CONTRACT.md §10)."""

    items: list[RecordOut]
    count: int


class RecordBulkItem(RecordIn):
    """A single entry of ``POST /api/records/bulk`` — a :class:`RecordIn`.

    An optional ``id`` selects an existing record for an update; leaving it unset
    inserts a new record (CONTRACT.md §10). ``source`` still defaults to
    ``"api"`` — see :class:`ApiRecordIn`.
    """

    source: Source = "api"
    id: Optional[UUID] = None


class BulkResult(BaseModel):
    """Body of ``POST /api/records/bulk`` (CONTRACT.md §10)."""

    created: int
    updated: int
    errors: list[dict] = []


class BulkRequest(BaseModel):
    """Body of ``POST /api/records/bulk`` — a list of :class:`RecordBulkItem`."""

    items: list[RecordBulkItem] = []


class TaxonomyItem(BaseModel):
    """A single taxonomy member — one product or one article number (CONTRACT.md §4)."""

    id: str
    label_sv: str
    label_en: str


class TaxonomyProductsResult(BaseModel):
    """Body of ``GET /api/taxonomy/products?category=<id>``.

    The products belonging to ``category``, rendered in config order with their
    language-specific labels (WEBUI.md §2). An unknown category yields an empty list.
    """

    items: list[TaxonomyItem] = []


class TaxonomyArticlesResult(BaseModel):
    """Body of ``GET /api/taxonomy/articles?category=&product=<id>``.

    The ``article_number`` members of ``product`` (WEBUI.md §2). Each carries the raw
    string value plus its localized label; the list is empty for an unknown pair.
    """

    items: list[TaxonomyItem] = []


class TaxonomyCategoriesResult(BaseModel):
    """Body of ``GET /api/taxonomy/categories``.

    All top-level categories in config order with their language-specific labels
    (WEBUI.md §2). The WebUI fetches these on page load to populate the ``category``
    <select> before the product cascade begins.
    """

    items: list[TaxonomyItem] = []


class SearchRequest(BaseModel):
    """Body of ``POST /api/search`` (CONTRACT.md §6/§8).

    ``scope`` filters the records leg only; ``source`` selects which leg(s) to run;
    ``top_k`` caps the returned hits (``None`` → the per-leg default).
    """

    q: str = Field(min_length=1)
    source: Literal["records", "rag", "both"] = "records"
    scope: Optional[Scope] = None
    top_k: Optional[int] = None


class SearchResponse(BaseModel):
    """Body of ``POST /api/search`` (CONTRACT.md §6) — the hits plus their count."""

    results: list[Hit] = []
    count: int


class RagIngestResponse(BaseModel):
    """Body of ``POST /api/rag/ingest`` (CONTRACT.md §6)."""

    files: int
    chunks: int
    embedded: int
    upserted: int


class RagIngestRequest(BaseModel):
    """Body of ``POST /api/rag/ingest``.

    An optional ``source_dir`` overrides where to read documents from; when
    omitted the default document directory (``rag_source``) is used.
    """

    source_dir: str = ""


class ChatRequest(BaseModel):
    """Body of ``POST /api/chat`` (CONTRACT.md §6).

    ``messages`` is the running conversation with the last turn the new user
    message; ``scope`` carries the UI-selected context header; ``lang`` selects
    the answer language.
    """

    messages: list[ChatTurn]
    scope: Optional[Scope] = None
    lang: Literal["sv", "en"] = "sv"


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
# application connection pool (it must not be able to starve request workers);
# the Ollama probe still reuses the shared OLLAMA_LOCK so it serialises against
# chat / search requests (NFR-2).
_HEALTH_HTTP = httpx.Client()


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app() -> FastAPI:
    """Build and return the FastAPI application.

    Health route and every envelope exception handler are mounted on the app
    here so they can be reused in tests without spinning up a server.
    """
    from app.logging import setup

    setup()  # install JSON logging (NFR-8) once per process
    app = FastAPI(title="F&S knowledge base", version="0.1.0")
    _register_handlers(app)
    _register_routes(app)
    _register_middleware(app)
    return app


def _register_middleware(app: FastAPI) -> None:
    """Attach the request-id / latency middleware (NFR-8).

    Every request gets a fresh ``X-Request-Id`` and, while the request is in
    flight, that id is merged into every log record emitted by the app so the
    agent / retrieval / db logs for a single request are traceable. The
    per-request latency is logged on completion.
    """

    @app.middleware("http")
    async def _request_tracing(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = str(uuid4())
        with LogContext(request_id=request_id):
            start = time.perf_counter()
            response: Response | None = None
            try:
                response = await call_next(request)
            finally:
                latency_ms = (time.perf_counter() - start) * 1000
                # Guard against an escaped exception from ``call_next`` (the app's
                # ``Exception`` handler below only runs per-route, so a middleware
                # level ``CancelledError`` etc. never assigns ``response``).
                # A bare ``response.status_code`` here would raise ``NameError``
                # and replace the original error; log the failure instead.
                status = response.status_code if response is not None else 500
                logger.info(
                    "http_request",
                    stage="app",
                    method=request.method,
                    path=request.url.path,
                    status=status,
                    latency_ms=round(latency_ms, 2),
                )
        response.headers["X-Request-Id"] = request_id
        return response


def _register_handlers(app: FastAPI) -> None:
    """Register one handler per failure kind, most specific first."""
    app.exception_handler(RequestValidationError)(_on_validation_error)
    app.exception_handler(InvalidTaxonomyError)(_on_invalid_taxonomy)
    app.exception_handler(DuplicateError)(_on_duplicate)
    app.exception_handler(NotFoundError)(_on_not_found)
    app.exception_handler(HTTPException)(_on_http_exception)
    app.exception_handler(Exception)(_on_unexpected)  # noqa: BLE001 - last resort


def _register_routes(app: FastAPI) -> None:
    @app.get("/api/health")
    def health() -> Response:
        body = _health()
        status = (
            200 if body["db"] == "ok" and body["ollama"] == "ok" else 503
        )
        return JSONResponse(status_code=status, content=body)

    @app.post(
        "/api/records",
        status_code=201,
        responses={409: {"model": "Error"}, 422: {"model": "Error"}},
    )
    def create_record(body: RecordIn) -> RecordOut:
        """Submit a record (automated, ``source='api'``) via the service pipeline."""
        payload = {k: v for k, v in body.model_dump().items() if k != "source"}
        return submit(ApiRecordIn(**payload), actor="api")

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

    @app.get(
        "/api/records",
        responses={422: {"model": "Error"}},
    )
    def list_records_endpoint(
        category: Optional[str] = None,
        product: Optional[str] = None,
        article_number: Optional[str] = None,
        status: Source = "active",
        q: Optional[str] = None,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> ListResult:
        """List records (default active) with optional filters, free text and paging."""
        conn = _checkout()
        try:
            items, count = list_records(
                conn,
                Scope(
                    category=category,
                    product=product,
                    article_number=article_number,
                ),
                status,
                q,
                limit,
                offset,
            )
        finally:
            _release(conn)
        return ListResult(items=items, count=count)

    @app.get(
        "/api/records/{record_id}",
        status_code=200,
        responses={404: {"model": "Error"}},
    )
    def get_record(record_id: UUID) -> RecordOut:
        """Fetch a single record by id (200) or 404 when absent."""
        conn = _checkout()
        try:
            record = get(conn, record_id)
            if record is None:
                conn.rollback()
                raise NotFoundError(f"record {record_id} not found")
            return record
        finally:
            _release(conn)

    @app.put(
        "/api/records/{record_id}",
        status_code=200,
        responses={404: {"model": "Error"}, 409: {"model": "Error"}, 422: {"model": "Error"}},
    )
    def update_record_endpoint(record_id: UUID, body: RecordIn) -> RecordOut:
        """Update a record; ``source='api'`` unless the caller overrides it."""
        payload = {k: v for k, v in body.model_dump().items() if k != "source"}
        return update_record(
            record_id, ApiRecordIn(**payload), actor="api"
        )

    @app.post(
        "/api/records/{record_id}/archive",
        status_code=200,
        responses={404: {"model": "Error"}},
    )
    def archive_record_endpoint(record_id: UUID) -> RecordOut:
        """Soft-archive a record; 404 when absent."""
        try:
            return archive_record(record_id, actor="api")
        except FileNotFoundError:
            raise NotFoundError(f"record {record_id} not found")

    @app.post(
        "/api/records/{record_id}/restore",
        status_code=200,
        responses={404: {"model": "Error"}, 409: {"model": "Error"}},
    )
    def restore_record_endpoint(record_id: UUID) -> RecordOut:
        """Restore an archived record; 404 when absent, 409 on hash collision."""
        return restore_record(record_id, actor="api")

    @app.post(
        "/api/records/bulk",
        status_code=200,
        responses={422: {"model": "Error"}},
    )
    def bulk_records(body: BulkRequest) -> BulkResult:
        """Bulk create; 409 on duplicate, 422 on validation error."""
        result = bulk(body.items, actor="api")
        return BulkResult(
            created=result["created"],
            updated=result.get("updated", 0),
            errors=result["errors"],
        )

    @app.post(
        "/api/search",
        status_code=200,
        responses={200: {"model": "SearchResponse"}},
    )
    def search(body: SearchRequest) -> SearchResponse:
        """Records and/or RAG search over ``q`` (CONTRACT.md §10).

        ``source=records`` -> :func:`retrieve_fs`; ``source=rag`` ->
        :func:`retrieve_rag`; ``both`` -> concatenation of the two legs. ``scope``
        filters the records leg only. Returns ``{results, count}`` sorted by score.
        """
        hits: list[Hit] = []
        if body.source in ("records", "both"):
            # ``scope`` is optional on the request; treat a missing scope as the
            # empty filter (category/product/article_number all None).
            scope = body.scope or Scope()
            hits.extend(retrieve_fs(scope, body.q, body.top_k))
        if body.source in ("rag", "both"):
            hits.extend(retrieve_rag(body.q, body.top_k))
        hits.sort(key=lambda h: h.score, reverse=True)
        return SearchResponse(results=hits, count=len(hits))

    @app.post(
        "/api/chat",
        status_code=200,
        responses={200: {"model": "ChatResponse"}},
    )
    def chat_endpoint(body: ChatRequest) -> ChatResponse:
        """One orchestrator chat turn (CONTRACT.md §11). Blocking; serialised via
        ``OLLAMA_LOCK`` inside :func:`run_agent` (NFR-2).

        The incoming user turn is folded into the session history before the agent
        runs, so follow-up turns carry the running context.
        """
        return chat(body.scope, list(body.messages), body.lang)

    @app.post(
        "/api/rag/ingest",
        status_code=200,
        responses={200: {"model": "RagIngestResponse"}},
    )
    def rag_ingest(body: RagIngestRequest) -> RagIngestResponse:
        """Chunk, embed and upsert documents under ``body.source_dir`` (or the
        default ``rag_source``) into ``rag_chunks``; returns ``{files, chunks,
        embedded, upserted}``. Idempotent by ``(source_file, content_hash)``."""
        source_dir = body.source_dir or "rag_source"
        result = ingest(source_dir)
        return RagIngestResponse(
            files=result.files,
            chunks=result.chunks,
            embedded=result.embedded,
            upserted=result.upserted,
        )

    @app.get(
        "/api/taxonomy/products",
        status_code=200,
        responses={200: {"model": "TaxonomyProductsResult"}},
    )
    def taxonomy_products(category: str = Query(...)) -> TaxonomyProductsResult:
        """Products for ``category`` (WebUI cascade) — config order, empty if unknown.

        The WebUI swaps these into the ``product`` <select> when the category changes
        (WEBUI.md §2); ``category`` is a required query parameter.
        """
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
        "/api/taxonomy/categories",
        status_code=200,
        responses={200: {"model": "TaxonomyCategoriesResult"}},
    )
    def taxonomy_categories() -> TaxonomyCategoriesResult:
        """All top-level categories (WebUI cascade) — config order.

        The WebUI fetches these on page load to populate the ``category`` <select>
        before the product cascade begins (WEBUI.md §2).
        """
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
        "/api/taxonomy/articles",
        status_code=200,
        responses={200: {"model": "TaxonomyArticlesResult"}},
    )
    def taxonomy_articles(
        category: str = Query(...), product: str = Query(...)
    ) -> TaxonomyArticlesResult:
        """Article numbers for ``product`` (WebUI cascade) — empty if unknown.

        The WebUI swaps these into the ``article_number`` <select> when the product
        changes (WEBUI.md §2); both ``category`` and ``product`` are required.
        """
        article_numbers = article_numbers_for_product(category, product)
        article_label = _product_label(category, product)
        return TaxonomyArticlesResult(
            items=[
                TaxonomyItem(id=art, label_sv=article_label, label_en=article_label)
                for art in article_numbers
            ]
        )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def _health() -> dict:
    """Probe every dependency and assemble the health body.

    ``db`` / ``ollama`` are ``"ok"`` when reachable, otherwise a short reason
    string; ``status`` is ``"degraded"`` when any dependency is down. The model
    names come from settings (they always resolve).
    """
    db = _db_status()
    ollama = _ollama_status()
    return {
        "status": "ok" if db == "ok" and ollama == "ok" else "degraded",
        "db": db,
        "ollama": ollama,
        "llm_model": settings.ollama.llm_model,
        "embed_model": settings.ollama.embed_model,
        "ts": _now_iso(),
    }


def _db_status() -> str:
    """``"ok"`` if Postgres answers ``SELECT 1``, otherwise a short reason."""
    try:
        conn = psycopg.connect(settings.db.dsn, connect_timeout=5)
        try:
            conn.execute("SELECT 1")
            conn.commit()
        finally:
            conn.close()
        return "ok"
    except Exception as exc:  # noqa: BLE001 - surfaced into the envelope body
        logger.warning("health: database check failed: %s", exc)
        return f"down: {exc}"


def _ollama_status() -> str:
    """``"ok"`` if the Ollama server answers ``/api/version``, else a reason.

    ``/api/version`` needs no model loaded, so it reports reachability without
    depending on the model registry. The probe serialises under ``OLLAMA_LOCK``
    (NFR-2) so it cannot run concurrently with a chat turn.
    """
    with OLLAMA_LOCK:
        try:
            resp = _HEALTH_HTTP.get(
                f"{settings.ollama.base_url}/api/version", timeout=5
            )
        except Exception as exc:  # noqa: BLE001 - surfaced below
            return f"down: {exc}"
    if 200 <= resp.status_code < 300:
        return "ok"
    return f"down: status {resp.status_code}"


def _now_iso() -> str:
    """Current UTC time as an RFC 3339 timestamp for the health body."""
    return datetime.now(timezone.utc).isoformat()


def _product_label(category: str, product: str) -> str:
    """Return the chosen-language label for ``product`` (WEBUI §3.2).

    The article <select> has no per-article label of its own, so every option in a
    product's group inherits the product's label; it is neutral when unknown.
    """
    match = (
        item
        for item in products_for_category(category)
        if item.id == product
    )
    for candidate in match:
        return candidate.label_en
    return product


# --------------------------------------------------------------------------- #
# Error envelope
# --------------------------------------------------------------------------- #
def _envelope(status: int, code: str, message: str) -> JSONResponse:
    """Build a §3.2 envelope response with the given status, code and message."""
    body = jsonable_encoder({"error": {"code": code, "message": message}})
    return JSONResponse(status_code=status, content=body)


def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Map a Pydantic / body validation failure to ``422 validation``.

    Reports the first offending field so the client gets one actionable message.
    """
    first = exc.errors()[0]
    field = ".".join(str(loc) for loc in first["loc"]) or "body"
    return _envelope(422, "validation", f"{field}: {first['msg']}")


def _on_invalid_taxonomy(
    request: Request, exc: InvalidTaxonomyError
) -> JSONResponse:
    """Map an out-of-vocabulary taxonomy triple to ``422 invalid_taxonomy``."""
    return _envelope(422, "invalid_taxonomy", str(exc))


def _on_duplicate(request: Request, exc: DuplicateError) -> JSONResponse:
    """Map a duplicate record to ``409 duplicate`` plus the existing record."""
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


def _on_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    """Map a missing record to ``404 not_found``."""
    return _envelope(404, "not_found", str(exc))


def _on_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """Map framework HTTP errors (unknown route, bad method, ...) to the envelope."""
    return _envelope(exc.status_code, _code_for_status(exc.status_code), _detail(exc))


def _on_unexpected(request: Request, exc: BaseException) -> JSONResponse:
    """Last-resort handler: anything uncaught becomes ``500 internal``.

    The traceback is logged; the client receives a neutral envelope.
    """
    logger.exception("unhandled error at %s %s", request.method, request.url.path)
    return _envelope(500, "internal", "internal error")


def _detail(exc: HTTPException) -> str:
    """Return a short, human-readable HTTP exception detail."""
    detail = exc.detail
    return detail if isinstance(detail, str) else "request failed"


def _code_for_status(status: int) -> str:
    """Map an HTTP status code onto the §3.2 envelope vocabulary."""
    if status == 404:
        return "not_found"
    if status == 409:
        return "duplicate"
    if status in (400, 422):
        return "validation"
    if status >= 500:
        return "internal"
    return "error"


# --------------------------------------------------------------------------- #
# Module-level application (``uvicorn app.api:app``)
# --------------------------------------------------------------------------- #
app = create_app()
