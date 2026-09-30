#!/usr/bin/env python3
"""Run super-fiffia as an MCP server.

Exposes the F&S knowledge base as Model Context Protocol tools so MCP clients
(Claude Code, Cursor, Windsurf, …) can drive the same retrieval / record / chat
pipeline the WebUI (:9001) and API (:9000) use — on its own port (default 9002),
leaving the WebUI and API processes untouched.

    uvicorn scripts.run_mcp:app --port 9002

``app.mcp.http_app`` already returns a self-contained Starlette app whose
lifespan is the FastMCP session manager; this runner adds a `/api/health`
probe on top of it.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from app.mcp import mcp
from app.api import _health


def _build_app():
    """Return the FastMCP app with a `/api/health` liveness probe added."""
    app = mcp.http_app(path="/")
    app.add_route("/api/health", _health, methods=["GET"])
    return app


app = _build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("scripts.run_mcp:app", host="0.0.0.0", port=9002)
