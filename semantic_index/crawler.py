"""Recursive source-file crawler with change detection helpers."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .config import IGNORED_DIRS, INDEXED_EXTENSIONS, MAX_FILE_SIZE_BYTES

logger = logging.getLogger("semantic_index.crawler")


@dataclass(frozen=True)
class CrawledFile:
    """A source file discovered by the crawler."""

    path: Path  # absolute path
    rel_path: str  # path relative to the indexed root (used as DB key)
    size: int
    mtime: float
    language: str


def classify_language(path: Path) -> str:
    """Map a file extension onto a language identifier used by the chunker."""
    ext = path.suffix.lower()
    return {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".kt": "kotlin",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".hpp": "cpp",
        ".cs": "csharp",
    }.get(ext, "unknown")


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def read_text(path: Path) -> str:
    """Read a source file as text, replacing undecodable bytes instead of failing."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def iter_source_files(root: Path) -> list[CrawledFile]:
    """Walk ``root`` recursively, yielding every indexable source file.

    Ignored directories (``node_modules``, ``.git``, ...) are pruned during
    the walk, and unreadable/in oversized files are skipped with a log line
    rather than raising.
    """
    results: list[CrawledFile] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune ignored/hidden directories in-place so os.walk skips them.
        dirnames[:] = [
            d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() not in INDEXED_EXTENSIONS:
                continue
            try:
                stat = path.stat()
                if stat.st_size > MAX_FILE_SIZE_BYTES:
                    logger.warning("Skipping oversized file: %s", path)
                    continue
                results.append(
                    CrawledFile(
                        path=path,
                        rel_path=path.relative_to(root).as_posix(),
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        language=classify_language(path),
                    )
                )
            except OSError as exc:
                logger.warning("Skipping unreadable file %s: %s", path, exc)
    return results
