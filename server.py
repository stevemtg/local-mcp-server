"""Codebase Semantic Index — MCP server entry point (stdio transport).

Exposes four tools to any MCP client (e.g. the Cline extension in VS Code):

* index_codebase(directory_path)                        -> build/refresh the index
* semantic_search(query, directory_path, ...)           -> discovery by meaning
* semantic_search_similar(path, directory_path, ...)    -> find similar code
* get_index_status(directory_path)                      -> index health/metadata

Policy: semantic results are CANDIDATES ONLY. The live filesystem is always
authoritative; findings must be verified by reading current source.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from semantic_index.config import DEFAULT_SEARCH_LIMIT
from semantic_index.indexer import Indexer

# stdout is reserved for JSON-RPC framing — all logging must go to stderr.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("semantic_index.server")

server = MCPServer(
    name="codebase-semantic-index",
    instructions=(
        "Fully local semantic code discovery. Workflow: semantic discovery -> "
        "exact source verification -> conclusion -> edit -> validate. "
        "Call index_codebase once per project to build the .semantic_index "
        "vector database, then use semantic_search to LOCATE candidate files/"
        "symbols by concept and semantic_search_similar to find code similar "
        "to a known snippet. Results are candidates only: ALWAYS read the "
        "current source with exact tools (read_file, grep/rg, symbol search) "
        "before concluding behavior or editing. If indexed content and the "
        "current source disagree, the current source wins. Prefer exact "
        "search when the user already gave a specific filename, symbol, "
        "error message, route, or exact string."
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


@server.tool()
def semantic_search(
    query: str,
    directory_path: str,
    limit: int = DEFAULT_SEARCH_LIMIT,
    path_filter: str | None = None,
    min_score: float | None = None,
) -> dict:
    """DISCOVERY tool: find WHERE code that matches a concept is implemented.

    Use this when you know the behavior/concept/feature but NOT the exact file
    or symbol — it searches by semantic meaning over the indexed codebase and
    returns candidate locations (path, line range, symbol, snippet). Examples
    of good queries: "where is authentication state updated?", "code
    responsible for retrying failed payments", "where user permissions are
    enforced", "flow from API request to database persistence",
    "implementations similar to background job scheduling".

    Rules:
    - Results are CANDIDATES ONLY. Always verify by reading the actual current
      source with read_file / grep before concluding behavior or editing.
      Never edit code based solely on an embedding result.
    - The index can be stale; the live filesystem is authoritative.
    - Prefer exact search (grep/read) first when the user already provided a
      specific filename, symbol, constant, error message, route or string.
    - For broad questions, run several focused queries instead of one vague one.
    - Optional: path_filter (glob or path segment, e.g. "src/auth/*"), min_score
      to drop weak matches.

    Returns {"results": [{path, absolutePath, startLine, endLine, symbol,
    score, snippet, reason}, ...]}.
    """
    logger.info("semantic_search(%r, %s, limit=%d)", query, directory_path, limit)
    return indexer.semantic_search(
        directory_path, query, limit=limit, path_filter=path_filter, min_score=min_score
    )


@server.tool()
def semantic_search_similar(
    directory_path: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> dict:
    """Find code IMPLEMENTATIONS SIMILAR to a known snippet or file.

    Answers "where else is something done like this?" — e.g. other handlers
    following the same pattern, or other places with the same kind of logic.
    The query is built from the CURRENT on-disk source at ``path`` (optionally
    narrowed to start_line..end_line), so it always reflects reality even if
    the index is stale. The queried source is split into chunks (same chunker
    as indexing), each chunk is embedded separately, per-chunk results are
    merged keeping the best score per chunk, and chunks overlapping the
    queried range are excluded. Same rules as semantic_search: candidates
    only; verify by reading the actual files before asserting anything or
    editing.
    """
    logger.info("semantic_search_similar(%s:%s-%s, %s)", path, start_line, end_line, directory_path)
    return indexer.similar(
        directory_path, path, start_line=start_line, end_line=end_line, limit=limit
    )


@server.tool()
def get_index_status(directory_path: str) -> dict:
    """Report index state for a project: indexed?, file/chunk counts, timestamps."""
    logger.info("get_index_status(%s)", directory_path)
    return indexer.status(directory_path)


if __name__ == "__main__":
    # stdio JSON-RPC transport — the only transport a local Cline client needs.
    server.run(transport="stdio")

