# Semantic code search

[Back to the README](../README.md)

## Workflow

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
[Cline rule](../.clinerules/10-semantic-code-search.md) use the same routing:

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

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama base URL |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model name |
| `EMBED_TIMEOUT_SECONDS` | `60` | Per-request timeout |
| `EMBED_MAX_RETRIES` | `3` | Retry attempts per embedding |
| `EMBED_CONCURRENCY` | `4` | Parallel embedding requests |
| `EMBED_NUM_GPU` | unset | Optional Ollama GPU-layer setting for embeddings; `0` requests CPU-only execution. |
| `MAX_FILE_SIZE_BYTES` | `307200` | Skip files larger than this |
| `MAX_CHUNK_LINES` | `180` | Split blocks longer than this |
| `MAX_CHUNK_CHARS` | `4000` | Maximum source characters per chunk, including newlines; embedding metadata is added separately |
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
- Embedding context limits depend on the model and its configuration. Source
  blocks are split at method boundaries where possible, then bounded by both
  `MAX_CHUNK_LINES` and `MAX_CHUNK_CHARS`, including fallback chunks and large
  methods. A single oversized line is split into fragments sharing its line
  number. The character limit is a heuristic, not a token count; lower it for
  models with smaller contexts or unusually token-dense source.
