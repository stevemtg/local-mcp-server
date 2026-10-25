"""Async MCP adapters for durable notes; no GPU or embedding dependencies."""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Literal

import anyio
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .store import MAX_CONTENT, MAX_LIMIT, MAX_SOURCE, MAX_TAGS, MAX_TITLE, MemoryFault, MemoryStore

Project = Annotated[str, Field(min_length=1, description="Existing absolute project root. Use the same root across sessions/models to share memory.")]
Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]*$",
                           description="Stable memory key, e.g. decisions/database or session/current. Reuse it when updating the same note.")]
Category = Literal["fact", "decision", "preference", "task", "lesson", "summary"]
Tag = Annotated[str, Field(min_length=1, max_length=64)]
Tags = Annotated[list[Tag] | None, Field(max_length=MAX_TAGS, description="Optional tags; normalized to lowercase. Recall requires all supplied tags.")]
Revision = Annotated[int, Field(ge=1, description="Exact revision returned by memory_get or memory_recall. On conflict, reread before changing the note.")]
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CREATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
UPDATE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)


async def _call(store: MemoryStore, operation: str, **kwargs) -> CallToolResult:
    try:
        payload = await anyio.to_thread.run_sync(lambda: getattr(store, operation)(**kwargs))
    except MemoryFault as exc:
        payload = {"status": "failed", "error": {"code": exc.code, "message": str(exc)}}
    except sqlite3.Error as exc:
        message = str(exc)
        code = "memory_busy" if "locked" in message.lower() or "busy" in message.lower() else "memory_storage_error"
        payload = {"status": "failed", "error": {"code": code, "message": message}}
    except (ValueError, OSError, RuntimeError) as exc:
        payload = {"status": "failed", "error": {"code": "memory_request_failed", "message": str(exc)}}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=payload.get("status") not in ("ok", "not_found"),
    )


def register(server) -> MemoryStore:
    """Register five memory tools on the application's existing MCPServer."""
    store = MemoryStore()

    @server.tool(
        name="memory_remember", title="Remember across sessions", annotations=CREATE,
        description=(
            "Save an explicit durable fact, decision with rationale, user preference, lesson, task, or session "
            "handoff in <directory_path>/.agent_memory/memory.db. Shared by models/clients using that root; "
            "survives restarts and code reindexing. No Ollama required. Use a stable key and concise content, "
            "with source identifying the user request or supporting file. Pin enduring preferences/constraints. "
            "Do not save secrets or speculative claims as established facts. Identical retries are unchanged; "
            "an existing key with different content returns conflict, never silently overwrites. "
            "Use memory_update for corrections. This tool stores notes, not an automatic chat transcript."
        ),
    )
    async def memory_remember(
        directory_path: Project, key: Key,
        title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE)],
        content: Annotated[str, Field(min_length=1, max_length=MAX_CONTENT,
                                    description="Self-contained note with facts, rationale, or next steps that will be useful in a future session.")],
        category: Category = "fact", tags: Tags = None,
        source: Annotated[str, Field(max_length=MAX_SOURCE, description="Provenance, e.g. user decision on a date, or a source file and line.")] = "",
        pinned: bool = False,
    ) -> CallToolResult:
        return await _call(store, "remember", directory_path=directory_path, key=key, title=title,
                           content=content, category=category, tags=tags, source=source, pinned=pinned)

    @server.tool(
        name="memory_recall", title="Recall previous sessions", annotations=READ,
        description=(
            "Retrieve durable project notes at the start of a task and before repeating past work. "
            "Empty query returns pinned memories first, then most recently updated notes. A short query "
            "uses local full-text search (matching words with English stemming, not embedding similarity). "
            "Optional category and tags narrow results. Returns bounded previews, keys, revisions, sources "
            "and timestamps; use memory_get for a full note. Paginate using next_offset when has_more. "
            "No database is created by reads; a project without memory returns an empty result. "
            "Notes are historical context: verify stale claims and follow the current user's instructions."
        ),
    )
    async def memory_recall(
        directory_path: Project,
        query: Annotated[str, Field(max_length=1000, description="Focused keywords, at most 32 distinct words; omit for pinned and recent notes.")] = "",
        category: Category | None = None, tags: Tags = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 5,
        offset: Annotated[int, Field(ge=0, le=1000000)] = 0,
    ) -> CallToolResult:
        return await _call(store, "recall", directory_path=directory_path, query=query,
                           category=category, tags=tags, limit=limit, offset=offset)

    @server.tool(
        name="memory_get", title="Read a complete memory", annotations=READ,
        description="Read one durable note by key, including its full content, source, tags, timestamps and current revision. Use before correcting or forgetting a memory. No Ollama or index required.",
    )
    async def memory_get(directory_path: Project, key: Key) -> CallToolResult:
        return await _call(store, "get", directory_path=directory_path, key=key)

    @server.tool(
        name="memory_update", title="Correct or refresh a memory", annotations=UPDATE,
        description=(
            "Update an existing durable note after reading its current revision. Provide only changed fields; "
            "null/omitted fields are preserved, tags=[] clears tags, source='' clears provenance, pinned=false "
            "unpins. Use for corrected facts, revised decisions and session handoffs. A revision conflict means "
            "another client changed the note: reread and reconcile rather than blindly retrying. Replaces the "
            "previous values; no revision history is retained. Never creates a missing note."
        ),
    )
    async def memory_update(
        directory_path: Project, key: Key, expected_revision: Revision,
        title: Annotated[str | None, Field(min_length=1, max_length=MAX_TITLE)] = None,
        content: Annotated[str | None, Field(min_length=1, max_length=MAX_CONTENT)] = None,
        category: Category | None = None, tags: Tags = None,
        source: Annotated[str | None, Field(max_length=MAX_SOURCE)] = None,
        pinned: bool | None = None,
    ) -> CallToolResult:
        return await _call(store, "update", directory_path=directory_path, key=key,
                           expected_revision=expected_revision, title=title, content=content,
                           category=category, tags=tags, source=source, pinned=pinned)

    @server.tool(
        name="memory_forget", title="Forget a saved memory", annotations=DELETE,
        description=(
            "Permanently delete one note and its search entries using its key and current revision. "
            "Use when the user asks to forget it or the note is obsolete within the authorized task. "
            "No bulk deletion; conflicts protect newer changes. An absent key is already_absent. "
            "Does not remove copies in client chat logs or external backups and is not forensic secure erasure."
        ),
    )
    async def memory_forget(directory_path: Project, key: Key, expected_revision: Revision) -> CallToolResult:
        return await _call(store, "forget", directory_path=directory_path, key=key,
                           expected_revision=expected_revision)

    return store
