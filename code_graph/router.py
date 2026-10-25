"""Code-aware routing between semantic hits, graph traces and live interfaces."""

from __future__ import annotations

import os
from pathlib import Path

from .config import MAX_RELATION_LIMIT, NAVIGATE_LISTING_MAX
from .extractors import classify_language, extract_brace, extract_python, read_text
from .graph_store import GraphMemory

NOTE = ("Static heuristic snapshot; targetCandidates are possible bindings, not proven runtime "
        "calls. Rebuild after edits and verify against live source.")


def failure(code: str, message: str) -> dict:
    return {"status": "failed", "error": {"code": code, "message": message}}


class StructuralRouter:
    """Decoupled adapter; consumes existing semantic result dictionaries."""

    def __init__(self) -> None:
        self.memory = GraphMemory()

    @staticmethod
    def _root(directory_path: str) -> Path:
        root = Path(directory_path)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("directory_path must be an existing absolute directory")
        return root.resolve()

    @staticmethod
    def _path(root: Path, current_path: str) -> Path:
        path = (root / current_path).resolve()
        if not path.is_relative_to(root):
            raise ValueError("current_path must stay inside directory_path")
        return path

    def build(self, directory_path: str) -> dict:
        try:
            root = self._root(directory_path)
        except (ValueError, OSError, RuntimeError) as exc:
            return failure("invalid_directory", str(exc))
        try:
            return self.memory.build(root)
        except (OSError, ValueError, RuntimeError) as exc:
            return failure("graph_build_failed", str(exc))

    def query(self, directory_path: str, target_symbol: str,
              relationship_type: str = "dependencies", limit: int = MAX_RELATION_LIMIT) -> dict:
        try:
            root = self._root(directory_path)
        except (ValueError, OSError, RuntimeError) as exc:
            return failure("invalid_directory", str(exc))
        if relationship_type not in ("dependencies", "dependents", "hierarchy"):
            return failure("invalid_relationship_type", "Use dependencies, dependents, or hierarchy")
        if not target_symbol.strip() or not 1 <= limit <= MAX_RELATION_LIMIT:
            return failure("invalid_argument", f"Provide a symbol and a limit from 1 to {MAX_RELATION_LIMIT}")
        try:
            if not self.memory.is_built(root):
                return failure("graph_not_built", "Call codebase_build_relations on this directory first")
            graph = self.memory.load(root)
            candidates = graph.resolve(target_symbol)
            result = {
                "status": "ok" if candidates else "no_match",
                "target_symbol": target_symbol, "candidates": candidates[:limit],
                "candidatesTruncated": len(candidates) > limit,
                "builtAt": graph.built_at, "graphTruncated": graph.truncated,
                "limit": limit, "note": NOTE,
            }
            if candidates:
                result[relationship_type] = getattr(graph, relationship_type)(target_symbol, limit)
            return result
        except (OSError, ValueError, RuntimeError) as exc:
            return failure("graph_unreadable", f"{exc}. Run codebase_build_relations to rebuild")

    def navigate(self, directory_path: str, action: str, current_path: str,
                 max_lines: int = 200) -> dict:
        try:
            root = self._root(directory_path)
        except (ValueError, OSError, RuntimeError) as exc:
            return failure("invalid_directory", str(exc))
        try:
            if action == "list_dir":
                return self._list_dir(root, current_path)
            if action == "view_file_summary":
                return self._file_summary(root, current_path)
            # These Python utilities remain available to embedding applications.
            if action == "read_file":
                return self._read_file(root, current_path, max_lines)
            if action == "inspect_symbol":
                return self._inspect_symbol(root, current_path)
            return failure("invalid_action", "Use list_dir or view_file_summary")
        except (OSError, ValueError, RuntimeError, SyntaxError, LookupError) as exc:
            return failure("navigation_failed", str(exc))

    def _list_dir(self, root: Path, current_path: str) -> dict:
        base = self._path(root, current_path)
        entries = []
        truncated = False
        with os.scandir(base) as children:
            for child in children:
                if len(entries) == NAVIGATE_LISTING_MAX:
                    truncated = True
                    break
                entries.append({
                    "name": child.name,
                    "kind": "dir" if child.is_dir(follow_symlinks=False) else "file",
                })
        entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
        return {"status": "ok", "path": base.relative_to(root).as_posix(),
                "entries": entries, "truncated": truncated}

    def _file_summary(self, root: Path, current_path: str) -> dict:
        path = self._path(root, current_path)
        rel = path.relative_to(root).as_posix()
        language = classify_language(path)
        if language == "unknown":
            return failure("unsupported_language", f"No structural extractor for {path.suffix}")
        text = read_text(path)
        analysis = extract_python(rel, text, {}) if language == "python" else extract_brace(rel, language, text)
        if analysis.error:
            return failure("parse_failed", f"{rel}: {analysis.error}")
        lines = text.splitlines()
        symbols = [
            {"symbol": s.qualified, "kind": s.kind, "startLine": s.start_line,
             "endLine": s.end_line, "declaration": lines[s.start_line - 1].strip()[:200]}
            for s in analysis.symbols[:MAX_RELATION_LIMIT]
        ]
        imports = [{"target": e.target, "line": e.line}
                   for e in analysis.edges if e.edge_type == "imports"]
        return {
            "status": "ok", "path": rel, "language": language, "numLines": len(lines),
            "symbols": symbols, "imports": imports[:MAX_RELATION_LIMIT],
            "truncated": len(analysis.symbols) > MAX_RELATION_LIMIT or len(imports) > MAX_RELATION_LIMIT,
            "note": "Live source interfaces; non-Python declarations use a heuristic token scanner.",
        }

    def _read_file(self, root: Path, current_path: str, max_lines: int) -> dict:
        path = self._path(root, current_path)
        lines = read_text(path).splitlines()
        limit = max(1, min(max_lines, 2000))
        content = "\n".join(lines[:limit])
        return {
            "status": "ok", "path": path.relative_to(root).as_posix(),
            "numLines": len(lines), "linesShown": min(len(lines), limit),
            "content": content[:16000], "truncated": len(lines) > limit or len(content) > 16000,
        }

    def _inspect_symbol(self, root: Path, symbol: str) -> dict:
        if not self.memory.is_built(root):
            return failure("graph_not_built", "Call codebase_build_relations first")
        graph = self.memory.load(root)
        found = [k for k in graph.resolve(symbol) if k in graph.symbols]
        if len(found) != 1:
            return {"status": "no_match" if not found else "ambiguous",
                    "candidates": found[:MAX_RELATION_LIMIT],
                    "note": "Use a qualified symbol or file::symbol to select one definition."}
        key = found[0]
        meta = graph.symbols[key]
        path = self._path(root, meta["file"])
        lines = read_text(path).splitlines()
        return {
            "status": "ok", "symbol": key, **meta,
            "snippet": "\n".join(lines[meta["startLine"] - 1:meta["endLine"]])[:4000],
            "note": NOTE,
        }

    def enrich_results(self, directory_path: str, payload: dict, limit: int = 20) -> dict:
        """Attach traces to semantic_search/similar hits using their file and range.

        Does not read embeddings, create index.db, run a sweep, or call Ollama.
        Missing graphs preserve the original search response. Corrupt graphs
        report an optional diagnostic without breaking semantic search.
        """
        if not payload.get("results"):
            return payload
        try:
            root = self._root(directory_path)
            if not self.memory.is_built(root):
                return payload
            graph = self.memory.load(root)
            enriched = []
            for hit in payload["results"]:
                path = hit.get("path", "").replace("\\", "/")
                line = hit.get("startLine", 0)
                keys = graph.file_symbols.get(path, ())
                containing = [k for k in keys if graph.symbols[k]["startLine"] <= line <= graph.symbols[k]["endLine"]]
                if containing:
                    # The innermost containing definition is the most specific.
                    width = min(graph.symbols[k]["endLine"] - graph.symbols[k]["startLine"] for k in containing)
                    containing = [k for k in containing if graph.symbols[k]["endLine"] - graph.symbols[k]["startLine"] == width]
                candidates = containing or ([path] if path in graph.file_meta else [])
                traces = [{
                    "target_symbol": key, "dependencies": graph.dependencies(key, limit),
                    "dependents": graph.dependents(key, limit),
                    "hierarchy": graph.hierarchy(key, limit),
                } for key in candidates[:1]]
                enriched.append({**hit, "structural": {
                    "candidates": candidates[:MAX_RELATION_LIMIT], "traces": traces,
                    "builtAt": graph.built_at, "graphTruncated": graph.truncated, "note": NOTE,
                }})
            return {**payload, "results": enriched}
        except (OSError, ValueError, RuntimeError) as exc:
            return {**payload, "structuralDiagnostic": str(exc)}

