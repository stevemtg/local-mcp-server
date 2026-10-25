# Structural graph and agentic navigation

The graph complements `.semantic_index/index.db`. It does not change the vector
schema, run embeddings, or require Ollama. All three tools are registered beside
the existing image and semantic tools. Reconnect the MCP client to discover them.

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `codebase_build_relations` | `directory_path` | Sweeps source files and atomically replaces `.semantic_index/graph.json`. |
| `codebase_query_graph` | `directory_path`, `target_symbol`, `relationship_type`, optional `limit` | Returns dependencies, dependents, or inheritance hierarchy from the snapshot. |
| `codebase_agentic_navigate` | `directory_path`, `current_path`, `action` | Lists a directory or summarizes live file declarations and imports. |

`directory_path` must be an existing absolute directory. `current_path` can be a
relative path or an absolute path inside that directory. Resolved paths cannot
escape the root. Navigation requires neither a graph nor a semantic index.

## Typical sequence

Call `codebase_agentic_navigate` to inspect a file's interface before searching:

```json
{"directory_path":"C:/work/game","current_path":"Assets/Player.cs","action":"view_file_summary"}
```

Use `action: "list_dir"` with `current_path: "."` for the project root. Summaries
return symbol names, declaration lines, kinds, ranges, and imports, without full
function bodies. Reads and responses are bounded.

Build structural memory independently of `index_codebase`:

```json
{"directory_path":"C:/work/game"}
```

Pass that object to `codebase_build_relations`. The result includes file, symbol,
and edge counts, `builtAt`, `truncated`, and per-file `diagnostics`. A successful
partial build has `status: "ok"` and `truncated: true`; parse failures do not
discard relationships from other files. `diagnosticCount` includes errors beyond
the 200 displayed diagnostics.

After `semantic_search` or `semantic_search_similar`, trace a symbol with
`codebase_query_graph`:

```json
{"directory_path":"C:/work/game","target_symbol":"Assets/Player.cs::Player.Start","relationship_type":"dependents","limit":20}
```

The allowed relationship types are `dependencies`, `dependents`, and `hierarchy`.
The default is `dependencies`. Hierarchy returns ancestors and descendants, each
bounded by the total edge limit and eight levels by default. A file query includes
its contained symbols; a class query includes its members. Qualified names keep
same-named definitions in different files separate. Bare names can return multiple
candidates. Non-Python symbol keys use `file::qualified_name`; Python keys use
module-qualified names.

When a graph exists, both semantic tools add `structural` to each hit, mapping its
path and start line to the innermost stored symbol and returning graph traces.
Scores, ranking, snippets, and other search fields are preserved. Missing graphs
leave the existing response unchanged. An unreadable graph adds a
`structuralDiagnostic` without failing semantic search. Neither search builds a
graph implicitly.

## Modules and integration

- [`extractors.py`](../code_graph/extractors.py): generator-based discovery,
  bounded source reads, Python AST extraction, and token-scanned engine patterns.
- [`graph_store.py`](../code_graph/graph_store.py): adjacency indexes, qualified
  locations, bounded hierarchy traversal, atomic snapshots, and an LRU cache.
- [`router.py`](../code_graph/router.py): graph queries, live summaries, diagnostics,
  and the `enrich_results(directory_path, payload)` semantic-result adapter.
- [`tool.py`](../code_graph/tool.py): asynchronous MCP registration. Blocking work
  runs in AnyIO worker threads.

For another existing server using this repository's MCP SDK registration API:

```python
from code_graph.tool import register

graph_router = register(server)
```

`server` is the application's existing MCPServer. The registration module is the
only SDK-dependent part; applications using another SDK can wrap the same router
methods in their own decorators. This repository's `server.py` also calls
`graph_router.enrich_results` on the existing vector-search response inside its
worker thread. The graph package itself uses only the Python standard library;
its MCP adapter uses the already-installed MCP, AnyIO, and Pydantic packages.

## Bounds and limitations

Defaults are 300 KiB per source file, 20,000 scanned files, 50,000 symbols, 50,000
edges, four cached projects, and 200 edges per query direction. Override these
with positive integer environment variables `CODE_GRAPH_MAX_FILE_SIZE_BYTES`,
`CODE_GRAPH_MAX_SCAN_FILES`, `CODE_GRAPH_MAX_TOTAL_SYMBOLS`,
`CODE_GRAPH_MAX_TOTAL_EDGES`, `CODE_GRAPH_MAX_CACHED_PROJECTS`, and
`CODE_GRAPH_MAX_RELATION_LIMIT`. Snapshots have a 64 MiB read/write limit.
Ignored directories include `.git`, `.semantic_index`, Unity's `Library`, build
outputs, and dependency caches. Linked source files and linked directories are
skipped. Directory listings return at most 200 entries.

Python imports, definitions, calls, and inheritance use `ast`. Other supported
languages use best-effort token patterns, including Unity `AddComponent` and
Unreal `CreateDefaultSubobject`. Comments and string bodies are excluded from
call extraction. This is not compiler-level type or runtime analysis: macros,
overloads, complex multiline declarations, re-exports, and dynamic receivers
may be incomplete or unresolved. `targetCandidates` explicitly exposes possible
bindings. Empty results do not establish absence.

Graphs are snapshots. Rebuild after source changes; verify stored ranges and
semantic hits against live files. Version-1 development snapshots require a
rebuild. Concurrent builds within a server process are serialized; a failed
write preserves the previous cached graph and snapshot. Snapshot persistence
never writes `index.db`.

Run `python -B dev/graph_test.py` for graph regressions, or
`python -B dev/run_tests.py --offline` for the complete offline suite.
