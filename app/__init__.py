"""F&S knowledge base application package."""

from __future__ import annotations

import logging as _logging

# Every logger created anywhere in this package (including the module-level
# ``logger`` globals defined at import time) must accept the free-form structured
# keyword arguments that the NFR-8 call sites pass (turn, action, source_ids,
# latency_ms, ...). ``logging.Logger`` rejects unknown kwargs, so switch the
# logger class up front and pin the root to JSON output on first import.
from app.logging import _StructuredLogger, setup  # noqa: E402

_logging.setLoggerClass(_StructuredLogger)
setup()  # install the JSON formatter on the root logger (idempotent)
