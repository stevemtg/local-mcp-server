"""Read-only, symbol-level context packs built from live on-disk source.

Purely analytical: never embeds, never touches the network, never writes to
the index. The live file on disk is always authoritative; the stored index is
consulted only through the read-only freshness lookup in store.py. The
returned references are lexical identifier matches, never proven callers or
callees.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .chunker import chunk_code
from .config import (
    CONTEXT_IMPORTS_MAX,
    CONTEXT_PREVIEW_MAX_CHARS,
    CONTEXT_SNIPPET_MAX_LINES,
    MAX_REFERENCES_CAP,
)
from .crawler import classify_language, hash_file, iter_source_files, read_text
from .store import read_only_indexed_hash

logger = logging.getLogger("semantic_index.symbol_context")

NOTE = (
    "Live source is authoritative. References are lexical matches, not proven "
    "callers or callees."
)

# Fallback window chunk names from the chunker ('lines-1-80') mean 'unnamed'.
_GENERIC_NAME_RE = re.compile(r"^lines-\d+-\d+$")

_TEST_DIR_PARTS = frozenset({"test", "tests", "spec", "specs", "__tests__"})

# Best-effort import/include/use statement patterns per language family.
_IMPORT_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^[ \t]*(?:from[ \t]+[\w.]+[ \t]+)?import[ \t]+\S"),
    "javascript": re.compile(r"^[ \t]*import\b|^[ \t]*export\b.*\bfrom\b"),
    "typescript": re.compile(r"^[ \t]*import\b|^[ \t]*export\b.*\bfrom\b"),
    "go": re.compile(r"^[ \t]*import\b"),
    "rust": re.compile(r"^[ \t]*(?:pub[ \t]+)?use\b"),
    "java": re.compile(r"^[ \t]*import\b"),
    "kotlin": re.compile(r"^[ \t]*import\b"),
    "c": re.compile(r"^[ \t]*#[ \t]*include\b"),
    "cpp": re.compile(r"^[ \t]*#[ \t]*include\b"),
    "csharp": re.compile(r"^[ \t]*using\b"),
}
_DEFAULT_IMPORT_RE = re.compile(r"^[ \t]*(?:import|use)\b|^[ \t]*#[ \t]*include\b")


def _is_test_path(rel_path: str) -> bool:
    """True for test-like relative paths (test/spec dirs or file naming)."""
    parts = Path(rel_path).parts
    if any(part in _TEST_DIR_PARTS for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    stem = Path(name).stem
    return (
        stem.startswith("test_")
        or stem.endswith("_test")
        or ".test." in name
        or ".spec." in name
    )


def build_symbol_context(
    root: Path, path: str, line: int, max_references: int
) -> dict:
    """Build the context pack for the live symbol at ``path:line``.

    ``path`` must be project-relative (absolute paths and root escapes are
    rejected); ``line`` is 1-based and must exist in the current file.
    """
    p = Path(path)
    if p.is_absolute() or p.drive:
        raise ValueError("path must be project-relative (absolute paths are rejected)")
    target = (root / p).resolve()
    if root not in target.parents and target != root:
        raise ValueError(f"Path escapes project root: {path}")
    if not target.is_file():
        raise ValueError(f"File not found: {path}")

    rel = target.relative_to(root).as_posix()
    language = classify_language(target)
    text = read_text(target)
    lines = text.splitlines()
    if not 1 <= line <= len(lines):
        raise ValueError(f"Line {line} outside file (valid: 1-{len(lines)})")
    limit = max(1, min(int(max_references), MAX_REFERENCES_CAP))

    # -- symbol selection (live source only) ---------------------------------
    chunks = chunk_code(text, language)
    matches = [c for c in chunks if c.start_line <= line <= c.end_line]
    if matches:
        # Smallest range wins; named blocks beat generic ones on a tie.
        chosen = min(
            matches,
            key=lambda c: (c.end_line - c.start_line, 0 if c.kind != "block" else 1),
        )
        raw_name = chosen.name
        kind = chosen.kind
        content = chosen.content
        sym_start, sym_end = chosen.start_line, chosen.end_line
    else:
        half = CONTEXT_SNIPPET_MAX_LINES // 2
        sym_start = max(1, min(line - half, len(lines)))
        sym_end = min(len(lines), sym_start + CONTEXT_SNIPPET_MAX_LINES - 1)
        raw_name, kind = "", "block"
        content = "\n".join(lines[sym_start - 1 : sym_end])
    name = raw_name if raw_name and not _GENERIC_NAME_RE.match(raw_name) else None

    declaration: str | None = None
    for raw in content.splitlines():
        if raw.strip():
            declaration = raw.strip()[:CONTEXT_PREVIEW_MAX_CHARS]
            break

    # -- bounded snippet (centered on the requested line when symbol is big) --
    if sym_end - sym_start + 1 > CONTEXT_SNIPPET_MAX_LINES:
        half = CONTEXT_SNIPPET_MAX_LINES // 2
        snip_start = max(
            sym_start, min(line - half, sym_end - CONTEXT_SNIPPET_MAX_LINES + 1)
        )
        snip_end = min(sym_end, snip_start + CONTEXT_SNIPPET_MAX_LINES - 1)
    else:
        snip_start, snip_end = sym_start, sym_end
    snippet_text = "\n".join(lines[snip_start - 1 : snip_end])

    # -- imports (best-effort, never raises on unusual syntax) -----------------
    imports: list[dict] = []
    import_re = _IMPORT_PATTERNS.get(language, _DEFAULT_IMPORT_RE)
    for idx, raw in enumerate(lines, 1):
        if len(imports) >= CONTEXT_IMPORTS_MAX:
            break
        if import_re.search(raw):
            imports.append(
                {"line": idx, "text": raw.strip()[:CONTEXT_PREVIEW_MAX_CHARS]}
            )

    # -- lexical references (exact identifier matches, deterministic order) ----
    references: list[dict] = []
    if name:
        ref_re = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
        hits: list[tuple[str, int, str]] = []
        for cf in iter_source_files(root):
            try:
                file_lines = read_text(cf.path).splitlines()
            except OSError as exc:  # unreadable snapshot race; skip the file
                logger.warning("Skipping unreadable file %s: %s", cf.path, exc)
                continue
            for idx, raw in enumerate(file_lines, 1):
                if cf.rel_path == rel and sym_start <= idx <= sym_end:
                    continue  # exclude the symbol's own definition range
                if ref_re.search(raw):
                    hits.append(
                        (cf.rel_path, idx, raw.strip()[:CONTEXT_PREVIEW_MAX_CHARS])
                    )
        hits.sort(key=lambda h: (h[0], h[1]))
        references = [
            {"path": h[0], "line": h[1], "preview": h[2]} for h in hits[:limit]
        ]

    # -- related tests (grouped subset of the references above) ----------------
    grouped: dict[str, list[int]] = {}
    for ref in references:
        if _is_test_path(ref["path"]):
            grouped.setdefault(ref["path"], []).append(ref["line"])
    related_tests = [
        {
            "path": tp,
            "referenceLines": sorted(ls),
            "reason": "Exact symbol reference in a test-like path",
        }
        for tp, ls in sorted(grouped.items())
    ]

    # -- index freshness (read-only lookup; never creates the index) -----------
    stored_hash = read_only_indexed_hash(root, rel)
    if stored_hash is None:
        index_state = "not_indexed"
    else:
        index_state = "current" if stored_hash == hash_file(target) else "stale"

    return {
        "directory": str(root),
        "path": rel,
        "absolutePath": str(target),
        "requestedLine": line,
        "language": language,
        "indexState": index_state,
        "symbol": {
            "name": name,
            "kind": kind,
            "startLine": sym_start,
            "endLine": sym_end,
            "declaration": declaration,
        },
        "snippet": {"startLine": snip_start, "endLine": snip_end, "text": snippet_text},
        "imports": imports,
        "references": references,
        "relatedTests": related_tests,
        "note": NOTE,
    }
