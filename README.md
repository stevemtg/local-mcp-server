# Codebase Semantic Index (local MCP server)

A **fully offline** Model Context Protocol (MCP) server that gives any MCP client
(e.g. the **Cline** extension in VS Code) semantic code search over local
projects. Everything runs on your machine: embeddings come from a local
[Ollama](https://ollama.com) instance running `nomic-embed-text`, and vectors
are persisted in a per-project, file-based SQLite database.

```
┌────────────┐  stdio JSON-RPC  ┌─────────────────────────────────────────┐
│   Cline    │◀═══════════════▶│  server.py  (MCPServer, transport=stdio)│
│ (VS Code)  │                 ├──────────┬──────────┬─────────┬─────────┤
└────────────┘                 │ crawler  │ chunker  │embedder │ store   │
                               │ (walk +  │ (AST-ish │ (Ollama │(SQLite +│
                               │  SHA-256)│ blocks)  │ /api/   │ cosine) │
                               └──────────┴──────────┴─────────┴─────────┘
                                                      │ HTTP
                                             http://localhost:11434 (Ollama)
```

## Requirements

- Python **3.10+** (developed on 3.13)
- [Ollama](https://ollama.com) running locally with the embedding model:

```powershell
ollama pull nomic-embed-text
```

## Install

```powershell
py -m pip install -r requirements.txt   # installs the official `mcp` SDK (>=2.0)
```

`requirements.txt` contains a single dependency (`mcp>=2.0.0`), which pulls in
`anyio`, `httpx`, `pydantic`, etc. Embedding calls use the standard library
(`urllib.request`); the vector store uses the stdlib `sqlite3` module — there
are no other runtime dependencies.

## How it works

| Module | Responsibility |
| --- | --- |
| `server.py` | MCP server init + registration of the five tools; stdio transport. |
| `semantic_index/crawler.py` | Recursive walk (prunes `.git`, `node_modules`, Unity `Library`, `dist`, `__pycache__`, ...), SHA-256 hashing, language detection. |
| `semantic_index/chunker.py` | Structure-aware chunking: indentation-based blocks for Python, regex header + brace-matched blocks for JS/TS/Go/Rust/Java/Kotlin/C/C++/C#, line-window fallback. Oversized blocks are split per method. |
| `semantic_index/embedder.py` | Local Ollama client (`POST /api/embeddings`, model `nomic-embed-text`) with retry/backoff and a thread pool. |
| `semantic_index/store.py` | SQLite vector store in `<project>/.semantic_index/index.db`; float32-packed embeddings, cosine scoring + MMR (maximal marginal relevance) diversification. |
| `semantic_index/indexer.py` | Orchestration: change detection by hash, chunk+embed changed files, stale-entry cleanup, agent-friendly result shaping, status. |
| `semantic_index/symbol_context.py` | Read-only context packs for `get_symbol_context`: live-source symbol selection, import extraction, lexical reference scanning, test grouping, freshness — no embeddings, no writes. |

Index state lives **inside the indexed project** at `<project>/.semantic_index/`
(`index.db` plus SQLite WAL sidecar files). Re-running `index_codebase`
re-embeds only files whose SHA-256 content hash changed; files that were
deleted are removed from the index. Changing `EMBED_MODEL` triggers a full
automatic re-embed.

## MCP tools

| Tool | Read-only? | Parameters | Returns |
| --- | --- | --- | --- |
| `index_codebase` | no (writes `.semantic_index/`) | `directory_path: string` | `{files_embedded, files_unchanged_skipped, files_removed, files_failed, total_chunks, full_reindex, indexed_at}` |
| `semantic_search` | yes | `query: string`, `directory_path: string`, `limit?`, `path_filter?`, `min_score?` | `{results: [{path, absolutePath, startLine, endLine, symbol, score, snippet, reason}]}` |
| `semantic_search_similar` | yes | `directory_path: string`, `path: string`, `start_line?`, `end_line?`, `limit?` | same `results` shape, ranked by similarity to the **current** source at `path` |
| `get_symbol_context` | yes | `directory_path: string`, `path: string`, `line: int`, `max_references?` (default 12, clamped 1–50) | context pack for the live symbol at `path:line`: `{symbol: {name, kind, startLine, endLine, declaration}, snippet, imports, references, relatedTests, indexState, note}` — built from **live source**; `references` are lexical matches, not proven callers/callees |
| `get_index_status` | yes | `directory_path: string` | `{indexed, num_files, num_chunks, embedding_model, embedding_dim, created_at, last_indexed_at}` |

### Choosing and calling a search tool

- Use `semantic_search` for one behavior described in words, such as
  `retry failed requests with exponential backoff`. Use
  `semantic_search_similar` for implementations resembling an existing file
  or complete method. Use exact search for known identifiers and strings.
- Both searches need an existing index and the configured Ollama embedding
  model. They never create or refresh a project index. A missing/empty index
  returns an empty result with guidance; an incompatible model raises an
  error explaining how to re-index. `get_index_status` reports stored index
  metadata; it does not check every live file for changes.
- `limit` accepts **1–50** (default **8**, configurable). `path_filter` accepts
  project-relative globs or segments, such as `src/auth/*`, `*.cs`, or `tests`.
  `min_score` is an optional cosine threshold from **-1 to 1**; omit it initially.
  Scores measure similarity, not confidence. Semantic search balances
  relevance and diversity, so results need not have descending scores.
- Similarity search requires a project-relative `path` and reads live source,
  even if that file is unindexed. Bounds are **1-based and inclusive**;
  `start_line` defaults to 1 and `end_line` defaults to EOF independently.
  Invalid, reversed, or out-of-file ranges raise errors. The returned `range`
  contains resolved bounds, or `null` when both were omitted.
- Similarity search excludes overlapping chunks in the query file; a whole-file
  query excludes that entire file, including stale chunks past the current EOF.
  Results are deduplicated using their best cosine score. Large queries use at
  most `SIMILAR_QUERY_MAX_PARTS` chunks; the response `note` reports any cap.
- Result symbols, snippets, and line numbers come from the stored index.
  Existing indexes can retain older chunk labels after a parser update.
  Follow promising hits with `get_symbol_context` and read the live file.
  Empty results do not establish absence: check index state, broaden filters,
  or rephrase the query.

All three discovery/context tools publish parameter descriptions, input examples,
and MCP `readOnlyHint`/`openWorldHint` annotations through `tools/list`.
Search limits and line numbers carry validation constraints; `max_references`
keeps its documented clamping behavior.

## Intended workflow (hybrid search policy)

```text
semantic discovery → symbol context → exact source verification → conclusion → edit → validate
```

- **Semantic search** (`semantic_search`, `semantic_search_similar`) is for
  *discovery*: unknown implementation locations, code located by concept,
  similar functionality, cross-cutting behavior. Results are **candidates
  only** — small, diverse (MMR) lists of paths + line ranges + symbols.
- **Exact search** (grep/`rg`, symbol lookup, references) is for known names:
  functions, classes, imports, callers, routes, constants, error messages,
  config keys, exact strings.
- **Source reading** (`read_file` on the live file) determines what the code
  actually does. **The filesystem is authoritative: if indexed content and the
  current source disagree, the current source wins.** Always read the current
  file before editing.
- **Symbol context** (`get_symbol_context`, read-only) is the step *after*
  semantic discovery and *before* editing: given a known `file:line`, it
  returns a live-source context pack — the containing symbol (name, kind,
  range, declaration, snippet), the file's imports, exact lexical
  `references` (identifier matches sorted by path/line), `relatedTests`,
  and `indexState` freshness. Its references are lexical candidates only —
  never describe them as proven callers or callees.
- **Validation** via tests / build / lint / typecheck after changes.

A copy of this policy ships as `.clinerules/10-semantic-code-search.md` —
copy that file (or point Cline's rules at it) in projects where you want the
behavior enforced.

## Cline / VS Code configuration

### Instructions for a local chat model

The three tool descriptions are self-contained: each states when to use it,
gives a JSON argument example, describes results, and provides the next step.
The shared server instructions and
[Cline rule](.clinerules/10-semantic-code-search.md) use the same routing:

| Task | Tool |
| --- | --- |
| Find an implementation from a behavior | `semantic_search` |
| Find implementations resembling known code | `semantic_search_similar` |
| Inspect a known file and line | `get_symbol_context` directly |

- Cline's conversational model and the MCP server's Ollama embedding model
  are configured separately. The searches use the server's `EMBED_MODEL`;
  `get_symbol_context` needs neither embeddings nor an existing index.
- Keep calls focused: start with `limit: 4`, query a complete method for
  similarity, and omit unused optional arguments. All examples show the
  tool's argument object; use the calling format supplied by Cline.
- Input names use `start_line` and `end_line`. A result's `startLine` becomes
  `line` when calling `get_symbol_context`; use its relative `path`.
- To use the workflow in another project, copy the rule into that project's
  `.clinerules/` and enable it in Cline's Rules panel. The rule in this server
  repository applies to this workspace. Cline reads workspace rules as prompt
  context, so keep them focused. See [Cline's rules documentation](https://docs.cline.bot/customization/cline-rules).
- Reconnect the MCP server in Cline after changing `server.py` so it receives
  the latest descriptions and schemas. The JSON examples are exercised by
  the MCP end-to-end tests; model-specific tool selection still depends on
  the chosen local model and its Cline configuration.

### Server connection

Add to Cline's MCP settings (`mcp_settings.json`). Only genuinely read-only
tools are auto-approved; the stateful `index_codebase` is **not**:

```json
{
  "mcpServers": {
    "codebase-semantic-index": {
      "command": "py",
      "args": ["E:/local-mcp-semantic-index/server.py"],
      "env": {
        "OLLAMA_URL": "http://localhost:11434",
        "EMBED_MODEL": "nomic-embed-text"
      },
      "autoApprove": [
        "semantic_search",
        "semantic_search_similar",
        "get_symbol_context",
        "get_index_status"
      ]
    }
  }
}
```

If `py` isn't resolvable from Cline's shell, use the absolute interpreter path,
e.g. `"command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python313\\python.exe"`.

Then, in Cline chat: *"Fix the issue where users occasionally get logged out
after refreshing their profile"* → Cline should call `semantic_search` with
focused queries ("profile refresh flow", "session state", "logout"), call
`get_symbol_context` on the located `file:line` for a live-source context
pack (symbol, imports, lexical references, related tests), open the returned
candidate files to verify the real behavior, trace callers with exact
search, fix the cause, and run the tests.

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama base URL |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model name |
| `EMBED_TIMEOUT_SECONDS` | `60` | Per-request timeout |
| `EMBED_MAX_RETRIES` | `3` | Retry attempts per embedding |
| `EMBED_CONCURRENCY` | `4` | Parallel embedding requests |
| `MAX_FILE_SIZE_BYTES` | `307200` | Skip files larger than this |
| `MAX_CHUNK_LINES` | `180` | Split blocks longer than this |
| `DEFAULT_SEARCH_LIMIT` | `8` | Default top-K for semantic searches |
| `SIMILAR_QUERY_MAX_PARTS` | `12` | Maximum source chunks used for a similarity query; a response note reports truncation |
| `MMR_LAMBDA` | `0.5` | Relevance(1.0) ↔ diversity(0.0) trade-off |
| `MMR_POOL_FACTOR` | `6` | MMR re-ranks the raw top `limit × factor` hits |
| `SNIPPET_MAX_LINES` | `24` | Snippets truncated to this many lines |
| `DEFAULT_MAX_REFERENCES` | `12` | Default cap on `get_symbol_context` references |
| `MAX_REFERENCES_CAP` | `50` | Hard ceiling for the `max_references` argument |
| `CONTEXT_SNIPPET_MAX_LINES` | `40` | Max lines in a `get_symbol_context` snippet/window |
| `CONTEXT_IMPORTS_MAX` | `20` | Max import entries listed per file |
| `CONTEXT_PREVIEW_MAX_CHARS` | `240` | Char cap for previews and declaration headers |

## Error handling

- Files that fail to hash, decode, chunk, or embed are **logged to stderr and
  skipped** — the MCP process never crashes on a single bad file.
- All logging goes to **stderr**; stdout carries only JSON-RPC frames.
- SQLite writes are transactional per file (rollback on failure).

## Limitations

- Chunking is regex/heuristic based (no tree-sitter): block starts on exotic
  syntax may be missed and fall back to fixed line-window chunks.
- Search is a brute-force cosine scan over the SQLite store — perfectly fine
  for tens of thousands of chunks, not for million-chunk monorepos.
- `nomic-embed-text` truncates inputs around 8k tokens; very large blocks are
  pre-split via `MAX_CHUNK_LINES`.

## Development

```powershell
py dev\run_tests.py      # full suite (offline regressions + MCP end-to-end)
py dev\smoke_test.py     # offline unit smoke: chunker + store (fake vectors)
py dev\csharp_test.py    # offline C# scope and symbol-context regressions
py dev\search_test.py    # offline search validation, filtering, ranges, and read-only checks
py dev\e2e_test.py       # full MCP stdio round-trip against real Ollama
```
