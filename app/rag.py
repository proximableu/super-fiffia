"""Chunking and idempotent ingestion of documents into ``rag_chunks``.

Documents are split into overlapping text blocks by heading and paragraph, and
each block is embedded and upserted into :class:`rag_chunks` keyed on a content
hash so that re-ingesting an unchanged document re-embeds nothing.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path

import psycopg

from app.db import _checkout, _release
from app.embedding import embed
from app.records_repo import _as_vector

# Document extensions ingested when walking a source directory.
_INGEST_EXTS = frozenset({".md", ".txt"})

# A Markdown/GFM heading: ``#``s at line start, then the heading text (the rest
# of the line). Markdown headings never carry inline content, so nothing after
# the marker belongs to the header.
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")


class RagIngestResult:
    """Outcome of an :func:`ingest` call (CONTRACT.md §6).

    Attributes:
        files: number of documents read.
        chunks: total chunks produced across all documents.
        embedded: newly-embedded chunks (those not already stored).
        upserted: rows written or updated in ``rag_chunks``.
    """

    def __init__(self, files: int, chunks: int, embedded: int, upserted: int) -> None:
        self.files = files
        self.chunks = chunks
        self.embedded = embedded
        self.upserted = upserted

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"RagIngestResult(files={self.files}, chunks={self.chunks}, "
            f"embedded={self.embedded}, upserted={self.upserted})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RagIngestResult):
            return NotImplemented
        return (
            self.files == other.files
            and self.chunks == other.chunks
            and self.embedded == other.embedded
            and self.upserted == other.upserted
        )


def _content_hash(source_file: str, chunk_text: str) -> str:
    """Return the SHA-256 of ``(source_file, chunk_text)`` as a hex digest.

    The two fields are joined with a NUL separator so the pair is unambiguous
    regardless of where a chunk boundary falls.
    """
    payload = f"{source_file}\x00{chunk_text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def chunk(
    text: str,
    *,
    chunk_chars: int = 8000,
    overlap: int = 800,
) -> list[tuple[int, str | None, str]]:
    """Split ``text`` into ``(chunk_index, section_header, chunk_text)`` triples.

    Text is grouped by the most recent Markdown heading; each block's paragraphs
    are then accumulated into chunks up to ``chunk_chars`` characters. Every
    chunk except the last begins by re-using ``overlap`` characters from the end
    of the previous chunk so no context is lost across a boundary. A chunk never
    mixes content from two sections, and a chunk that is at least ``chunk_chars``
    long is split into successive chunk-sized pieces. The ``section_header`` is
    the heading of the block in which a chunk starts.

    Args:
        text: the raw document text.
        chunk_chars: target size of each chunk in characters (must be positive).
        overlap: characters carried from the end of one chunk into the next
            (must be in ``[0, chunk_chars)``).
        Sizes are characters, not model tokens — the project has no tokenizer
        for the embedder's BPE, so ``8000`` / ``800`` approximate a ~2k-token
        chunk with a ~1k-token overlap.

    Returns:
        An ordered list of ``(chunk_index, section_header, chunk_text)`` where
        ``chunk_index`` is the zero-based position of the chunk.

    Raises:
        ValueError: if ``chunk_chars`` is not positive or ``overlap`` is outside
            the range ``[0, chunk_chars)``.
    """
    if chunk_chars <= 0:
        raise ValueError("chunk_chars must be positive")
    if overlap < 0 or overlap >= chunk_chars:
        raise ValueError("overlap must be in the range [0, chunk_chars)")

    # Split into (header, body) blocks on heading lines. Each block's body is
    # everything up to the next heading, with runs of whitespace collapsed so
    # the block is paragraph-shaped rather than line-shaped.
    blocks: list[tuple[str | None, str]] = []
    current_header: str | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            blocks.append((current_header, "\n".join(current_body).strip()))
            current_header = heading.group(1).strip()
            current_body = []
            continue
        current_body.append(line)
    blocks.append((current_header, "\n".join(current_body).strip()))

    # Walk the blocks in order, grouping their tokens into chunks. A chunk
    # never crosses a section boundary: entering a new section flushes any
    # pending buffer before new content is buffered. Every chunk except the
    # last begins by re-using ``overlap`` characters from the end of the
    # previous chunk, and a body longer than ``chunk_chars`` is split into
    # successive chunk-sized pieces with that same overlap.
    chunks: list[tuple[int, str | None, str]] = []
    index = 0
    buffer: str = ""
    buffer_header: str | None = None

    def flush() -> None:
        """Emit the current buffer as one or more chunks and clear it."""
        nonlocal buffer, buffer_header, index
        if not buffer:
            return
        if len(buffer) >= chunk_chars:
            start = 0
            while start < len(buffer):
                end = start + chunk_chars
                chunks.append((index, buffer_header, buffer[start:end]))
                index += 1
                if end >= len(buffer):
                    break
                start = end - overlap
        else:
            chunks.append((index, buffer_header, buffer))
            index += 1
        buffer = ""
        buffer_header = None

    for header, body in blocks:
        for para in body.split():
            if buffer_header != header:
                # A new section begins: flush anything pending first.
                flush()
            if not buffer:
                buffer, buffer_header = para, header
                continue
            if len(buffer) + 1 + len(para) <= chunk_chars:
                buffer = f"{buffer} {para}"
                continue
            # Buffer is full: emit it, then start fresh with the current token.
            flush()
            if not buffer:
                buffer, buffer_header = para, header

    if buffer:
        chunks.append((index, buffer_header, buffer))

    return chunks


def _iter_sources(root: Path) -> list[Path]:
    """Return document files under ``root`` sorted by relative path."""
    sources = [
        p
        for ext in _INGEST_EXTS
        for p in root.rglob(f"*.{ext.lstrip('.')}")
        if p.is_file()
    ]
    return sorted(sources, key=lambda p: p.relative_to(root).as_posix())


def _existing_hashes(
    conn: psycopg.Connection, source_files: list[Path], root: Path
) -> set[tuple[str, str]]:
    """Return stored ``(source_file, content_hash)`` pairs for ``source_files``."""
    rel_files = sorted({p.relative_to(root).as_posix() for p in source_files})
    if not rel_files:
        return set()
    placeholders = ", ".join(f"%s" for _ in rel_files)
    query = (
        f"SELECT source_file, content_hash FROM rag_chunks WHERE source_file IN ({placeholders})"
    )
    try:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(query, rel_files)
            return {(row["source_file"], row["content_hash"]) for row in cur.fetchall()}
    finally:
        conn.rollback()


def _upsert_batch(
    conn: psycopg.Connection,
    rows: Iterable[tuple[str, int, str | None, str, str, str, str | None, str | None]],
) -> int:
    """Upsert a batch of chunks; return the number of rows written.

    Each row is ``(source_file, index, header, body, digest, embedding,
    category, product)``; ``category``/``product`` populate the new rag_chunks
    scope columns (nullable for an unscoped ingest).
    """
    rows = list(rows)
    if not rows:
        return 0
    query = """
        INSERT INTO rag_chunks (
            source_file, chunk_index, section_header, chunk_text,
            content_hash, embedding, category, product
        )
        VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s)
        ON CONFLICT (source_file, content_hash) DO UPDATE
        SET chunk_index = EXCLUDED.chunk_index,
            section_header = EXCLUDED.section_header,
            chunk_text = EXCLUDED.chunk_text,
            embedding = EXCLUDED.embedding,
            category = EXCLUDED.category,
            product = EXCLUDED.product
    """
    try:
        with conn.cursor() as cur:
            cur.executemany(query, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def ingest(
    source_dir: str | os.PathLike[str],
    *,
    chunk_chars: int = 8000,
    overlap: int = 800,
    category: str | None = None,
    product: str | None = None,
    dry_run: bool = False,
) -> RagIngestResult:
    """Chunk and embed every document under ``source_dir`` into ``rag_chunks``.

    Ingestion is idempotent: a chunk whose ``(source_file, content_hash)`` is
    already stored is skipped and never re-embedded. Rows are written with
    ``ON CONFLICT DO UPDATE`` so re-ingesting an existing chunk updates it in
    place without duplicating it.

    The optional ``category``/``product`` populate the new rag_chunks scope
    columns so retrieval (see :func:`app.retrieval.retrieve_rag`) can filter RAG
    candidates to those tagged for the product being discussed. An unscoped
    ingest leaves the columns NULL, which retrieval treats as "matches anything".

    Args:
        source_dir: path to the directory of markdown/text documents.
        chunk_chars: target chunk size (characters) passed to :func:`chunk`.
        overlap: overlap (characters) between chunks passed to :func:`chunk`.
        category: optional product category to tag every chunk from this ingest.
        product: optional product name to tag every chunk from this ingest.
        dry_run: when true, count the work without writing or embedding.

    Returns:
        A :class:`RagIngestResult` tallying files, chunks, embedded, and
        upserted counts.

    Raises:
        FileNotFoundError: if ``source_dir`` is not a directory.
        EmbeddingError: if the embedding model is unreachable.
    """
    root = Path(source_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"source directory not found: {root}")

    sources = _iter_sources(root)

    existing: set[tuple[str, str]] = set()
    if sources and not dry_run:
        conn = _checkout()
        try:
            existing = _existing_hashes(conn, sources, root)
        finally:
            _release(conn)

    planned: list[tuple[str, int, str | None, str, str]] = []
    for path in sources:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for index, header, body in chunk(text, chunk_chars=chunk_chars, overlap=overlap):
            planned.append((rel, index, header, body, _content_hash(rel, body)))

    embedded_texts: list[str] = []
    embedding_index: dict[str, int] = {}
    for rel, index, header, body, digest in planned:
        if (rel, digest) in existing:
            continue
        embedding_index[digest] = len(embedded_texts)
        embedded_texts.append(body)

    embeddings: list[list[float]] = []
    if embedded_texts and not dry_run:
        conn = _checkout()
        try:
            embeddings = embed(embedded_texts)
        finally:
            _release(conn)

    if dry_run:
        return RagIngestResult(
            files=len(sources),
            chunks=len(planned),
            embedded=len(embedded_texts),
            upserted=0,
        )

    rows: list[tuple[str, int, str | None, str, str, str, str | None, str | None]] = []
    for rel, index, header, body, digest in planned:
        if (rel, digest) in existing:
            continue
        rows.append(
            (
                rel,
                index,
                header,
                body,
                digest,
                _as_vector(embeddings[embedding_index[digest]]),
                category,
                product,
            )
        )

    conn = _checkout()
    try:
        upserted = _upsert_batch(conn, rows)
    finally:
        _release(conn)

    return RagIngestResult(
        files=len(sources),
        chunks=len(planned),
        embedded=len(embedded_texts),
        upserted=upserted,
    )
