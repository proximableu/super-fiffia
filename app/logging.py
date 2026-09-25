"""Structured JSON logging for the F&S knowledge base (NFR-8).

The application logs with :mod:`logging`, but by default the logging system
formats records as ``levelname -- message`` — fine for debugging, but not
machine-consumable. NFR-8 asks for *structured* logging so an agent turn can be
replayed from the log stream: action taken, query issued, filters applied,
selected source ids and their fused scores, turn index and per-stage latency.

:func:`setup` installs a single :class:`JsonFormatter` on the root logger (once,
from :func:`app.api.create_app` and from the test suite). Every
:class:`logging.LogRecord` is then rendered as one JSON line with a stable
schema:

    {
      "ts":         "<RFC-3339 UTC>",
      "levelname":  "INFO",
      "logger":     "app.agent",
      "message":    "agent turn 1: action=search_records ...",
      "stage":      "retrieval" | "agent" | "app",
      "turn":       1,
      "action":     "search_records",
      "query":      "...",
      "filters":    {"category": "electric"},
      "source_ids": ["...", ...],
      "scores":     [0.95, ...],
      "latency_ms": 12.4,
      "request_id": "..."
    }

Call sites attach any extra structured fields as keyword arguments::

    logger.info("retrieval", turn=2, action="search_records",
                source_ids=[str(h.id) for h in hits],
                scores=[h.score for h in hits], latency_ms=1.2)

Unknown keyword arguments are stashed onto the :class:`logging.LogRecord`
(see :class:`_StructuredLogger`) and emitted verbatim by :class:`JsonFormatter`.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

__all__ = ["setup", "JsonFormatter", "LogContext"]

_CONFIGURED = False

# Fields already present on every :class:`logging.LogRecord` that must not be
# treated as structured data and echoed into the payload.
_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


class JsonFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "levelname": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Structured fields: anything a call site attached that is not already a
        # standard LogRecord attribute is emitted verbatim.
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        # ``request_id`` is attached per-request by the app middleware.
        if (req_id := getattr(record, "request_id", None)) is not None:
            payload["request_id"] = req_id
        return json.dumps(payload, default=str)


class LogContext:
    """Attach per-call metadata (request id, turn index, latency, ...) to logs.

    Usage::

        with LogContext(turn=1, action="search_records", request_id="abc"):
            logger.info("retrieval", source_ids=[...], scores=[...], latency_ms=1.2)

    Every :class:`LogRecord` emitted while the context is active carries the
    context's fields merged into the structured payload.
    """

    def __init__(self, **fields: Any) -> None:
        self._fields = {k: v for k, v in fields.items() if v is not None}

    def __enter__(self) -> LogContext:
        _context_stack.append(self._fields)
        return self

    def __exit__(self, *exc: object) -> None:
        if _context_stack:
            _context_stack.pop()


_context_stack: list[dict[str, Any]] = []


class _StructuredLogger(logging.Logger):
    """A :class:`logging.Logger` subclass that stashes extra kwargs on the record.

    Plain :class:`logging.Logger._log` rejects unknown keyword arguments. This
    variant collects them onto the record's ``__dict__`` so :class:`JsonFormatter`
    can emit them, and merges any fields from an active :class:`LogContext`.
    """

    def _log(  # type: ignore[override]
        self,
        level: int,
        msg: str,
        args: tuple[Any, ...],
        exc_info: Any = None,
        extra: Any = None,
        stack_info: bool = False,
        stacklevel: int = 1,
        **kwargs: Any,
    ) -> None:
        # Fold the free-form structured fields (turn, action, source_ids, ...)
        # plus the context bundle into the record so JsonFormatter can emit
        # them verbatim.
        merged: dict[str, Any] = {}
        if _context_stack:
            merged.update(_context_stack[-1])
        merged.update(kwargs)
        if extra:
            merged.update(extra)
        record = self.makeRecord(
            self.name,
            level,
            __file__,  # runtime path of this module
            0,
            msg if isinstance(msg, str) else self.formatMsg(msg, args),
            args,
            exc_info,
            func="record",
            extra=merged,
            sinfo=None,  # stack text: not needed for JSON records
        )
        self.handle(record)


def setup() -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: safe to call from :func:`app.api.create_app` and from tests.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    logging.setLoggerClass(_StructuredLogger)
    formatter = JsonFormatter()
    root = logging.getLogger()
    # A ``basicConfig`` somewhere up the import chain may already have raised the
    # level to WARNING; always pin it to INFO so application records emit.
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.addHandler(handler)
    _CONFIGURED = True


def reset() -> None:
    """Forget the module-level "configured" flag (test teardown)."""
    global _CONFIGURED
    _CONFIGURED = False
