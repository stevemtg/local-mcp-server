"""SQLite-backed local vector store (one database per indexed project)."""

from __future__ import annotations

import fnmatch
import json
import sqlite3
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_FILE_NAME, INDEX_DIR_NAME, MMR_LAMBDA, MMR_POOL_FACTOR


@dataclass
class StoredChunk:
    file_path: str
    start_line: int
    end_line: int
    language: str
    chunk_type: str
    name: str
    content: str
    score: float = 0.0
    embedding: list[float] | None = field(default=None, repr=False, compare=False)


def path_filter_matches(rel_path: str, pattern: str) -> bool:
    """True when the relative path (or any of its segments) matches ``pattern``.

    Accepts glob patterns like ``src/auth/*.py`` and bare directory/file
    segments like ``auth`` or ``models.py``.
    """
    if fnmatch.fnmatch(rel_path, pattern):
        return True
    stem = Path(rel_path).name
    if fnmatch.fnmatch(stem, pattern):
        return True
    # Bare segment match: 'auth' matches 'src/auth/store.py'
    return any(fnmatch.fnmatch(part, pattern) for part in Path(rel_path).parts)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    mtime        REAL NOT NULL,
    size         INTEGER NOT NULL,
    language     TEXT NOT NULL,
    num_chunks   INTEGER NOT NULL DEFAULT 0,
    indexed_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path  TEXT NOT NULL,
    chunk_type TEXT NOT NULL,
    name       TEXT NOT NULL,
    language   TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    content    TEXT NOT NULL,
    embedding  BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);
"""


def cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / ((norm_a**0.5) * (norm_b**0.5))


def read_only_indexed_hash(project_root: Path, rel_path: str) -> str | None:
    """Read-only lookup of a file's stored content hash (never writes).

    Checks for the database file BEFORE connecting, opens SQLite in read-only
    URI mode, and returns ``None`` when the database is missing, the schema
    table is missing, or no record exists — callers map ``None`` to
    ``not_indexed``. Never creates a database, directory, schema, or WAL file.
    """
    db_path = project_root / INDEX_DIR_NAME / DB_FILE_NAME
    if not db_path.is_file():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT content_hash FROM files WHERE path = ?", (rel_path,)
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()


class VectorStore:
    """File-based vector index stored at ``<root>/.semantic_index/index.db``."""

    def __init__(self, project_root: Path) -> None:
        self.index_dir = project_root / INDEX_DIR_NAME
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.index_dir / DB_FILE_NAME
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    # -- meta -----------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: object) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self._conn.commit()

    def touch_indexed_at(self) -> None:
        self.set_meta(
            "last_indexed_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
        )

    # -- files ----------------------------------------------------------------
    def get_file_hash(self, rel_path: str) -> str | None:
        row = self._conn.execute(
            "SELECT content_hash FROM files WHERE path = ?", (rel_path,)
        ).fetchone()
        return row[0] if row else None

    def indexed_files(self) -> dict[str, str]:
        """Map of rel_path -> content_hash for everything already indexed."""
        return {
            row[0]: row[1]
            for row in self._conn.execute("SELECT path, content_hash FROM files").fetchall()
        }

    def replace_file(
        self,
        rel_path: str,
        content_hash: str,
        mtime: float,
        size: int,
        language: str,
        chunks: list[StoredChunk],
        vectors: list[list[float]],
    ) -> None:
        """Atomically replace all chunks for one file and upsert its record."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
            self._conn.execute(
                "INSERT INTO files(path, content_hash, mtime, size, language, num_chunks, indexed_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET content_hash=excluded.content_hash, "
                "mtime=excluded.mtime, size=excluded.size, language=excluded.language, "
                "num_chunks=excluded.num_chunks, indexed_at=excluded.indexed_at",
                (rel_path, content_hash, mtime, size, language, len(chunks), now),
            )
            rows = [
                (
                    c.file_path,
                    c.chunk_type,
                    c.name,
                    c.language,
                    c.start_line,
                    c.end_line,
                    c.content,
                    array("f", vector).tobytes(),
                )
                for c, vector in zip(chunks, vectors)
            ]
            self._conn.executemany(
                "INSERT INTO chunks(file_path, chunk_type, name, language, start_line, end_line, content, embedding) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def remove_file(self, rel_path: str) -> None:
        self._conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
        self._conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))
        self._conn.commit()

    # -- queries ----------------------------------------------------------------
    def count_files(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def count_chunks(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def search(
        self,
        query_vector: list[float],
        limit: int,
        path_filter: str | None = None,
        min_score: float | None = None,
        exclude: list[tuple[str, int, int]] | None = None,
        mmr_lambda: float = MMR_LAMBDA,
    ) -> list[StoredChunk]:
        """Cosine similarity + MMR re-ranking over stored chunks.

        1. Score every chunk by cosine similarity to ``query_vector``
           (optionally filtered by fnmatch ``path_filter`` on the relative
           path and by ``min_score``).
        2. Keep the raw top ``limit * MMR_POOL_FACTOR`` candidates.
        3. Greedily select ``limit`` results with Maximal Marginal
           Relevance so near-duplicate chunks don't crowd out diverse
           candidate locations.
        """

        def overlaps(c: StoredChunk) -> bool:
            for e_path, e_start, e_end in exclude or []:
                if (
                    c.file_path == e_path
                    and c.start_line <= e_end
                    and c.end_line >= e_start
                ):
                    return True
            return False

        scored: list[StoredChunk] = []
        cursor = self._conn.execute(
            "SELECT file_path, start_line, end_line, language, chunk_type, name, content, embedding "
            "FROM chunks"
        )
        for file_path, start_line, end_line, language, chunk_type, name, content, blob in cursor:
            if path_filter and not path_filter_matches(file_path, path_filter):
                continue
            vector = array("f")
            vector.frombytes(bytes(blob))
            chunk = StoredChunk(
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                language=language,
                chunk_type=chunk_type,
                name=name,
                content=content,
                score=cosine(query_vector, list(vector)),
            )
            if exclude and overlaps(chunk):
                continue
            if min_score is not None and chunk.score < min_score:
                continue
            chunk.embedding = list(vector)
            scored.append(chunk)

        scored.sort(key=lambda c: c.score, reverse=True)
        pool = scored[: max(limit * MMR_POOL_FACTOR, 40)]

        # Greedy MMR selection: maximize lambda*relevance - (1-lambda)*maxSim(chosen).
        selected: list[StoredChunk] = []
        remaining = list(pool)
        while remaining and len(selected) < limit:
            best_i, best_val = 0, float("-inf")
            for i, cand in enumerate(remaining):
                penalty = 0.0
                for chosen in selected:
                    sim = cosine(cand.embedding, chosen.embedding)
                    if sim > penalty:
                        penalty = sim
                value = mmr_lambda * cand.score - (1.0 - mmr_lambda) * penalty
                if value > best_val:
                    best_val, best_i = value, i
            selected.append(remaining.pop(best_i))
        return selected

    def stats(self) -> dict:
        return {
            "indexed": self.count_files() > 0,
            "num_files": self.count_files(),
            "num_chunks": self.count_chunks(),
            "embedding_model": self.get_meta("embedding_model"),
            "embedding_dim": json.loads(self.get_meta("embedding_dim") or "null"),
            "created_at": self.get_meta("created_at"),
            "last_indexed_at": self.get_meta("last_indexed_at"),
        }

