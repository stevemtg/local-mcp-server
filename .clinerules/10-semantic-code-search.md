# Semantic Code Search Workflow

## Core policy

```text
semantic discovery → exact source verification → conclusion → edit → validation
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

## Verification is mandatory

- Semantic results are **candidate locations only**.
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
| Symbol names, imports, callers, references, routes, constants, error messages, exact strings | `search_files`/`rg`/symbol lookup |
| What the code actually does | `read_file` on the live source |
| Validation | tests, builds, linting, type checks |

Do not try to replace grep, symbol lookup, or source reading with embeddings.

## After edits

- Re-run relevant exact searches (callers/references of changed symbols).
- Run the project's tests / build / lint / typecheck that are affected.
- If edits invalidated the index for a file, re-run `index_codebase` if
  semantic discovery is needed again afterwards.

## Auto-approval

- Read-only tools (`semantic_search`, `semantic_search_similar`,
  `get_index_status`) are safe to auto-approve.
- `index_codebase` writes state into the project and is **not** auto-approved.

## Example target flow

User: "Fix the issue where users occasionally get logged out after refreshing their profile."

1. Implementation location unknown → call `semantic_search` with focused
   queries: "profile refresh flow", "session/auth state updates",
   "logout / session invalidation".
2. Take the candidate files/symbols returned.
3. Open the actual current files; use exact search for callers/references of
   session mutations and auth state updates.
4. Determine the real cause from verified source only.
5. Edit, then run tests/typecheck/build and re-check affected references.
