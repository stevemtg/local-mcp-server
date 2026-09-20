"""Central configuration for the Codebase Semantic Index MCP server."""

from __future__ import annotations

import os

# --- Ollama / embedding settings -------------------------------------------
OLLAMA_URL: str = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_ENDPOINT: str = "/api/embeddings"
EMBED_MODEL: str = os.environ.get("EMBED_MODEL", "nomic-embed-text")
EMBED_TIMEOUT_SECONDS: float = float(os.environ.get("EMBED_TIMEOUT_SECONDS", "60"))
EMBED_MAX_RETRIES: int = int(os.environ.get("EMBED_MAX_RETRIES", "3"))
EMBED_CONCURRENCY: int = int(os.environ.get("EMBED_CONCURRENCY", "4"))

# --- Index storage ------------------------------------------------------------
# Name of the folder created INSIDE each indexed project that holds its state.
INDEX_DIR_NAME: str = ".semantic_index"
DB_FILE_NAME: str = "index.db"

# --- Crawler settings ---------------------------------------------------------
# Directories never descended into (matched by directory name, any depth).
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "dist",
        "build",
        "out",
        "target",
        "venv",
        ".venv",
        "env",
        ".idea",
        ".vscode",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        "Library",  # Unity-generated asset/import cache
        "coverage",
        ".tox",
        ".eggs",
        INDEX_DIR_NAME,
    }
)

# File extensions considered "source code" for indexing purposes.
INDEXED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".hpp",
        ".cs",
    }
)

# Files larger than this are skipped (guards against minified/vendored blobs).
MAX_FILE_SIZE_BYTES: int = int(os.environ.get("MAX_FILE_SIZE_BYTES", str(300 * 1024)))

# --- Chunking settings ---------------------------------------------------------
# Bound source chunks by both lines and characters. The character budget is a
# conservative heuristic for dense code, not an exact model token count; the
# embedding metadata header is added separately.
MAX_CHUNK_LINES: int = int(os.environ.get("MAX_CHUNK_LINES", "180"))
MAX_CHUNK_CHARS: int = int(os.environ.get("MAX_CHUNK_CHARS", "4000"))
if MAX_CHUNK_LINES < 1 or MAX_CHUNK_CHARS < 1:
    raise ValueError("MAX_CHUNK_LINES and MAX_CHUNK_CHARS must be positive")
# Fallback line-window chunking for files with no parseable structure blocks.
FALLBACK_CHUNK_LINES: int = 80
FALLBACK_CHUNK_OVERLAP: int = 10

# --- Retrieval settings ----------------------------------------------------------
# Default number of results returned by semantic_search / semantic_search_similar.
DEFAULT_SEARCH_LIMIT: int = int(os.environ.get("DEFAULT_SEARCH_LIMIT", "8"))
# Bound tool responses and the work spent diversifying search results.
MAX_SEARCH_LIMIT: int = 50
# MMR (maximal marginal relevance) diversity knob: 1.0 = pure relevance, 0.0 = pure diversity.
MMR_LAMBDA: float = float(os.environ.get("MMR_LAMBDA", "0.5"))
# MMR re-ranks this many raw top hits before selecting the final limit results.
MMR_POOL_FACTOR: int = int(os.environ.get("MMR_POOL_FACTOR", "6"))
# Semantic snippets are truncated to this many lines; full source stays authoritative.
SNIPPET_MAX_LINES: int = int(os.environ.get("SNIPPET_MAX_LINES", "24"))
# --- get_symbol_context settings ---------------------------------------------
# Default cap on lexical references returned by get_symbol_context.
DEFAULT_MAX_REFERENCES: int = int(os.environ.get("DEFAULT_MAX_REFERENCES", "12"))
# Hard ceiling for the caller-supplied max_references argument.
MAX_REFERENCES_CAP: int = int(os.environ.get("MAX_REFERENCES_CAP", "50"))
# Context snippets/window chunks are capped to this many lines.
CONTEXT_SNIPPET_MAX_LINES: int = int(os.environ.get("CONTEXT_SNIPPET_MAX_LINES", "40"))
# Cap on import/include/use statements listed per file.
CONTEXT_IMPORTS_MAX: int = int(os.environ.get("CONTEXT_IMPORTS_MAX", "20"))
# Reference previews and declaration headers are capped at this many chars.
CONTEXT_PREVIEW_MAX_CHARS: int = int(os.environ.get("CONTEXT_PREVIEW_MAX_CHARS", "240"))

# Max query parts (chunks) embedded per semantic_search_similar call. The
# queried source is split with the same chunker as indexing so every part
# fits the embedding model's context window; sources yielding more parts than
# this cap use only the first N.
SIMILAR_QUERY_MAX_PARTS: int = int(os.environ.get("SIMILAR_QUERY_MAX_PARTS", "12"))
