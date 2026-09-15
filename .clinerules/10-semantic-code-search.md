# Code discovery with the semantic-index MCP server

Use the connected `codebase-semantic-index` tools through Cline's provided
MCP tool interface. The examples below are JSON argument objects; replace the
sample project root and locations with real ones. Omit unused optional fields.

## Choose the tool

| You know / need | Action |
| --- | --- |
| A behavior, but not its implementation location | `semantic_search` with one focused query |
| A file or method; want other similar implementations | `semantic_search_similar` with a complete method range |
| A file and line; need its definition and references | `get_symbol_context` directly |
| An exact name or string; need its location | Cline's available exact-search tool, then `get_symbol_context` |

`semantic_search` example:

```json
{"directory_path":"C:/work/project","query":"retry failed requests with exponential backoff","limit":4}
```

`semantic_search_similar` example:

```json
{"directory_path":"C:/work/project","path":"app.py","start_line":5,"end_line":13,"limit":4}
```

`get_symbol_context` example:

```json
{"directory_path":"C:/work/project","path":"app.py","line":5}
```

## Arguments and next steps

- `directory_path`: absolute root of the project being inspected.
- `path`: project-relative source file, using forward slashes. Copy a result's
  `path`; reserve `absolutePath` for file-reading tools that accept it.
- Lines are 1-based. Similarity ranges include both endpoints. Input arguments
  are **`start_line` / `end_line`**; output fields are `startLine` / `endLine`.
  To inspect a search hit, pass its `path` and use its `startLine` as **`line`**
  in `get_symbol_context`.
- Start with a small result count, such as `limit: 4`. For similarity search,
  select a complete method. Read `note` for capped queries or missing indexes.
- Both searches require a candidate index and the server's Ollama embedding
  model. Cline's conversational model can be local and is configured separately.
  `get_symbol_context` works without an index or Ollama; call it directly for
  a known location.
- A missing index: inspect `get_index_status`. Build/refresh with `index_codebase`
  when needed and authorized by the task. Existing authorization still applies.
- An invalid argument: correct the path, range, or type before retrying.
  An outdated location: use exact search to find the symbol's current line.
- No useful hits: check index state, broaden filters, or rephrase the query.
  Empty results do not prove the code is absent.

## Verify before editing

1. Inspect a promising location with `get_symbol_context`, whether it came
   from semantic search, exact search, or the user.
2. Read the live source with Cline's available file-reading tool before
   concluding behavior or editing. Stored snippets and line numbers may be stale.
3. Treat scores as similarity, not confidence. `references` are capped lexical
   identifier matches outside the definition, including comments/strings;
   they do not prove callers or callees. `relatedTests` is the test-like subset
   of returned references; an empty list does not prove there are no tests.
4. `indexState` reports this file's stored hash: `current`, `stale`, or
   `not_indexed`. Live source is authoritative in every state. Use exact search
   when you need complete references or relationships.
5. After edits, check affected references and run relevant tests/build checks.
   Refresh the index when further semantic discovery needs the changed source.

The three tools above are read-only. Cline's configured approval settings
control execution. `index_codebase` writes index state and is not included in
this project's read-only auto-approval list.
