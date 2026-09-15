"""Codebase Semantic Index — MCP server entry point (stdio transport).

Exposes five tools to any MCP client (e.g. the Cline extension in VS Code):

* index_codebase(directory_path)                        -> build/refresh the index
* semantic_search(query, directory_path, ...)           -> discovery by meaning
* semantic_search_similar(path, directory_path, ...)    -> find similar code
* get_symbol_context(directory_path, path, line, ...)   -> live-source context pack
* get_index_status(directory_path)                      -> index health/metadata

Policy: semantic results are CANDIDATES ONLY. The live filesystem is always
authoritative; findings must be verified by reading current source.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from semantic_index.config import (
    DEFAULT_MAX_REFERENCES,
    DEFAULT_SEARCH_LIMIT,
    MAX_REFERENCES_CAP,
    MAX_SEARCH_LIMIT,
)
from semantic_index.indexer import Indexer

# stdout is reserved for JSON-RPC framing — all logging must go to stderr.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("semantic_index.server")

ProjectDirectory = Annotated[str, Field(
    min_length=1, description="Absolute root of the project to inspect, e.g. C:/work/project."
)]
SourcePath = Annotated[str, Field(
    min_length=1,
    description="Existing source file relative to directory_path, e.g. app.py or Assets/Player.cs. Use the result's path, not absolutePath. Root escapes and absolute paths are rejected."
)]
SearchLimit = Annotated[int, Field(
    ge=1, le=MAX_SEARCH_LIMIT,
    description="Maximum number of candidate chunks to return; keep small for focused discovery."
)]
READ_ONLY_ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=False)

server = MCPServer(
    name="codebase-semantic-index",
    instructions=(
        "Choose by task: semantic_search locates code from a behavior described "
        "in words; semantic_search_similar finds implementations resembling a "
        "known file or method; get_symbol_context inspects a known file and line. "
        "Use exact search for known names and strings. All three tools are read-only. "
        "Only the two searches require an index and Ollama embeddings; "
        "get_symbol_context works directly on live files without either. "
        "Pass the absolute project root as directory_path and a project-relative "
        "file as path. Inputs use start_line/end_line; result startLine/endLine "
        "are output fields. To inspect a hit, pass its path and startLine as line "
        "to get_symbol_context, then verify the live source. Search snapshots "
        "may be stale; scores are similarity, not confidence. Empty results "
        "do not prove absence. If search reports a missing index, check "
        "get_index_status; index_codebase writes index state and requires task "
        "authorization. Correct invalid arguments before retrying."
    ),
)
indexer = Indexer()


@server.tool()
def index_codebase(directory_path: str) -> dict:
    """Index (or incrementally re-index) a project directory. WRITE OPERATION.

    Walks the directory recursively (ignoring .git, node_modules, dist, ...),
    skips files whose SHA-256 hash is unchanged since the last index, splits
    changed files into semantic blocks (functions/classes/methods), embeds each
    block with the local Ollama 'nomic-embed-text' model and stores the vectors
    in <directory_path>/.semantic_index/index.db. Must be called once per
    project before semantic_search works; re-run to pick up file changes.
    """
    logger.info("index_codebase(%s)", directory_path)
    return indexer.index_directory(directory_path)


@server.tool(title="Find code by behavior", annotations=READ_ONLY_ANNOTATIONS)
def semantic_search(
    query: Annotated[str, Field(
        min_length=1,
        description="One focused description of the desired implementation, e.g. 'retry failed requests with exponential backoff'."
    )],
    directory_path: ProjectDirectory,
    limit: SearchLimit = DEFAULT_SEARCH_LIMIT,
    path_filter: Annotated[str | None, Field(
        min_length=1,
        description="Optional project-relative glob or path segment, e.g. 'src/auth/*', '*.cs', or 'tests'. Use forward slashes; omit to search all indexed files."
    )] = None,
    min_score: Annotated[float | None, Field(
        ge=-1, le=1,
        description="Optional minimum cosine similarity (-1 to 1), applied before ranking. Omit initially: this is not a confidence threshold."
    )] = None,
) -> dict:
    """Find where a behavior is implemented when its location is unknown.

    Use one focused description of the desired code. Use exact search for a
    known name/string, or semantic_search_similar for an example implementation.
    Read-only. Requires an existing index and Ollama with its embedding model.

    Example arguments:
    {"directory_path":"C:/work/project","query":"retry failed requests with exponential backoff","limit":4}

    Returns results with path, startLine, endLine, symbol, score, snippet, and
    reason, plus a note. Lines are 1-based and inclusive. These are indexed
    snapshots; symbol is a best-effort chunk label. Scores measure similarity,
    not confidence; diversity ranking can put a lower score before a higher one.

    Next: pass a promising result's path and startLine as path and line to
    get_symbol_context, then verify the live source. If the location has moved,
    find it with exact search. Empty results do not prove absence: check index
    state, broaden filters, or rephrase. Search never builds or refreshes an index.
    """
    logger.info("semantic_search(%r, %s, limit=%d)", query, directory_path, limit)
    return indexer.semantic_search(
        directory_path, query, limit=limit, path_filter=path_filter, min_score=min_score
    )


@server.tool(title="Find similar implementations", annotations=READ_ONLY_ANNOTATIONS)
def semantic_search_similar(
    directory_path: ProjectDirectory,
    path: SourcePath,
    start_line: Annotated[int | None, Field(
        ge=1, description="First live-source line to use, 1-based and inclusive; defaults to line 1. Prefer a complete method or function."
    )] = None,
    end_line: Annotated[int | None, Field(
        ge=1, description="Last live-source line to use, inclusive; defaults to the file's final line. Must be at least start_line and within the file."
    )] = None,
    limit: SearchLimit = DEFAULT_SEARCH_LIMIT,
) -> dict:
    """Find other implementations resembling a known file or method.

    Use for similar handlers or potential reuse. Reads the query from live
    source, even if unindexed. Read-only. Requires a candidate index and Ollama
    with its embedding model. Use get_symbol_context to inspect the given code;
    use semantic_search when you only have a behavior described in words.

    Example arguments:
    {"directory_path":"C:/work/project","path":"app.py","start_line":5,"end_line":13,"limit":4}

    Prefer a complete method. Bounds are 1-based and inclusive; either can be
    omitted. Omitting both queries the whole file. Excludes overlapping chunks
    in the query file; whole-file queries exclude that entire file. Large inputs
    may be capped; note explains how to narrow the range.

    Returns deduplicated results with path, startLine, endLine, symbol, score,
    snippet, and reason, plus range and note. Range is resolved bounds or null
    for a whole file. Scores measure similarity, not equivalent behavior.
    Next: call get_symbol_context with a result's path and startLine as line,
    then verify the live source; indexed candidates may be stale.
    """
    logger.info("semantic_search_similar(%s:%s-%s, %s)", path, start_line, end_line, directory_path)
    return indexer.similar(
        directory_path, path, start_line=start_line, end_line=end_line, limit=limit
    )


@server.tool(title="Inspect a live symbol", annotations=READ_ONLY_ANNOTATIONS)
def get_symbol_context(
    directory_path: ProjectDirectory,
    path: SourcePath,
    line: Annotated[int, Field(
        ge=1,
        description="Existing 1-based line inside the symbol to inspect. From a search hit, pass startLine as line; verify the location if the index is stale."
    )],
    max_references: Annotated[int, Field(
        description=f"Maximum lexical references across the project. Omit for the default; values are clamped to 1-{MAX_REFERENCES_CAP}."
    )] = DEFAULT_MAX_REFERENCES,
) -> dict:
    """Inspect the live symbol containing a known file and line before editing.

    Call directly when the user or exact search gives a location, or after
    semantic_search/semantic_search_similar. Read-only: no Ollama, embeddings,
    or existing index required. Never creates or refreshes an index.

    Example arguments:
    {"directory_path":"C:/work/project","path":"app.py","line":5}

    Returns symbol (name, kind, startLine, endLine, declaration), bounded
    snippet, imports, references, relatedTests, indexState, and note. Boundaries
    are best-effort; an unnamed block can be returned when parsing fails.
    References are capped exact identifier matches outside the definition,
    including comments/strings, not proven callers or callees. relatedTests
    groups only returned references in test-like paths; empty does not mean
    there are no tests.

    indexState is current, stale, or not_indexed for this file's stored hash;
    live source remains authoritative in every state. Next: read the live file
    to verify behavior or omitted code; use exact search for complete references.
    If line is outside the current file, locate the symbol and retry.
    """
    logger.info("get_symbol_context(%s:%s, %s)", path, line, directory_path)
    return indexer.symbol_context(directory_path, path, line, max_references)


@server.tool()
def get_index_status(directory_path: str) -> dict:
    """Report index state for a project: indexed?, file/chunk counts, timestamps."""
    logger.info("get_index_status(%s)", directory_path)
    return indexer.status(directory_path)


if __name__ == "__main__":
    # stdio JSON-RPC transport — the only transport a local Cline client needs.
    server.run(transport="stdio")

