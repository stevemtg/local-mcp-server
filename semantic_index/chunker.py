"""Structure-aware source chunking.

Extracts semantic blocks (functions / classes / methods / interfaces) using
language-specific heuristics:

* Python  -> indentation-based block extraction.
* JS/TS/Go/Rust/Java/Kotlin/C/C++/C# -> regex header detection with
  brace matching (comments & string literals are masked before counting).
* Anything unparsable -> fixed line-window fallback chunking.

Line numbers returned are 1-based and inclusive.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .config import FALLBACK_CHUNK_LINES, FALLBACK_CHUNK_OVERLAP, MAX_CHUNK_LINES

logger = logging.getLogger("semantic_index.chunker")


@dataclass
class CodeChunk:
    """One semantic unit of source code with its (1-based, inclusive) range."""

    content: str
    start_line: int
    end_line: int
    kind: str  # "function" | "class" | "method" | "block"
    name: str = ""


@dataclass
class _Block:
    start: int  # 0-based first line of the block
    end: int  # 0-based last line of the block (inclusive)
    kind: str
    name: str


@dataclass
class _Start:
    line: int  # 0-based header line
    indent: int
    kind: str
    name: str


_PY_COMMENT = ("#",)
_CURLY_COMMENT = ("//", "/*", "*")

_PY_DEF = re.compile(r"(?P<indent>[ \t]*)(?:async\s+)?def\s+(?P<name>\w+)")
_PY_CLASS = re.compile(r"(?P<indent>[ \t]*)class\s+(?P<name>\w+)")

_JS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)"), "function"),
    (re.compile(r"(?:export\s+)?(?:abstract\s+|final\s+)?(?:class|interface|enum|type)\s+(?P<name>[A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"(?:export\s+)(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)"), "function"),
    (re.compile(r"^(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)"), "function"),
    (re.compile(r"(?P<name>[A-Za-z_$][\w$]*)\s*\([^;{]*\)\s*\{?\s*$"), "method"),
]

_GO_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s*)?(?P<name>\w+)\s*(?:\[[^\]]+\])?\s*\("), "function"),
    (re.compile(r"^type\s+(?P<name>\w+)\s+(?:struct|interface)"), "class"),
]

_RUST_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:^|\s)(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+)?fn\s+(?P<name>\w+)"), "function"),
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:#!?\[.*\]\s*)*(?:struct|enum|trait|impl|mod)\s+(?P<name>\w+)"), "class"),
]

_JVM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*(?:@\w+\s+)*(?:public|private|protected|abstract|final|static|sealed)?\s*(?:class|interface|enum|record)\s+(?P<name>\w+)"), "class"),
    (re.compile(r"^\s*(?:@\w+\s+)*(?:public|private|protected|static|final|abstract|synchronized|default|override)\s+[\w<>\[\].]+\s+(?P<name>\w+)\s*\([^)]*\)\s*\{?\s*$"), "method"),
]

_C_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+(?P<name>[A-Za-z_]\w*)"), "class"),
    (re.compile(r"^(?![#/])[A-Za-z_][\w\s\*&:<>,\[\]]*?\s\*?(?P<name>[A-Za-z_]\w*)\s*\([^;{)]*\)\s*(?:const\s*)?(?:override\s*)?\{?\s*$"), "function"),
]

_CSHARP_PATTERNS = _JVM_PATTERNS

_BRACE_LANGUAGES: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "javascript": _JS_PATTERNS,
    "typescript": _JS_PATTERNS,
    "go": _GO_PATTERNS,
    "rust": _RUST_PATTERNS,
    "java": _JVM_PATTERNS,
    "kotlin": _JVM_PATTERNS,
    "c": _C_PATTERNS,
    "cpp": _C_PATTERNS,
    "csharp": _CSHARP_PATTERNS,
}

_DECORATOR_PREFIXES = ("@", "#[")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _is_comment(stripped: str) -> bool:
    return stripped.startswith(_PY_COMMENT + _CURLY_COMMENT)


def _mask_line(line: str, state: dict) -> str:
    """Strip comments and string-literal contents from one logical line."""
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if state["comment"]:
            if line.startswith("*/", i):
                state["comment"] = False
                i += 2
            else:
                i += 1
        elif state["string"] is not None:
            q = state["string"]
            if c == "\\":
                i += 2
                continue
            # JS template interpolation: skip its contents naively.
            if q == "`" and line.startswith("${", i):
                depth, i = 1, i + 2
                while i < n and depth:
                    if line[i] == "{":
                        depth += 1
                    elif line[i] == "}":
                        depth -= 1
                    i += 1
                continue
            if c == q:
                state["string"] = None
                i += 1
            else:
                i += 1
        else:
            two = line[i : i + 2]
            if two == "//":
                break
            if two == "/*":
                state["comment"] = True
                i += 2
            elif c in ("'", '"'):
                state["string"] = c
                i += 1
            elif c == "`":
                state["string"] = "`"
                i += 1
            else:
                out.append(c)
                i += 1
    return "".join(out)


def _mask_source(lines: list[str]) -> list[str]:
    state = {"comment": False, "string": None}
    return [_mask_line(line, state) for line in lines]


def _extend_up(lines: list[str], header_line: int, max_up: int) -> int:
    """Extend a block start upward over comments/decorators attached to it."""
    j = header_line
    while j > max_up:
        k = j - 1
        while k > max_up and not lines[k].strip():
            k -= 1
        stripped = lines[k].strip()
        if _is_comment(stripped) or stripped.startswith(_DECORATOR_PREFIXES):
            j = k
        else:
            break
    return j


def _find_brace_end(masked: list[str], header_line: int) -> int:
    """Return the 0-based line of the closing brace, or -1 if not a block."""
    depth = 0
    started = False
    for idx in range(header_line, len(masked)):
        code = masked[idx]
        if not started:
            pos = code.find("{")
            semicolon = code.find(";")
            if pos == -1 or (semicolon != -1 and semicolon < pos):
                return -1  # declaration, not a definition block
            started = True
            depth = code[pos:].count("{") - code[pos:].count("}")
        else:
            depth += code.count("{") - code.count("}")
        if started and depth <= 0:
            return idx
    return len(masked) - 1


def _find_py_end(lines: list[str], header_line: int, indent: int) -> int:
    """Python block end: first following non-blank line at indent <= own."""
    for idx in range(header_line + 1, len(lines)):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        if _indent_of(lines[idx]) <= indent:
            return idx - 1
    return len(lines) - 1


def _split_large_block(
    lines: list[str], block: _Block, inner: list[_Start]
) -> list[_Block]:
    """Split an oversized block into per-method chunks where possible."""
    inner = [s for s in inner if block.start < s.line <= block.end]
    if not inner:
        return []
    pieces: list[_Block] = []
    prev_end = block.start
    for pos, s in enumerate(inner):
        start = _extend_up(lines, s.line, prev_end)
        end = inner[pos + 1].line - 1 if pos + 1 < len(inner) else block.end
        pieces.append(_Block(start, end, s.kind, s.name))
        prev_end = end + 1
    # Header piece: from block start up to the first method's (raw) start.
    header_end = inner[0].line - 1
    if header_end >= block.start:
        pieces.insert(
            0, _Block(block.start, header_end, block.kind, block.name)
        )
    return pieces


def _window_split(block: _Block) -> list[_Block]:
    """Fixed line-window split for oversized blocks with no inner methods."""
    pieces: list[_Block] = []
    total = block.end - block.start + 1
    for offset in range(0, total, MAX_CHUNK_LINES):
        s = block.start + offset
        e = min(block.start + offset + MAX_CHUNK_LINES - 1, block.end)
        suffix = f" (part {offset // MAX_CHUNK_LINES + 1})" if total > MAX_CHUNK_LINES else ""
        pieces.append(_Block(s, e, block.kind, block.name + suffix))
    return pieces





# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def _python_blocks(text: str) -> list[_Block]:
    lines = text.splitlines()
    starts: list[_Start] = []
    for idx, line in enumerate(lines):
        for pattern, kind in ((_PY_DEF, "function"), (_PY_CLASS, "class")):
            m = pattern.search(line)
            if m:
                indent = len(m.group("indent"))
                name = m.group("name")
                if kind == "function" and indent > 0:
                    kind = "method"
                starts.append(_Start(idx, indent, kind, name))
                break
    if not starts:
        return []
    blocks: list[_Block] = []
    pos = 0
    while pos < len(starts):
        s = starts[pos]
        if s.indent != 0:  # module-level only; nested defs are absorbed
            pos += 1
            continue
        raw_end = _find_py_end(lines, s.line, s.indent)
        block = _Block(_extend_up(lines, s.line, 0), raw_end, s.kind, s.name)
        if block.end - block.start + 1 > MAX_CHUNK_LINES:
            split = _split_large_block(lines, block, starts) or _window_split(block)
            blocks.extend(split)
        else:
            blocks.append(block)
        # Skip nested starts absorbed by this (unsplit) block.
        nxt = pos + 1
        while nxt < len(starts) and starts[nxt].line <= block.end:
            nxt += 1
        pos = nxt
    return blocks


def _brace_blocks(text: str, language: str) -> list[_Block]:
    patterns = _BRACE_LANGUAGES[language]
    lines = text.splitlines()
    masked = _mask_source(lines)
    starts: list[_Start] = []
    for idx, line in enumerate(masked):
        for pattern, kind in patterns:
            m = pattern.search(line)
            if m:
                starts.append(
                    _Start(idx, _indent_of(lines[idx]), kind, m.group("name"))
                )
                break
    if not starts:
        return []
    blocks: list[_Block] = []
    pos = 0
    while pos < len(starts):
        s = starts[pos]
        if _indent_of(lines[s.line]) > 0:  # top-level headers only
            pos += 1
            continue
        end = _find_brace_end(masked, s.line)
        if end == -1 or end < s.line:
            pos += 1
            continue
        block = _Block(_extend_up(lines, s.line, 0), end, s.kind, s.name)
        if block.end - block.start + 1 > MAX_CHUNK_LINES:
            split = _split_large_block(lines, block, starts) or _window_split(block)
            blocks.extend(split)
        else:
            blocks.append(block)
        nxt = pos + 1
        while nxt < len(starts) and starts[nxt].line <= block.end:
            nxt += 1
        pos = nxt
    return blocks


def _window_chunks(text: str) -> list[_Block]:
    lines = text.splitlines()
    blocks: list[_Block] = []
    step = max(FALLBACK_CHUNK_LINES - FALLBACK_CHUNK_OVERLAP, 1)
    total = len(lines)
    offset = 0
    part = 1
    while offset < total:
        end = min(offset + FALLBACK_CHUNK_LINES - 1, total - 1)
        blocks.append(_Block(offset, end, "block", f"lines-{offset + 1}-{end + 1}"))
        offset += step
        part += 1
        if end == total - 1:
            break
    return blocks


def chunk_code(text: str, language: str) -> list[CodeChunk]:
    """Split source ``text`` into semantic ``CodeChunk`` units (best effort).

    Any failure is swallowed by the caller (indexer) which logs and skips;
    an unparsable file simply falls back to fixed line-window chunks.
    """
    lines = text.splitlines()
    if language == "python":
        blocks = _python_blocks(text)
    elif language in _BRACE_LANGUAGES:
        blocks = _brace_blocks(text, language)
    else:
        blocks = []
    if not blocks:
        blocks = _window_chunks(text)
    chunks: list[CodeChunk] = []
    for b in blocks:
        content = "\n".join(lines[b.start : b.end + 1])
        if not content.strip():
            continue
        chunks.append(
            CodeChunk(
                content=content,
                start_line=b.start + 1,
                end_line=b.end + 1,
                kind=b.kind,
                name=b.name,
            )
        )
    return chunks


def embed_text(chunk: CodeChunk, rel_path: str, language: str) -> str:
    """Build the text actually handed to the embedding model."""
    header = f"File: {rel_path} | {language} | lines {chunk.start_line}-{chunk.end_line}"
    if chunk.name:
        header += f" | {chunk.kind} {chunk.name}"
    return f"{header}\n{chunk.content}"

