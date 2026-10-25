"""MCP adapters for the structural graph layer (mirrors image_orchestration tools).

Exposes three tools through ``register(server)``:
codebase_build_relations (write), codebase_query_graph (read-only),
codebase_agentic_navigate (read-only live-source follow-up actions).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal

import anyio
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .config import MAX_RELATION_LIMIT
from .router import StructuralRouter

logger = logging.getLogger("code_graph.tool")

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _result(payload: dict, ok_when: tuple[str, ...]) -> CallToolResult:
    is_error = payload.get("status") not in ok_when
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structured_content=payload,
        is_error=is_error,
    )


def register(server) -> StructuralRouter:
    """Advertise the structural graph tools on the given MCPServer."""
    router = StructuralRouter()

    @server.tool(
        name="codebase_build_relations",
        title="Build structural relationship graph",
        description=(
            "Build (or rebuild) the structural relationship graph for a project directory. WRITE "
            "OPERATION: scans every indexed source file and OVERWRITES "
            "<directory_path>/.semantic_index/graph.json. Python files are parsed with the ast "
            "module; C#/JS/TS/Go/Rust/Java/Kotlin/C/C++ go through a heuristic line scanner. "
            "Records imports, inheritance, cross-file calls, and engine component attachments "
            "(Unity/Unreal patterns). Skips excluded directories ('Library', 'node_modules', "
            "and friends). Must be called once per project before codebase_query_graph; "
            "re-run after significant edits. Oversized files and parse failures are reported "
            "in diagnostics; truncated means the graph is partial. Example: "
            '{\"directory_path\":\"C:/work/project\"}'
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                    idempotent_hint=True, open_world_hint=False),
    )
    async def codebase_build_relations(
        directory_path: Annotated[str, Field(
            min_length=1, description="Absolute root of the project to scan, e.g. C:/work/project."
        )],
    ) -> CallToolResult:
        logger.info("codebase_build_relations(%s)", directory_path)
        payload = await anyio.to_thread.run_sync(router.build, directory_path)
        return _result(payload, ok_when=("ok",))

    @server.tool(
        name="codebase_query_graph",
        title="Query structural relationships",
        description=(
            "Query the stored structural graph for one symbol or file: dependencies (what it "
            "imports/calls/attaches), dependents (who imports/calls/extends it), and hierarchy "
            "(transitive base-class chain, bounded depth, both directions). Read-only; requires "
            "codebase_build_relations first - it never scans lazily. Edges are heuristic "
            "candidates extracted from a snapshot, NOT proven callers; live source is "
            "authoritative, so verify with get_symbol_context or exact search before editing. "
            "Empty results do not prove absence. Example: "
            '{\"directory_path\":\"C:/work/project\",\"target_symbol\":\"PlayerController\",\"relationship_type\":\"dependencies\"}'
        ),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def codebase_query_graph(
        directory_path: Annotated[str, Field(
            min_length=1, description="Absolute root of the project whose graph was built."
        )],
        target_symbol: Annotated[str, Field(
            min_length=1,
            description=(
                "Symbol or file to trace: a bare or dotted name ('Helper', 'pkg.mod.Helper') "
                "or a project-relative path ('pkg/helper.py')."
            ),
        )],
        relationship_type: Annotated[Literal["dependencies", "dependents", "hierarchy"], Field(
            description="Which relationship view to return.",
        )] = "dependencies",
        limit: Annotated[int, Field(
            ge=1, le=MAX_RELATION_LIMIT,
            description="Cap on edges per view; bounded BFS depth applies to hierarchy.",
        )] = MAX_RELATION_LIMIT,
    ) -> CallToolResult:
        logger.info("codebase_query_graph(%s, %s)", target_symbol, directory_path)
        payload = await anyio.to_thread.run_sync(
            lambda: router.query(directory_path, target_symbol, relationship_type, limit)
        )
        return _result(payload, ok_when=("ok", "no_match"))

    @server.tool(
        name="codebase_agentic_navigate",
        title="Navigate graph findings on live source",
        description=(
            "Explore before search or follow up a graph result. Actions: list_dir (bounded "
            "directory listing), view_file_summary (live declarations, symbols, and imports). "
            "Needs no graph, vector index, or Ollama. Read-only. Paths cannot "
            "escape directory_path. Example: "
            '{\"directory_path\":\"C:/work/project\",\"action\":\"view_file_summary\",\"current_path\":\"pkg/helper.py\"}'
        ),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def codebase_agentic_navigate(
        directory_path: Annotated[str, Field(
            min_length=1, description="Absolute root of the project to navigate."
        )],
        current_path: Annotated[str, Field(
            min_length=0,
            description="Directory or file inside the project; use '.' for the project root.",
        )],
        action: Annotated[Literal["list_dir", "view_file_summary"], Field(
            description="Navigation action: list_dir or view_file_summary.",
        )],
    ) -> CallToolResult:
        logger.info("codebase_agentic_navigate(%s: %s)", action, current_path)
        payload = await anyio.to_thread.run_sync(
            lambda: router.navigate(directory_path, action, current_path)
        )
        return _result(payload, ok_when=("ok", "no_match"))

    return router

