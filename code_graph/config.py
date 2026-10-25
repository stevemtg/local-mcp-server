"""Configuration for the structural graph layer (stdlib-only, env-overridable)."""

from __future__ import annotations

import os

# --- Graph storage -----------------------------------------------------------
# The graph snapshot is stored in the SAME folder as index.db so each indexed
# project keeps one state directory; graph.json complements the chunk vectors.
GRAPH_DIR_NAME: str = os.environ.get("CODE_GRAPH_DIR_NAME", ".semantic_index")
GRAPH_FILE_NAME: str = os.environ.get("CODE_GRAPH_FILE_NAME", "graph.json")

# --- Sweep settings ------------------------------------------------------------
# Directories never descended into (matched by directory name, any depth).
# Mirrors semantic_index.config.IGNORED_DIRS on purpose: the two layers stay
# decoupled, so each owns its own ignore list.
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
        ".semantic_index",
    }
)

# File extensions scanned by the structural sweep.
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

# Per-file size guard (minified/vendored blobs) and whole-graph bounds so the
# adjacency structure stays bounded on massive game codebases.
MAX_FILE_SIZE_BYTES: int = int(os.environ.get("CODE_GRAPH_MAX_FILE_SIZE_BYTES", str(300 * 1024)))
MAX_SCAN_FILES: int = int(os.environ.get("CODE_GRAPH_MAX_SCAN_FILES", "20000"))
MAX_TOTAL_EDGES: int = int(os.environ.get("CODE_GRAPH_MAX_TOTAL_EDGES", "50000"))
MAX_TOTAL_SYMBOLS: int = int(os.environ.get("CODE_GRAPH_MAX_TOTAL_SYMBOLS", "50000"))
MAX_CACHED_PROJECTS: int = int(os.environ.get("CODE_GRAPH_MAX_CACHED_PROJECTS", "4"))
MAX_DIAGNOSTICS: int = 200
MAX_SNAPSHOT_BYTES: int = 64 * 1024 * 1024

# --- Relation output settings -----------------------------------------------------
# Edge context previews and declaration-line previews are capped here.
EDGE_CONTEXT_MAX_CHARS: int = int(os.environ.get("CODE_GRAPH_EDGE_CONTEXT_MAX_CHARS", "200"))
# Hard cap on relationships returned by one codebase_query_graph call.
MAX_RELATION_LIMIT: int = int(os.environ.get("CODE_GRAPH_MAX_RELATION_LIMIT", "200"))
# BFS depth for 'hierarchy' traces (base-class chains this deep).
MAX_HIERARCHY_DEPTH: int = int(os.environ.get("CODE_GRAPH_MAX_HIERARCHY_DEPTH", "8"))
# Cap on entries listed by one codebase_agentic_navigate list_dir call.
NAVIGATE_LISTING_MAX: int = int(os.environ.get("CODE_GRAPH_NAVIGATE_LISTING_MAX", "200"))
