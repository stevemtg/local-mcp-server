# Semantic Code Search Workflow

## Core policy

```text
semantic discovery → symbol context → exact source verification → conclusion → edit → validation
```

The semantic index (`semantic_search` / `semantic_search_similar` MCP tools) is
a **discovery aid only**. The live filesystem/source code is **always
authoritative**. If indexed content and the current source code disagree, the
current source code wins. The index may be stale.

## When to use semantic search

- Use `semantic_search` **early** when you do NOT know where a feature,
  behavior, bug, workflow, or architectural concept is implemented.
- Search by **meaning and intent**, not exact identifiers, e.g.
  "where is authentication state updated?", "code responsible for retrying
  failed payments", "where user permissions are enforced".
- Prefer **multiple focused queries** over one broad query when a single query
  is insufficient (decompose the concept into sub-questions).
- Use `semantic_search_similar(path, startLine, endLine)` to answer "where
  else is something implemented like this?".
- If the user already provided a specific filename, function/class/constant
  name, error message, route, config key, database field, or exact string,
  **prefer exact search** (`search_files`/`rg`/`read_file`, symbol lookup)
  directly — do not waste a semantic query on a known identifier.

## When to use symbol context

- Once an interesting source location is known, call
  `get_symbol_context(directory_path, path, line)` before concluding behavior
  or editing code. This applies whether the location came from semantic search,
  exact search, an error, or the user.
- Pass the absolute project root as `directory_path`, the project-relative path
  as `path`, and a 1-based line inside the relevant symbol as `line`.
- Use the result to orient the investigation: it provides a bounded live-source
  snippet, the containing symbol, imports, lexical references, related tests,
  and `indexState` (`current`, `stale`, or `not_indexed`).
- The returned snippet and references are bounded. Use `read_file` for omitted
  source and exact search when a complete reference list is required.
- `references` are exact lexical identifier matches, **not proven callers or
  callees**. Imports and symbol boundaries are best-effort. Verify important
  relationships with exact search or language-aware definition/reference tools.
- `get_symbol_context` reads the current file and works without Ollama or a
  current semantic index. A `stale` or `not_indexed` state affects later
  semantic discovery, not the authority of the live-source context it returns.

## Verification is mandatory

- Semantic results are **candidate locations only**.
- For a candidate source line, use `get_symbol_context` to gather focused
  live-source context before drawing conclusions or preparing an edit.
- Before concluding behavior or editing anything, **read the actual current
  source** at the candidate location with exact tools (`read_file`, `rg`,
  symbol/definition search).
- Verify important **callers, callees, imports, references, configuration
  keys, routes, and related symbols** with exact search — not embeddings.
- **Never modify code solely from an embedding/index result.**
- Before editing a file, always read its current version from disk.

## Division of responsibility

| Task | Tool class |
| --- | --- |
| Unknown location, concept-level discovery, "similar implementations" | `semantic_search` / `semantic_search_similar` |
| Focused live context for a known file and line | `get_symbol_context` |
| Symbol names, imports, callers, references, routes, constants, error messages, exact strings | `search_files`/`rg`/symbol lookup |
| What the code actually does | `read_file` on the live source |
| Validation | tests, builds, linting, type checks |

Do not try to replace grep, symbol lookup, or source reading with embeddings.

## After edits

- Re-run relevant exact searches (callers/references of changed symbols).
- Re-run `get_symbol_context` when useful to inspect the updated live symbol
  and its bounded reference/test candidates.
- Run the project's tests / build / lint / typecheck that are affected.
- If edits invalidated the index for a file, re-run `index_codebase` if
  semantic discovery is needed again afterwards. Until then,
  `get_symbol_context` may correctly report `indexState: stale`.

## Auto-approval

- Read-only tools (`semantic_search`, `semantic_search_similar`,
  `get_symbol_context`, `get_index_status`) are safe to auto-approve.
- `index_codebase` writes state into the project and is **not** auto-approved.

## Example target flow

User: "Fix the issue where users occasionally get logged out after refreshing their profile."

1. Implementation location unknown → call `semantic_search` with focused
   queries: "profile refresh flow", "session/auth state updates",
   "logout / session invalidation".
2. Take the candidate files/symbols returned.
3. Call `get_symbol_context` with the project root, candidate path, and a line
   inside the candidate range. Inspect its live symbol/snippet, imports,
   lexical references, related tests, and `indexState`.
4. Open the actual current files; use exact search for complete callers and
   references of session mutations and auth state updates. Do not treat the
   context tool's lexical matches as a proven call graph.
5. Determine the real cause from verified source only.
6. Edit, then run tests/typecheck/build and re-check affected references.
