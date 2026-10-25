"""Static relationship extraction from live source files (no embeddings, no network).

Python files are parsed with the built-in ``ast`` module; every other indexed
language goes through a line-oriented regex/token scanner. Extraction is
heuristic by design — the caller-facing note always states this and the live
source stays authoritative. Extractors NEVER raise to their caller: a file that
cannot be parsed yields a clean diagnostic string instead.
"""

from __future__ import annotations

import ast
import io
import logging
import os
import re
import stat
import tokenize
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import (
    EDGE_CONTEXT_MAX_CHARS,
    IGNORED_DIRS,
    INDEXED_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
    MAX_SCAN_FILES,
)

logger = logging.getLogger("code_graph.extractors")


@dataclass(frozen=True)
class ScannedFile:
    """One source file discovered by the structural sweep."""

    path: Path  # absolute
    rel_path: str  # posix path relative to the project root (node-key namespace)
    language: str
    size: int
    mtime: float


@dataclass(frozen=True)
class SymbolDef:
    """A named structural unit (class/function/method) with its line range."""

    qualified: str  # canonical node key, e.g. 'pkg.mod.Class' or 'Class.method'
    kind: str  # 'class' | 'function' | 'method' | 'struct' | 'trait'
    start_line: int
    end_line: int


@dataclass(frozen=True)
class RawEdge:
    """One raw relationship extracted from one file."""

    source: str  # node key: file rel_path or qualified symbol
    target: str  # node key (file) or dotted/bare symbol name
    edge_type: str  # 'imports' | 'inherits' | 'calls' | 'attaches'
    file: str  # project-relative path the edge was extracted from
    line: int
    context: str  # trimmed source line the edge came from


@dataclass
class FileAnalysis:
    """Per-file extraction outcome; ``error`` carries parse diagnostics."""

    rel_path: str
    language: str
    symbols: list[SymbolDef] = field(default_factory=list)
    edges: list[RawEdge] = field(default_factory=list)
    error: str | None = None


def classify_language(path: Path) -> str:
    """Map a file extension onto the language identifier used by extractors."""
    ext = path.suffix.lower()
    return {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".kt": "kotlin",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".hpp": "cpp",
        ".cs": "csharp",
    }.get(ext, "unknown")


def read_text(path: Path) -> str:
    """Bound the actual read, including files that grow after discovery."""
    if not path.is_file():
        raise ValueError(f"Not a regular source file: {path}")
    with path.open("rb") as fh:
        data = fh.read(MAX_FILE_SIZE_BYTES + 1)
    if len(data) > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"File exceeds the {MAX_FILE_SIZE_BYTES}-byte source limit")
    encoding = "utf-8-sig"
    if path.suffix.lower() in (".py", ".pyi"):
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding, errors="replace")


def iter_source_files(root: Path, report=None):
    """Generate every structurally scannable file under ``root``.

    A generator (not a list) so huge trees stream through the sweep without
    holding every path at once; unreadable/oversized files are skipped with a
    log line, never an exception.
    """
    root = root.resolve()
    def diagnostic(path, message):
        logger.warning("%s: %s", path, message)
        if report is not None:
            report(str(path), message)

    count = 0
    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=False,
        onerror=lambda exc: diagnostic(exc.filename, str(exc)),
    ):
        allowed = []
        for name in dirnames:
            if name in IGNORED_DIRS or name.startswith("."):
                continue
            directory = Path(dirpath) / name
            try:
                if directory.is_symlink() or directory.resolve() != directory.absolute():
                    diagnostic(directory, "Skipped linked directory")
                    continue
                allowed.append(name)
            except (OSError, RuntimeError) as exc:
                diagnostic(directory, f"Unreadable directory: {exc}")
        dirnames[:] = sorted(allowed)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() not in INDEXED_EXTENSIONS:
                continue
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    diagnostic(path, "Skipped source link outside the scan policy")
                    continue
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    diagnostic(path, "Skipped non-regular file")
                    continue
            except (OSError, RuntimeError) as exc:
                diagnostic(path, f"Unreadable file: {exc}")
                continue
            if info.st_size > MAX_FILE_SIZE_BYTES:
                diagnostic(path, "Skipped oversized file")
                continue
            count += 1
            if count > MAX_SCAN_FILES:
                diagnostic(path, f"Scan file cap ({MAX_SCAN_FILES}) reached; graph is partial")
                return
            yield ScannedFile(
                path=path,
                rel_path=path.relative_to(root).as_posix(),
                language=classify_language(path),
                size=info.st_size,
                mtime=info.st_mtime,
            )


def module_for(rel_path: str) -> str:
    """Dotted Python module name for a project-relative path ('a/b/c.py' -> 'a.b.c')."""
    head, _, file_part = rel_path.rpartition("/")
    stem = file_part.rsplit(".", 1)[0]
    parts = head.split("/") if head else []
    return ".".join(parts if stem == "__init__" else [*parts, stem])


# ---------------------------------------------------------------------------
# Python extractor (ast-based)
# ---------------------------------------------------------------------------

def _dotted_name(node, aliases: dict[str, str]) -> str:
    """Dotted identifier for a Name/Attribute/Subscript base, alias-resolved."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        head = _dotted_name(node.value, aliases)
        return f"{head}.{node.attr}" if head else node.attr
    if isinstance(node, ast.Subscript):  # generic base like Base[T]
        return _dotted_name(node.value, aliases)
    return ""


def extract_python(rel_path: str, text: str, module_paths: dict[str, str]) -> FileAnalysis:
    """Extract lexically scoped Python definitions, imports and calls."""
    analysis = FileAnalysis(rel_path=rel_path, language="python")
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        analysis.error = f"python parse failed: {exc}"
        return analysis

    lines = text.splitlines()
    module = module_for(rel_path)
    pkg_parts = module.split(".") if Path(rel_path).stem == "__init__" else module.split(".")[:-1]

    def context_for(lineno: int) -> str:
        if 1 <= lineno <= len(lines):
            return lines[lineno - 1].strip()[:EDGE_CONTEXT_MAX_CHARS]
        return ""

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scope = module
            self.kind = "module"
            self.class_scope = ""
            self.aliases: dict[str, str] = {}

        def edge(self, target, kind, node, source=None):
            if target:
                analysis.edges.append(RawEdge(
                    source or (self.scope if self.kind != "module" else rel_path),
                    target, kind, rel_path, node.lineno, context_for(node.lineno),
                ))

        def visit_Import(self, node):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                self.aliases[local] = alias.name if alias.asname else local
                self.edge(module_paths.get(alias.name, alias.name), "imports", node, rel_path)

        def visit_ImportFrom(self, node):
            if node.level:
                keep = max(len(pkg_parts) - (node.level - 1), 0)
                base = ".".join(pkg_parts[:keep])
                dotted = f"{base}.{node.module}" if node.module and base else (node.module or base)
            else:
                dotted = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    self.edge(module_paths.get(dotted, dotted), "imports", node, rel_path)
                    continue
                target_dotted = f"{dotted}.{alias.name}" if dotted else alias.name
                self.aliases[alias.asname or alias.name] = target_dotted
                self.edge(module_paths.get(target_dotted, target_dotted), "imports", node, rel_path)

        def definition(self, node, kind):
            key = f"{self.scope}.{node.name}" if self.scope else node.name
            self.aliases[node.name] = key
            analysis.symbols.append(SymbolDef(key, kind, node.lineno, node.end_lineno or node.lineno))
            for decorator in node.decorator_list:
                self.visit(decorator)
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    self.edge(_dotted_name(base, self.aliases), "inherits", node, key)
                    self.visit(base)
            else:
                # Defaults execute in the enclosing scope, not the function body.
                for default in [*node.args.defaults, *node.args.kw_defaults]:
                    if default is not None:
                        self.visit(default)
            previous = self.scope, self.kind, self.class_scope, self.aliases
            self.scope, self.kind = key, kind
            self.aliases = dict(self.aliases)
            if kind == "class":
                self.class_scope = key
            else:
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                            *([node.args.vararg] if node.args.vararg else []),
                            *([node.args.kwarg] if node.args.kwarg else [])]:
                    self.aliases.pop(arg.arg, None)
                if kind == "method":
                    self.aliases.update(self=self.class_scope, cls=self.class_scope)
            for child in node.body:
                self.visit(child)
            self.scope, self.kind, self.class_scope, self.aliases = previous

        def visit_ClassDef(self, node):
            self.definition(node, "class")

        def visit_FunctionDef(self, node):
            self.definition(node, "method" if self.kind == "class" else "function")

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            self.edge(_dotted_name(node.func, self.aliases), "calls", node)
            self.generic_visit(node)

    Visitor().visit(tree)
    return analysis


# ---------------------------------------------------------------------------
# Brace-language extractors (regex/token scan, best-effort by design)
# ---------------------------------------------------------------------------

_IMPORT_RES: dict[str, re.Pattern[str]] = {
    "csharp": re.compile(r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;"),
    "java": re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;"),
    "kotlin": re.compile(r"^\s*import\s+([\w.]+)"),
    "javascript": re.compile("""from\\s+["']([^"']+)["']|^\\s*import\\s+["']([^"']+)["']"""),
    "typescript": re.compile("""from\\s+["']([^"']+)["']|^\\s*import\\s+["']([^"']+)["']"""),
    "go": re.compile("""(?:import|package)\\s+(?:\\w+\\s+)?"([^"]+)"|^\\s*package\\s+(\\w+)"""),
    "rust": re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+)"),
    "c": re.compile(r'#\s*include\s*[<"]([^">]+)[>"]'),
}
_HEADER_RES: dict[str, list[tuple[re.Pattern[str], str]]] = {
    "csharp": [
        (re.compile(r"\b(?:class|struct|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*(?::\s*(?P<bases>[^\r\n{;]+))?"), "class"),
        (re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(?:public|private|protected|internal)\b[^=;{]*\b([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*\{?"), "method"),
    ],
    "java": [
        (re.compile(r"\b(?:class|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*(?:(?:extends|implements)\s+(?P<bases>[^\r\n{]+))?"), "class"),
        (re.compile(r"^\s*(?:public|private|protected|static|abstract|final|override)\b[^=;{]*\b([A-Za-z_]\w*)\s*\([^)]*\)\s*\{?"), "method"),
    ],
    "kotlin": [
        (re.compile(r"\b(?:class|interface|object|enum)\s+(?P<name>[A-Za-z_]\w*)\s*(?::\s*(?P<bases>[^\r\n{]+))?"), "class"),
        (re.compile(r"^\s*(?:(?:override|open|suspend|inline|private|internal|public)\s+)*fun\s+([A-Za-z_]\w*)\s*\("), "method"),
    ],
    "javascript": [
        (re.compile(r"\bclass\s+(?P<name>[A-Za-z_$][\w$]*)(?:\s+extends\s+(?P<bases>[A-Za-z_$][\w$.]*))?"), "class"),
        (re.compile(r"\binterface\s+(?P<name>[A-Za-z_$][\w$]*)(?:\s+extends\s+(?P<bases>[A-Za-z_$][\w$.]*))?"), "class"),
        (re.compile(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*\("), "function"),
        (re.compile(r"(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>)"), "function"),
        (re.compile(r"^\s*(?:(?:public|private|protected|static|async)\s+)*(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::[^={]+)?\s*\{"), "method"),
    ],
    "go": [
        (re.compile(r"^type\s+(?P<name>\w+)\s+(?:struct|interface)\b"), "class"),
        (re.compile(r"^func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s*)?(?P<name>\w+)\s*(?:\[|\()"), "function"),
    ],
    "rust": [
        (re.compile(r"\b(?:struct|enum|trait|mod)\s+(?P<name>[A-Za-z_]\w*)"), "class"),
        (re.compile(r"\bimpl(?:\s*<[^>]*>)?\s+(?P<name>[A-Za-z_]\w*)"), "class"),
        (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>[A-Za-z_]\w*)"), "function"),
    ],
    "c": [
        (re.compile(r"\b(?:struct|union|enum)\s+([A-Za-z_]\w*)\b"), "class"),
        (re.compile(r"^\s*(?:[\w:*&<>]+\s+)+(?P<name>\w+)\s*\([^;]*\)\s*(?:const\s*)?\{"), "function"),
    ],
}
_HEADER_RES["typescript"] = _HEADER_RES["javascript"]
_HEADER_RES["cpp"] = [
    (re.compile(r"\b(?:class|struct)\s+(?P<name>\w+)\s*(?::\s*(?P<bases>[^\r\n{;]+))?"), "class"),
    *_HEADER_RES["c"],
]
_IMPORT_RES["cpp"] = _IMPORT_RES["c"]

_CALL_RES: list[re.Pattern[str]] = [
    re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*(?:(?:\.|->|::)[A-Za-z_$][\w$]*)*)\s*(?:<[^;(){}]*>)?\s*\("),
]
_ATTACH_RES: list[re.Pattern[str]] = [
    re.compile(r"AddComponent\s*<\s*([A-Za-z_][\w.:]*)\s*>\s*\("),
    re.compile(r"AddComponent\s*\(\s*typeof\s*\(\s*([A-Za-z_][\w.:]*)\s*\)\s*\)"),
    re.compile(r"CreateDefaultSubobject\s*<\s*([A-Za-z_][\w.:]*)\s*>"),
]

_NONCODE = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|@"(?:""|[^"])*"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
_CONTROL_WORDS = {"if", "for", "foreach", "while", "switch", "catch", "using", "lock",
                  "sizeof", "typeof", "nameof", "return", "new", "when"}


def _mask_noncode(text: str, keep_strings: bool = False) -> str:
    def mask(match):
        value = match.group()
        if keep_strings and not value.startswith(("//", "/*")):
            return value
        return "".join("\n" if char == "\n" else " " for char in value)
    return _NONCODE.sub(mask, text)



def extract_brace(rel_path: str, language: str, text: str) -> FileAnalysis:
    """Best-effort token scan with comment masking and brace-scoped ownership.

    This is not a compiler: macros, overloads, dynamic receivers and complex
    multiline declarations can remain unresolved. File-qualified keys avoid
    merging identically named types in different files.
    """
    analysis = FileAnalysis(rel_path=rel_path, language=language)
    header_res = _HEADER_RES.get(language, [])
    import_res = _IMPORT_RES.get(language)
    depth = 0
    # Each scope stores its qualified name, symbol index and opening depth.
    scopes: list[dict] = []
    raw_lines = text.splitlines()
    import_lines = _mask_noncode(text, keep_strings=True).splitlines()
    for lineno, code in enumerate(_mask_noncode(text).splitlines(), 1):
        context = raw_lines[lineno - 1].strip()[:EDGE_CONTEXT_MAX_CHARS]
        declaration_span = None
        if import_res is not None:
            imp = import_res.search(import_lines[lineno - 1])
            if imp:
                target = next((g for g in imp.groups() if g), "")
                if target:
                    analysis.edges.append(RawEdge(rel_path, target, "imports", rel_path, lineno, context))
                continue
        for pattern, kind in header_res:
            match = pattern.search(code)
            if not match:
                continue
            name = match.groupdict().get("name") or match.group(1)
            if name in _CONTROL_WORDS:
                continue
            declaration_span = match.span("name" if "name" in match.groupdict() else 1)
            parent = scopes[-1]["name"] if scopes else ""
            qualified = f"{parent}.{name}" if parent else name
            analysis.symbols.append(SymbolDef(qualified, kind, lineno, lineno))
            for base in re.split(r",|\bimplements\b", match.groupdict().get("bases") or ""):
                base = re.sub(r"\b(public|private|protected|virtual)\s+", "", base).strip()
                if base:
                    analysis.edges.append(RawEdge(qualified, base, "inherits", rel_path, lineno, context))
            scopes.append({"name": qualified, "index": len(analysis.symbols) - 1,
                           "depth": depth, "opened": "{" in code})
            break

        source = scopes[-1]["name"] if scopes else rel_path
        for pattern in _CALL_RES:
            for match in pattern.finditer(code):
                target = match.group(1)
                if target in _CONTROL_WORDS or (
                    declaration_span and match.start(1) == declaration_span[0]
                ):
                    continue
                if target.startswith(("this.", "this->", "self.")):
                    owner = next((s["name"] for s in reversed(scopes)
                                  if analysis.symbols[s["index"]].kind == "class"), "")
                    if owner:
                        target = f"{owner}.{target.replace('->', '.').split('.', 1)[1]}"
                analysis.edges.append(RawEdge(source, target, "calls", rel_path, lineno, context))
        for pattern in _ATTACH_RES:
            for match in pattern.finditer(code):
                analysis.edges.append(RawEdge(source, match.group(1), "attaches", rel_path, lineno, context))

        if scopes and "{" in code:
            scopes[-1]["opened"] = True
        depth += code.count("{") - code.count("}")
        while scopes and (
            (scopes[-1]["opened"] and depth <= scopes[-1]["depth"])
            or (not scopes[-1]["opened"] and ";" in code)
        ):
            closed = scopes.pop()
            index = closed["index"]
            analysis.symbols[index] = replace(analysis.symbols[index], end_line=lineno)
        depth = max(0, depth)
    for scope in scopes:
        index = scope["index"]
        analysis.symbols[index] = replace(analysis.symbols[index], end_line=max(len(raw_lines), 1))
    return analysis


def extract_file(sf: ScannedFile, module_paths: dict[str, str]) -> FileAnalysis:
    """Dispatch one file through the right extractor; never raises."""
    try:
        text = read_text(sf.path)
        if sf.language == "python":
            return extract_python(sf.rel_path, text, module_paths)
        return extract_brace(sf.rel_path, sf.language, text)
    except (OSError, ValueError, SyntaxError, LookupError, RecursionError) as exc:
        analysis = FileAnalysis(rel_path=sf.rel_path, language=sf.language)
        analysis.error = f"Source extraction failed: {exc}"
        return analysis

