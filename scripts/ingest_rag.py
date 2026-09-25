#!/usr/bin/env python3
"""Ingest markdown/text documents into :class:`rag_chunks`.

Walks a source directory, chunks each document, embeds the new chunks, and
upserts them into ``rag_chunks`` (idempotent by ``(source_file, content_hash)``).
"""

from __future__ import annotations

import argparse
import os
from dataclasses import replace

from app import db
from app.config import settings
from app.rag import ingest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the RAG ingestion CLI."""
    parser = argparse.ArgumentParser(
        description="Chunk and embed documents under a source directory into rag_chunks."
    )
    parser.add_argument("--source", required=True, help="Directory of markdown/text documents.")
    parser.add_argument(
        "--chunk-chars", type=int, default=2000, help="Target chunk size in characters."
    )
    parser.add_argument("--overlap", type=int, default=200, help="Overlap between chunks.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Count work without writing or embedding."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run ingestion and print a tally.

    Honors ``TEST_DATABASE_DSN`` (pointing at the local trust-auth test cluster
    on port 5999) before opening the pool, so the CLI runs against the test
    database without touching the shared system Postgres on port 5432.
    """
    args = parse_args(argv)
    test_dsn = os.environ.get("TEST_DATABASE_DSN")
    if test_dsn:
        settings.db = replace(settings.db, dsn=test_dsn)
    db.get_pool()
    result = ingest(
        args.source,
        chunk_chars=args.chunk_chars,
        overlap=args.overlap,
        dry_run=args.dry_run,
    )
    print(
        f"files={result.files} chunks={result.chunks} "
        f"embedded={result.embedded} upserted={result.upserted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
