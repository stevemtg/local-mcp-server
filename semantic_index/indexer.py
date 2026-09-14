"""Indexing orchestration: crawl -> chunk -> embed -> store, with change detection."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .chunker import chunk_code, embed_text
from .config import (
    DEFAULT_SEARCH_LIMIT,
    EMBED_MODEL,
    SIMILAR_QUERY_MAX_PARTS,
    SNIPPET_MAX_LINES,
)
from .crawler import classify_language, hash_file, iter_source_files, read_text
from .embedder import OllamaEmbedder
from .store import StoredChunk, VectorStore

logger = logging.getLogger("semantic_index.indexer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Indexer:
    """Coordinates the crawler, chunker, embedder and vector store."""

    def __init__(self, embedder: OllamaEmbedder | None = None) -> None:
        self.embedder = embedder or OllamaEmbedder()

    def index_directory(self, directory_path: str) -> dict:
        """(Re-)embed every new/changed file under ``directory_path``."""
        root = Path(directory_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Not a directory: {directory_path}")

        store = VectorStore(root)
        try:
            stored_model = store.get_meta("embedding_model")
            force_full = stored_model is not None and stored_model != self.embedder.model
            if force_full:
                logger.warning(
                    "Embedding model changed (%s -> %s); re-embedding all files",
                    stored_model,
                    self.embedder.model,
                )
                known: dict[str, str] = {}
            else:
                known = store.indexed_files()

            if store.get_meta("created_at") is None:
                store.set_meta("created_at", _now())
            store.set_meta("embedding_model", self.embedder.model)

            crawled = iter_source_files(root)
            current_paths = {cf.rel_path for cf in crawled}

            embedded = skipped = failed = 0
            dim = 0
            for cf in crawled:
                try:
                    content_hash = hash_file(cf.path)
                    if not force_full and known.get(cf.rel_path) == content_hash:
                        skipped += 1
                        continue
                    text = read_text(cf.path)
                    code_chunks = chunk_code(text, cf.language)
                    if not code_chunks:
                        continue
                    texts = [embed_text(c, cf.rel_path, cf.language) for c in code_chunks]
                    vectors = self.embedder.embed_many(texts)
                    dim = len(vectors[0]) if vectors else 0
                    store.replace_file(
                        cf.rel_path,
                        content_hash,
                        cf.mtime,
                        cf.size,
                        cf.language,
                        [
                            StoredChunk(
                                file_path=cf.rel_path,
                                start_line=c.start_line,
                                end_line=c.end_line,
                                language=cf.language,
                                chunk_type=c.kind,
                                name=c.name,
                                content=c.content,
                            )
                            for c in code_chunks
                        ],
                        vectors,
                    )
                    embedded += 1
                except Exception as exc:  # noqa: BLE001 - skip & log, never crash
                    logger.warning("Failed to index %s: %s", cf.path, exc)
                    failed += 1

            # Drop state for files that vanished (or became non-indexable).
            removed = 0
            for rel in set(known) - current_paths:
                store.remove_file(rel)
                removed += 1

            if dim:
                store.set_meta("embedding_dim", dim)
            store.touch_indexed_at()

            return {
                "directory": str(root),
                "files_embedded": embedded,
                "files_unchanged_skipped": skipped,
                "files_removed": removed,
                "files_failed": failed,
                "total_chunks": store.count_chunks(),
                "full_reindex": force_full,
                "indexed_at": store.get_meta("last_indexed_at"),
            }
        finally:
            store.close()

    def semantic_search(
        self,
        directory_path: str,
        query: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
        path_filter: str | None = None,
        min_score: float | None = None,
    ) -> dict:
        """Embed ``query`` and return agent-friendly candidate locations.

        Results are CANDIDATES ONLY — the live source files are authoritative.
        """
        root = Path(directory_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Not a directory: {directory_path}")
        store = VectorStore(root)
        try:
            if store.count_chunks() == 0:
                return {
                    "query": query,
                    "directory": str(root),
                    "results": [],
                    "note": "Index is empty — call index_codebase(directory_path) first.",
                }
            query_vector = self.embedder.embed_one(query)
            hits = store.search(
                query_vector, max(1, limit), path_filter=path_filter, min_score=min_score
            )
            return {
                "query": query,
                "directory": str(root),
                "note": "Candidates only. Read the current file before concluding or editing; the index may be stale.",
                "results": [self._as_result(root, i, c) for i, c in enumerate(hits, 1)],
            }
        finally:
            store.close()

    def similar(
        self,
        directory_path: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> dict:
        """Find chunks similar to the CURRENT source at ``path``[:lines].

        The queried source is split with the same structure-aware chunker
        used for indexing, every part is embedded in its own Ollama request
        (so large files can never exceed the embedding model's context
        window), and the per-part results are merged keeping the best score
        per chunk. The live file on disk stays authoritative. Chunks
        overlapping the queried range are excluded from results.
        """
        root = Path(directory_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Not a directory: {directory_path}")
        target = (root / path).resolve()
        if root not in target.parents and target != root:
            raise ValueError(f"Path escapes project root: {path}")
        if not target.is_file():
            raise ValueError(f"File not found (index may be stale): {path}")
        store = VectorStore(root)
        try:
            if store.count_chunks() == 0:
                return {
                    "path": path,
                    "directory": str(root),
                    "results": [],
                    "note": "Index is empty — call index_codebase(directory_path) first.",
                }
            text = read_text(target)
            lines = text.splitlines()
            rel = target.relative_to(root).as_posix()
            language = classify_language(target)
            if start_line is not None:
                s = max(1, start_line)
                e = end_line or len(lines)
                text = "\n".join(lines[s - 1 : e])
                offset = s - 1
                exclude: list[tuple[str, int, int]] = [(rel, s, e)]
            else:
                offset = 0
                exclude = [(rel, 1, len(lines))]
            # Chunk the query source like at index time and embed each part
            # separately: a single whole-file request would exceed the
            # embedding model's context window for larger files.
            parts = chunk_code(text, language)
            if len(parts) > SIMILAR_QUERY_MAX_PARTS:
                logger.warning(
                    "Query source %s yields %d parts; capping at %d",
                    rel,
                    len(parts),
                    SIMILAR_QUERY_MAX_PARTS,
                )
                parts = parts[:SIMILAR_QUERY_MAX_PARTS]
            vectors = self.embedder.embed_many(
                [
                    embed_text(
                        replace(
                            p,
                            start_line=p.start_line + offset,
                            end_line=p.end_line + offset,
                        ),
                        rel,
                        language,
                    )
                    for p in parts
                ]
            )
            merged: dict[tuple[str, int, int], StoredChunk] = {}
            for query_vector in vectors:
                for hit in store.search(query_vector, max(1, limit), exclude=exclude):
                    key = (hit.file_path, hit.start_line, hit.end_line)
                    prev = merged.get(key)
                    if prev is None or hit.score > prev.score:
                        merged[key] = hit
            hits = sorted(
                merged.values(), key=lambda c: c.score, reverse=True
            )[: max(1, limit)]
            return {
                "path": path,
                "range": [start_line, end_line] if start_line else None,
                "directory": str(root),
                "note": "Candidates only. Read the current files before concluding or editing; the index may be stale.",
                "results": [self._as_result(root, i, c) for i, c in enumerate(hits, 1)],
            }
        finally:
            store.close()

    @staticmethod
    def _as_result(root: Path, rank: int, c: StoredChunk) -> dict:
        """Shape a hit into the compact agent-facing result schema."""
        lines = c.content.splitlines()
        truncated = len(lines) > SNIPPET_MAX_LINES
        snippet = "\n".join(lines[:SNIPPET_MAX_LINES])
        if truncated:
            hidden = len(lines) - SNIPPET_MAX_LINES
            snippet += (
                f"\n… [{hidden} more lines — read {c.file_path}:"
                f"{c.start_line + SNIPPET_MAX_LINES}-{c.end_line} for the rest]"
            )
        symbol = c.name or Path(c.file_path).stem
        reason = (
            f"Rank {rank}: semantic (cosine) match to the query — {c.language} "
            f"{c.chunk_type} '{symbol}' at {c.file_path}:{c.start_line}-{c.end_line}. "
            f"Verify against the current source before use."
        )
        return {
            "path": c.file_path,
            "absolutePath": str(root / c.file_path),
            "startLine": c.start_line,
            "endLine": c.end_line,
            "symbol": symbol,
            "score": round(c.score, 4),
            "snippet": snippet,
            "reason": reason,
        }

    def status(self, directory_path: str) -> dict:
        root = Path(directory_path).expanduser().resolve()
        index_dir = root / ".semantic_index"
        if not index_dir.is_dir():
            return {
                "directory": str(root),
                "indexed": False,
                "num_files": 0,
                "num_chunks": 0,
                "embedding_model": EMBED_MODEL,
                "embedding_dim": None,
                "created_at": None,
                "last_indexed_at": None,
            }
        store = VectorStore(root)
        try:
            stats = store.stats()
            stats["directory"] = str(root)
            return stats
        finally:
            store.close()

