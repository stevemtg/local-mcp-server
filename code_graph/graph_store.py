"""Bounded repository adjacency graph with independent, atomic JSON snapshots."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    GRAPH_DIR_NAME, GRAPH_FILE_NAME, MAX_CACHED_PROJECTS, MAX_DIAGNOSTICS,
    MAX_HIERARCHY_DEPTH, MAX_RELATION_LIMIT, MAX_SNAPSHOT_BYTES,
    MAX_TOTAL_EDGES, MAX_TOTAL_SYMBOLS, MAX_SCAN_FILES,
)
from .extractors import RawEdge, extract_file, iter_source_files, module_for

EDGE_TYPES = ("imports", "inherits", "calls", "attaches")
_BUILD_LOCK = threading.Lock()
_SNAPSHOT_LOCK = threading.RLock()


def bare_name(key: str) -> str:
    if "::" in key:
        key = key.split("::", 1)[1]
    if "/" in key or Path(key).suffix in (".py", ".pyi", ".cs", ".js", ".ts", ".cpp"):
        return Path(key).stem
    return key.rsplit(".", 1)[-1]


class RelationGraph:
    """One immutable-after-publication graph; queries use adjacency indexes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.built_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        self.edges: list[dict] = []
        self._edge_keys: set[tuple] = set()
        self.name_index: dict[str, set[str]] = {}
        self.module_paths: dict[str, str] = {}
        self.file_meta: dict[str, dict] = {}
        self.symbols: dict[str, dict] = {}
        self.outgoing: dict[str, list[int]] = {}
        self.incoming: dict[str, list[int]] = {}
        self.members: dict[str, set[str]] = {}
        self.file_symbols: dict[str, list[str]] = {}
        self.truncated = False
        self.diagnostics: list[dict] = []
        self.diagnostic_count = 0

    def report(self, path: str, message: str) -> None:
        self.truncated = True
        self.diagnostic_count += 1
        if len(self.diagnostics) < MAX_DIAGNOSTICS:
            self.diagnostics.append({"path": path, "error": message})

    def index_identifier(self, identifier: str) -> None:
        self.name_index.setdefault(identifier, set()).add(identifier)
        self.name_index.setdefault(bare_name(identifier), set()).add(identifier)

    def add_symbol(self, qualified: str, kind: str, start_line: int, end_line: int,
                   file: str = "", name: str = "") -> None:
        if len(self.symbols) >= MAX_TOTAL_SYMBOLS:
            self.truncated = True
            return
        self.symbols[qualified] = {
            "kind": kind, "startLine": start_line, "endLine": end_line,
            "file": file, "name": name or qualified,
        }
        self.index_identifier(qualified)
        self.name_index.setdefault(name or qualified, set()).add(qualified)

    def add_edge(self, edge: RawEdge) -> bool:
        key = (edge.source, edge.target, edge.edge_type, edge.file, edge.line)
        if key in self._edge_keys:
            return False
        if len(self.edges) >= MAX_TOTAL_EDGES:
            self.truncated = True
            return False
        self._edge_keys.add(key)
        self.edges.append({
            "source": edge.source, "target": edge.target, "type": edge.edge_type,
            "file": edge.file, "line": edge.line, "context": edge.context,
        })
        return True

    def _target_keys(self, edge: dict) -> list[str]:
        """Resolve known local targets; retain unknown expressions as external nodes."""
        target, file = edge["target"], edge["file"]
        if target in self.symbols or target in self.file_meta:
            return [target]
        if target in self.module_paths:
            return [self.module_paths[target]]
        if edge["type"] == "imports":
            # Relative JS/TS imports and C/C++ includes, without filesystem reads.
            base = Path(file).parent / target
            if target.startswith(".") or Path(target).suffix:
                for candidate in (str(base), str(Path(target))):
                    normalized = os.path.normpath(candidate).replace("\\", "/")
                    for suffix in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
                        if normalized + suffix in self.file_meta:
                            return [normalized + suffix]
        local = f"{file}::{target}"
        if local in self.symbols:
            return [local]
        # Python lexical resolution searches enclosing scopes, then the module.
        if self.file_meta.get(file, {}).get("language") == "python" and "." not in target:
            scope = edge["source"].rsplit(".", 1)[0]
            while scope:
                key = f"{scope}.{target}"
                if key in self.symbols:
                    return [key]
                scope = scope.rpartition(".")[0]
            key = f"{module_for(file)}.{target}".lstrip(".")
            if key in self.symbols:
                return [key]
            # Unimported Python names must not bind to another module's symbol.
            return [target]
        # Token-scanned languages lack compiler namespace/type resolution.
        # Multiple matching definitions are exposed explicitly as candidates.
        candidates = sorted(k for k in self.name_index.get(target, ()) if k in self.symbols)
        if len(candidates) > MAX_RELATION_LIMIT:
            self.truncated = True
        return candidates[:MAX_RELATION_LIMIT] or [target]

    def finalize(self) -> None:
        """Build owner/member and endpoint adjacency indexes once per snapshot."""
        self.outgoing.clear()
        self.incoming.clear()
        self.members.clear()
        self.file_symbols.clear()
        for key, meta in self.symbols.items():
            self.file_symbols.setdefault(meta["file"], []).append(key)
            self.members.setdefault(meta["file"], set()).add(key)
            parent = key.rpartition(".")[0]
            while parent:
                if parent in self.symbols:
                    self.members.setdefault(parent, set()).add(key)
                parent = parent.rpartition(".")[0]
        # Resolve before indexing external endpoints, so earlier edges cannot
        # influence later name resolution.
        targets = [self._target_keys(edge) for edge in self.edges]
        for index, (edge, keys) in enumerate(zip(self.edges, targets)):
            edge["targetCandidates"] = keys
            self.outgoing.setdefault(edge["source"], []).append(index)
            self.index_identifier(edge["source"])
            for key in keys:
                self.incoming.setdefault(key, []).append(index)
                self.index_identifier(key)

    def resolve(self, target_symbol: str) -> list[str]:
        target_symbol = target_symbol.replace("\\", "/")
        if target_symbol in self.symbols or target_symbol in self.file_meta:
            return [target_symbol]
        if target_symbol in self.module_paths:
            return [self.module_paths[target_symbol]]
        return sorted(self.name_index.get(target_symbol, ()))

    def _relations(self, target: str, incoming: bool, limit: int) -> list[dict]:
        keys = set(self.resolve(target))
        for key in tuple(keys):
            keys.update(self.members.get(key, ()))
        adjacency = self.incoming if incoming else self.outgoing
        indices = {i for key in keys for i in adjacency.get(key, ())}
        return [dict(self.edges[i]) for i in sorted(indices)[:max(1, min(limit, MAX_RELATION_LIMIT))]]

    def dependencies(self, target_symbol: str, limit: int = MAX_RELATION_LIMIT) -> list[dict]:
        return self._relations(target_symbol, False, limit)

    def dependents(self, target_symbol: str, limit: int = MAX_RELATION_LIMIT) -> list[dict]:
        return self._relations(target_symbol, True, limit)

    def hierarchy(self, target_symbol: str, limit: int = MAX_RELATION_LIMIT) -> dict:
        return {"ancestors": self._transitive(target_symbol, False, limit),
                "descendants": self._transitive(target_symbol, True, limit)}

    def _transitive(self, start: str, incoming: bool, limit: int) -> list[list[dict]]:
        frontier = set(self.resolve(start))
        visited = set(frontier)
        seen_edges: set[int] = set()
        levels: list[list[dict]] = []
        remaining = max(1, min(limit, MAX_RELATION_LIMIT))
        adjacency = self.incoming if incoming else self.outgoing
        for _ in range(MAX_HIERARCHY_DEPTH):
            found, next_frontier = [], set()
            indices = sorted({i for key in frontier for i in adjacency.get(key, ())})
            for i in indices:
                edge = self.edges[i]
                if edge["type"] != "inherits" or i in seen_edges:
                    continue
                seen_edges.add(i)
                found.append(dict(edge))
                endpoints = [edge["source"]] if incoming else edge["targetCandidates"]
                next_frontier.update(k for k in endpoints if k not in visited)
                remaining -= 1
                if remaining == 0:
                    break
            if not found:
                break
            levels.append(found)
            if remaining == 0 or not next_frontier:
                break
            visited.update(next_frontier)
            frontier = next_frontier
        return levels


class GraphMemory:
    """Bounded LRU cache. Serialize publication and builds to bound peak memory."""

    def __init__(self) -> None:
        self._graphs: OrderedDict[str, RelationGraph] = OrderedDict()
        # Shared across adapters in the same process, including Windows where
        # replacing a snapshot while another adapter opens it can be denied.
        self._lock = _SNAPSHOT_LOCK
        self._build_lock = _BUILD_LOCK

    @staticmethod
    def graph_path(root: Path) -> Path:
        root = root.resolve()
        path = root / GRAPH_DIR_NAME / GRAPH_FILE_NAME
        if not path.resolve().is_relative_to(root) or path.name == "index.db":
            raise ValueError("Graph snapshot must stay inside the project and separate from index.db")
        return path

    def _cache(self, root: Path, graph: RelationGraph) -> None:
        self._graphs[str(root)] = graph
        self._graphs.move_to_end(str(root))
        while len(self._graphs) > max(1, MAX_CACHED_PROJECTS):
            self._graphs.popitem(last=False)

    def is_built(self, root: Path) -> bool:
        root = root.resolve()
        with self._lock:
            return str(root) in self._graphs or self.graph_path(root).is_file()

    def build(self, root: Path) -> dict:
        root = root.resolve()
        with self._build_lock:
            graph = RelationGraph(root)
            # Source contents/ASTs are freed after each generator yield.
            for sf in iter_source_files(root, graph.report):
                graph.file_meta[sf.rel_path] = {
                    "language": sf.language, "sizeBytes": sf.size, "numSymbols": 0,
                }
                graph.index_identifier(sf.rel_path)
                if sf.language == "python":
                    graph.module_paths[module_for(sf.rel_path)] = sf.rel_path
                analysis = extract_file(sf, {})
                if analysis.error:
                    graph.report(sf.rel_path, analysis.error)
                for sym in analysis.symbols:
                    key = sym.qualified if sf.language == "python" else f"{sf.rel_path}::{sym.qualified}"
                    graph.add_symbol(key, sym.kind, sym.start_line, sym.end_line, sf.rel_path, sym.qualified)
                for edge in analysis.edges:
                    source = edge.source
                    if sf.language != "python" and source != sf.rel_path:
                        source = f"{sf.rel_path}::{source}"
                    graph.add_edge(RawEdge(source, edge.target, edge.edge_type,
                                           edge.file, edge.line, edge.context))
                graph.file_meta[sf.rel_path]["numSymbols"] = len(analysis.symbols)
            graph.finalize()
            with self._lock:
                # Failed persistence leaves the previous cached graph usable.
                self._persist(graph)
                self._cache(root, graph)
            by_type = {t: 0 for t in EDGE_TYPES}
            for edge in graph.edges:
                by_type[edge["type"]] += 1
            return {
                "status": "ok", "directory": str(root), "filesScanned": len(graph.file_meta),
                "symbolsIndexed": len(graph.symbols), "edgesStored": len(graph.edges),
                "edgesByType": by_type, "truncated": graph.truncated,
                "diagnostics": graph.diagnostics, "diagnosticCount": graph.diagnostic_count,
                "builtAt": graph.built_at, "graphFile": str(self.graph_path(root)),
            }

    def load(self, root: Path) -> RelationGraph:
        root = root.resolve()
        with self._lock:
            cached = self._graphs.get(str(root))
            if cached is not None:
                self._graphs.move_to_end(str(root))
                return cached
            path = self.graph_path(root)
            with path.open("rb") as stream:
                data = stream.read(MAX_SNAPSHOT_BYTES + 1)
            if len(data) > MAX_SNAPSHOT_BYTES:
                raise ValueError("Graph snapshot exceeds its size limit; rebuild with smaller graph limits")
            graph = self._from_payload(root, json.loads(data))
            self._cache(root, graph)
            return graph

    def _persist(self, graph: RelationGraph) -> None:
        target = self.graph_path(graph.root)
        payload = {
            "version": 2, "builtAt": graph.built_at, "directory": str(graph.root),
            "truncated": graph.truncated, "files": graph.file_meta,
            "modulePaths": graph.module_paths, "symbols": graph.symbols,
            "edges": graph.edges, "diagnostics": graph.diagnostics,
            "diagnosticCount": graph.diagnostic_count,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                             prefix="graph-", suffix=".tmp", delete=False) as stream:
                temp_path = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False)
            if temp_path.stat().st_size > MAX_SNAPSHOT_BYTES:
                raise ValueError("Graph snapshot exceeds its size limit; reduce graph limits")
            temp_path.replace(target)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _from_payload(root: Path, payload: dict) -> RelationGraph:
        try:
            if not isinstance(payload, dict) or payload.get("version") != 2:
                raise ValueError("Unsupported graph version; run codebase_build_relations")
            graph = RelationGraph(root)
            graph.built_at = payload["builtAt"]
            graph.truncated = bool(payload["truncated"])
            for name in ("files", "symbols", "modulePaths"):
                if not isinstance(payload[name], dict):
                    raise ValueError(f"Invalid graph {name}")
            graph.file_meta = payload["files"]
            graph.module_paths = payload["modulePaths"]
            if len(graph.file_meta) > MAX_SCAN_FILES or len(graph.module_paths) > MAX_SCAN_FILES:
                raise ValueError("Graph exceeds the configured file limit; rebuild with current limits")
            for path in graph.file_meta:
                if Path(path).is_absolute() or not (root / path).resolve().is_relative_to(root):
                    raise ValueError("Graph contains a path outside the project")
                graph.index_identifier(path)
            for key, meta in payload["symbols"].items():
                if meta["file"] not in graph.file_meta or not (
                    isinstance(meta["startLine"], int) and isinstance(meta["endLine"], int)
                    and 1 <= meta["startLine"] <= meta["endLine"]
                ):
                    raise ValueError("Invalid graph symbol location")
                graph.add_symbol(key, meta["kind"], meta["startLine"], meta["endLine"], meta["file"], meta["name"])
            for edge in payload["edges"]:
                if edge["type"] not in EDGE_TYPES or edge["file"] not in graph.file_meta:
                    raise ValueError("Invalid graph edge")
                graph.add_edge(RawEdge(edge["source"], edge["target"], edge["type"],
                                       edge["file"], edge["line"], edge["context"]))
            graph.diagnostics = payload.get("diagnostics", [])[:MAX_DIAGNOSTICS]
            graph.diagnostic_count = payload.get("diagnosticCount", len(graph.diagnostics))
            graph.finalize()
            return graph
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"Invalid graph snapshot; run codebase_build_relations: {exc}") from exc

