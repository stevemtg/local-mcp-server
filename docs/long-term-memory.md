# Long-term memory for local models

This server can now retain decisions, user preferences, facts, lessons, tasks and
session handoffs across conversations and model/server restarts. Memories live in
`<directory_path>/.agent_memory/memory.db`, separate from the rebuildable code
indexes. All models and MCP clients that use the same root share those notes.
No Ollama, embeddings, GPU, network service or code index is needed.

The client must call the memory tools to save and recall information. The server
does not see the full conversation automatically, train model weights, or record
every message. Its MCP instructions encourage recall at task start and saving
useful durable knowledge. A reusable [Cline rule](cline-global-rules/30-long-term-memory.md)
provides the same workflow explicitly.

## Tools

Every tool accepts `directory_path`, an existing absolute directory. The first
successful `memory_remember` initializes storage; reads on a new project return
empty results without creating directories or a database.

| Tool | Required inputs besides directory_path | Optional inputs | Result |
| --- | --- | --- | --- |
| `memory_remember` | `key`, `title`, `content` | `category`, `tags`, `source`, `pinned` | Creates a durable note, or reports an unchanged retry or key conflict. |
| `memory_recall` | None | `query`, `category`, `tags`, `limit`, `offset` | Ranked previews, keys, revisions and provenance; an empty query lists pinned then recent notes. |
| `memory_get` | `key` | None | Full note and its current revision, or `not_found`. |
| `memory_update` | `key`, `expected_revision` | `title`, `content`, `category`, `tags`, `source`, `pinned` | Changes only supplied fields, or reports a revision conflict. |
| `memory_forget` | `key`, `expected_revision` | None | Deletes the note, tags and search entries; a missing key is `already_absent`. |

Categories are `fact` (default), `decision`, `preference`, `task`, `lesson`, and
`summary`. Keys are stable, case-sensitive identifiers, such as
`decisions/save-format`, `preferences/testing`, or `session/current`. Supported
characters are letters, digits, dots, underscores, colons, slashes and hyphens;
the first character must be a letter or digit. Keys are identifiers, not paths.

## Example: remember a decision

Call `memory_remember`:

```json
{
  "directory_path": "C:/work/game",
  "key": "decisions/save-format",
  "title": "Keep JSON save files",
  "content": "Keep saves in JSON so players can inspect and repair them. We rejected a binary format because human-readable saves are a project requirement.",
  "category": "decision",
  "tags": ["saving", "architecture"],
  "source": "User's save-system requirements",
  "pinned": true
}
```

Save rationale and evidence, not just a conclusion. `source` is caller-supplied
provenance; the server does not verify it. A note includes `created_at` and
`updated_at` UTC timestamps, `revision`, tags, category, source and pinned state.

In a later conversation, call `memory_recall`:

```json
{"directory_path":"C:/work/game","query":"save format JSON","limit":5}
```

Retrieval uses SQLite FTS5 with English stemming and matches any query word.
Titles, keys and tags have stronger ranking weights than body text. This is
full-text retrieval, not embedding-based semantic similarity: synonyms without
shared words may not match. Use concise keywords, tags, category filters or an
empty query when a search misses. Tags are case-insensitive and all supplied
tags must match. Pagination returns `has_more` and `next_offset`; concurrent
changes can shift offset-based pages, so do not treat a multi-page read as a
fixed snapshot.

Recall returns bounded `preview` text, with `content_truncated` when needed.
Use `memory_get` with a returned key to retrieve the full content.

## Corrections, handoffs and forgetting

Call `memory_get` before modifying a note, then pass its exact `revision` as
`expected_revision` to `memory_update`. For example, if the returned revision is 1:

```json
{
  "directory_path":"C:/work/game",
  "key":"decisions/save-format",
  "expected_revision":1,
  "content":"Keep JSON for saves. Compression is allowed if an export command produces readable JSON. This refines the earlier decision."
}
```

Omitted or null fields stay unchanged. `tags: []` clears tags, `source: ""`
clears provenance, and `pinned: false` unpins the note. At least one change field
is required. Updates replace the prior values; no history or hidden transcript
is retained. Use separate keys when two decisions both remain relevant.

Use `session/current` for a concise handoff: goal, completed work, unresolved
issues, relevant files, test results and next steps. Refresh it as work
progresses. A new session can recall it without retaining the old chat.

`memory_forget` also requires the current revision and operates on one key.
Deleted notes disappear from retrieval. It does not remove copies from client
conversations or backups and does not promise forensic erasure from storage.

## Durability and concurrency

- Writes commit notes, tags and search entries in one SQLite transaction. A
  failed transaction leaves the preceding state available.
- Repeating an identical `memory_remember` with the same key changes nothing.
  Different content under that key returns `conflict`, without overwriting.
- Updates/deletions compare revisions while holding the write transaction.
  Reread and reconcile conflicts; do not blindly retry with a newer revision.
- Revisions increase across the database, including when a forgotten key is
  recreated. Treat them as comparison tokens, not a per-note edit count.
- Connections close after each operation; work runs in AnyIO worker threads.
  SQLite waits up to ten seconds for locks, then returns a `memory_busy`
  diagnostic. Other storage failures also return structured diagnostics.
- Readers open existing databases read-only. Unsupported schema versions and
  unrelated databases are rejected without replacement. Linked storage paths
  are rejected. Keep the database on a local filesystem with SQLite locking.

The Python SQLite build must include FTS5; the project's tested Python 3.13
environment does. No new Python packages are needed. Notes are ordinary local
database content, not encrypted. `.agent_memory/` is excluded from this
repository's Git tracking and from the existing hidden-directory code scans.
For other projects, add `.agent_memory/` to their `.gitignore` as appropriate.
Back up the directory while clients/server are stopped.

Bounds: 128 characters per key, 240 per title, 16,000 per content, 2,000 per
source, and 20 tags of up to 64 characters each. Queries accept up to 1,000
characters and 32 distinct words. Recall defaults to five notes, with a maximum
of 20 and previews capped at 1,000 characters each. Nothing automatically expires.

## Enable the workflow in Cline

1. Restart this MCP server in Cline so it discovers the five `memory_*` tools.
2. Copy the [memory rule](cline-global-rules/30-long-term-memory.md) into your
   project's `.clinerules/` directory, or enable it as a global Cline rule.
3. Ask Cline to remember a concrete project decision. Start a new conversation
   and ask it to recall that decision using the same absolute project root.

Memory is scoped by directory, not model identity. To share preferences across
projects, select an existing dedicated directory as a shared memory root and
explicitly tell your client to use it for shared notes as well as each project's
root for project notes. The server never silently combines different roots.

Recalled notes are context, not trusted instructions or proof of current code.
Current user directions and live source take precedence. Do not store secrets,
unrelated personal data, or guesses presented as established facts.

Run `python -B dev/memory_test.py` for persistence, retrieval, concurrency,
rollback, isolation and MCP lifecycle tests. The regular offline test runner
includes this suite.
