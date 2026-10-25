"""Offline regressions for the structural graph layer (no Ollama, no network)."""

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from code_graph.extractors import RawEdge
from code_graph.graph_store import GraphMemory, bare_name
from code_graph.router import StructuralRouter
from code_graph import graph_store
from code_graph import extractors

PY_A = """import json
from .b import Helper

class Base:
    def go(self):
        pass

class Impl(Base):
    def run(self, h):
        h.help()
        return json.dumps({})
"""
PY_B = """class Helper:
    def help(self):
        pass
"""
CS_FILE = """using System.IO;

public class Widget : BaseWidget
{
    public void Spin() { Gizmos.Draw(); }
}
"""


class ExtractorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="graph_fixture_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "__init__.py").write_text("")
        (self.root / "pkg" / "a.py").write_text(PY_A, encoding="utf-8")
        (self.root / "pkg" / "b.py").write_text(PY_B, encoding="utf-8")
        (self.root / "thing.cs").write_text(CS_FILE, encoding="utf-8")
        self.memory = GraphMemory()
        self.summary = self.memory.build(self.root)
        self.graph = self.memory.load(self.root)

    def test_python_symbols_and_edges(self):
        self.assertIn("pkg.a.Impl", self.graph.symbols)
        self.assertIn("pkg.a.Impl.run", self.graph.symbols)
        deps = self.graph.dependencies("Impl")
        inherits = [e for e in deps if e["type"] == "inherits"]
        self.assertEqual([e["target"] for e in inherits], ["pkg.a.Base"])
        calls = [e for e in deps if e["type"] == "calls"]
        self.assertIn("h.help", [e["target"] for e in calls])
        self.assertIn("json.dumps", [e["target"] for e in calls])

    def test_python_import_edge_resolves_module_path(self):
        imports = [e for e in self.graph.edges if e["type"] == "imports"]
        pairs = {(e["source"], e["target"]) for e in imports}
        self.assertIn(("pkg/a.py", "json"), pairs)
        self.assertIn(("pkg/a.py", "pkg.b.Helper"), pairs)

    def test_brace_language_edges(self):
        self.assertIn("thing.cs::Widget", self.graph.symbols)
        self.assertIn("thing.cs::Widget.Spin", self.graph.symbols)
        widget_deps = self.graph.dependencies("Widget")
        by_type = {e["type"]: e["target"] for e in widget_deps}
        self.assertEqual(by_type["inherits"], "BaseWidget")
        self.assertEqual(by_type["calls"], "Gizmos.Draw")
        file_deps = self.graph.dependencies("thing.cs")
        self.assertEqual([e["target"] for e in file_deps if e["type"] == "imports"],
                         ["System.IO"])

    def test_hierarchy_and_bare_name(self):
        hierarchy = self.graph.hierarchy("Base")
        self.assertTrue(any(e["source"] == "pkg.a.Impl"
                            for level in hierarchy["descendants"] for e in level))
        impl_up = self.graph.hierarchy("Impl")["ancestors"]
        self.assertTrue(any(e["target"] == "pkg.a.Base"
                            for level in impl_up for e in level))
        self.assertEqual(bare_name("pkg.a.Impl"), "Impl")
        self.assertEqual(bare_name("pkg/a.py"), "a")

    def test_persistence_roundtrip_and_dedup(self):
        fresh = GraphMemory()
        reloaded = fresh.load(self.root)
        self.assertEqual(len(reloaded.edges), len(self.graph.edges))
        stored = reloaded.edges[0]
        before = len(reloaded.edges)
        duplicate = RawEdge(stored["source"], stored["target"], stored["type"],
                            stored["file"], stored["line"], stored["context"])
        self.assertFalse(reloaded.add_edge(duplicate))
        self.assertEqual(len(reloaded.edges), before)
        payload = json.loads(
            (self.root / ".semantic_index" / "graph.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["directory"], str(self.root))
        self.assertEqual(self.summary["filesScanned"], 4)
        self.assertFalse(self.summary["truncated"])


class RouterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="graph_router_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "a.py").write_text(PY_A, encoding="utf-8")
        (self.root / "pkg" / "b.py").write_text(PY_B, encoding="utf-8")
        self.router = StructuralRouter()

    def test_query_fails_closed_before_build(self):
        result = self.router.query(str(self.root), "Impl")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "graph_not_built")

    def test_build_then_query_and_no_match(self):
        built = self.router.build(str(self.root))
        self.assertEqual(built["status"], "ok")
        self.assertGreaterEqual(built["edgesStored"], 1)
        result = self.router.query(str(self.root), "Impl", relationship_type="hierarchy")
        self.assertEqual(result["status"], "ok")
        self.assertIn("pkg.a.Impl", result["candidates"])
        missing = self.router.query(str(self.root), "NoSuchSymbol")
        self.assertEqual(missing["status"], "no_match")

    def test_navigate_actions_and_traversal_guard(self):
        self.router.build(str(self.root))
        listing = self.router.navigate(str(self.root), "list_dir", "pkg")
        self.assertEqual(listing["status"], "ok")
        self.assertIn({"name": "a.py", "kind": "file"}, listing["entries"])
        reading = self.router.navigate(str(self.root), "read_file", "pkg/b.py")
        self.assertIn("class Helper", reading["content"])
        symbol = self.router.navigate(str(self.root), "inspect_symbol", "Helper")
        self.assertEqual(symbol["status"], "ok")
        self.assertIn("class Helper", symbol["snippet"])
        self.assertEqual(symbol["file"], "pkg/b.py")
        sneaky = self.router.navigate(str(self.root), "read_file", "../outside.txt")
        self.assertEqual(sneaky["status"], "failed")
        bad = self.router.navigate(str(self.root), "explode", "pkg")
        self.assertEqual(bad["error"]["code"], "invalid_action")

    def test_edge_cap_marks_truncated(self):
        with patch.object(graph_store, "MAX_TOTAL_EDGES", 1):
            memory = GraphMemory()
            summary = memory.build(self.root)
        self.assertTrue(summary["truncated"])

    def test_summary_before_build_contains_interfaces_without_bodies(self):
        summary = self.router.navigate(str(self.root), "view_file_summary", "pkg/a.py")
        self.assertEqual(summary["status"], "ok")
        self.assertIn("pkg.a.Impl.run", [s["symbol"] for s in summary["symbols"]])
        self.assertIn("pkg.b.Helper", [s["target"] for s in summary["imports"]])
        self.assertNotIn("content", summary)
        self.assertNotIn("json.dumps", json.dumps(summary))
        self.assertFalse((self.root / ".semantic_index").exists())

    def test_invalid_sources_and_caps_have_diagnostics(self):
        (self.root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
        summary = self.router.build(str(self.root))
        self.assertEqual(summary["status"], "ok")
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["diagnostics"][0]["path"], "bad.py")
        self.assertEqual(self.router.navigate(str(self.root), "view_file_summary", "bad.py")["status"], "failed")
        self.assertEqual(self.router.query(str(self.root), "Impl")["status"], "ok")
        with patch.object(extractors, "MAX_SCAN_FILES", 1):
            limited = self.router.build(str(self.root))
        self.assertTrue(limited["truncated"])
        self.assertTrue(any("Scan file cap" in d["error"] for d in limited["diagnostics"]))
        with patch.object(extractors, "MAX_FILE_SIZE_BYTES", 8):
            large = self.router.navigate(str(self.root), "view_file_summary", "pkg/a.py")
        self.assertEqual(large["status"], "failed")
        self.assertIn("byte", large["error"]["message"])

    def test_qualified_queries_do_not_mix_same_named_types(self):
        for name in ("one", "two"):
            (self.root / f"{name}.py").write_text(
                "class Base: pass\nclass Impl(Base):\n    def run(self):\n        self.go()\n    def go(self): pass\n",
                encoding="utf-8")
        self.router.build(str(self.root))
        result = self.router.query(str(self.root), "one.Impl", "hierarchy")
        self.assertEqual(result["candidates"], ["one.Impl"])
        self.assertEqual(result["hierarchy"]["ancestors"][0][0]["targetCandidates"], ["one.Base"])
        result = self.router.query(str(self.root), "one.Impl.go", "dependents")
        self.assertEqual([e["source"] for e in result["dependents"]], ["one.Impl.run"])

    def test_package_imports_nested_calls_and_file_dependents(self):
        (self.root / "pkg" / "__init__.py").write_text("from .b import Helper\n", encoding="utf-8")
        (self.root / "caller.py").write_text(
            "import pkg.b\nfrom pkg.b import Helper as H\n"
            "def outer():\n    def inner():\n        pkg.b.Helper()\n"
            "    return inner()\nif True:\n    def factory():\n        return H()\n",
            encoding="utf-8")
        self.router.build(str(self.root))
        graph = self.router.memory.load(self.root)
        self.assertIn("pkg", graph.module_paths)
        self.assertEqual(graph.module_paths["pkg"], "pkg/__init__.py")
        self.assertIn("caller.outer.inner", graph.symbols)
        self.assertIn("caller.factory", graph.symbols)
        calls = [e for e in graph.dependencies("caller.outer.inner") if e["type"] == "calls"]
        self.assertEqual(calls[0]["targetCandidates"], ["pkg.b.Helper"])
        outer_calls = [e for e in graph.edges if e["source"] == "caller.outer"]
        self.assertEqual([e["target"] for e in outer_calls], ["caller.outer.inner"])
        dependents = graph.dependents("pkg/b.py")
        self.assertTrue(any(e["source"] == "caller.factory" for e in dependents))
        self.assertTrue(any(e["source"] == "pkg/__init__.py" for e in dependents))

    def test_game_components_and_standalone_symbol_locations(self):
        (self.root / "empty.cs").write_text("public class Empty {}\n", encoding="utf-8")
        (self.root / "player.cs").write_text(
            'public class Player : Empty\n{\n    public void Start()\n    {\n'
            '        // AddComponent<Fake>();\n        var s = "Bad.Call()";\n'
            '        gameObject.AddComponent<Health>();\n        this.Tick();\n    }\n'
            '    public void Tick() {}\n}\n', encoding="utf-8")
        (self.root / "unreal.cpp").write_text(
            '#include "player.h"\nclass Actor : public Base\n{\n'
            '    void Setup() { CreateDefaultSubobject<UCameraComponent>("Camera"); }\n};\n',
            encoding="utf-8")
        self.router.build(str(self.root))
        graph = self.router.memory.load(self.root)
        calls = graph.dependencies("player.cs::Player.Start")
        self.assertTrue(any(e["type"] == "attaches" and e["target"] == "Health" for e in calls))
        self.assertTrue(any(e["targetCandidates"] == ["player.cs::Player.Tick"] for e in calls))
        self.assertFalse(any("Fake" in e["target"] or "Bad" in e["target"] for e in graph.edges))
        self.assertTrue(any(e["type"] == "attaches" and e["target"] == "UCameraComponent" for e in graph.edges))
        inspected = self.router.navigate(str(self.root), "inspect_symbol", "Empty")
        self.assertEqual(inspected["file"], "empty.cs")
        self.assertIn("class Empty", inspected["snippet"])
        self.assertEqual(graph.symbols["player.cs::Player.Start"]["endLine"], 9)

    def test_hierarchy_total_limit_and_cycle(self):
        (self.root / "tree.py").write_text(
            "class A(B): pass\nclass B(C): pass\nclass C(A): pass\n", encoding="utf-8")
        self.router.build(str(self.root))
        graph = self.router.memory.load(self.root)
        for direction in graph.hierarchy("tree.A", limit=2).values():
            self.assertLessEqual(sum(len(level) for level in direction), 2)

    def test_duplicate_brace_names_keep_both_locations(self):
        (self.root / "one.cs").write_text("public class Same {}\n", encoding="utf-8")
        (self.root / "two.cs").write_text("public class Same {}\n", encoding="utf-8")
        self.router.build(str(self.root))
        graph = self.router.memory.load(self.root)
        self.assertEqual(graph.resolve("Same"), ["one.cs::Same", "two.cs::Same"])
        self.assertEqual(graph.resolve("one.cs::Same"), ["one.cs::Same"])
        self.assertEqual(self.router.navigate(str(self.root), "inspect_symbol", "Same")["status"], "ambiguous")

    def test_corrupt_graph_and_io_failures_return_diagnostics(self):
        self.router.build(str(self.root))
        path = GraphMemory.graph_path(self.root)
        for value in ("broken JSON", "[]", '{"version": 2}', '{"version": 1}'):
            path.write_text(value, encoding="utf-8")
            response = StructuralRouter().query(str(self.root), "Impl")
            self.assertEqual(response["status"], "failed")
            self.assertEqual(response["error"]["code"], "graph_unreadable")
        with patch("code_graph.router.read_text", side_effect=PermissionError("denied")):
            response = self.router.navigate(str(self.root), "view_file_summary", "pkg/a.py")
        self.assertEqual(response["status"], "failed")
        self.assertIn("denied", response["error"]["message"])
        self.assertEqual(self.router.build("relative/path")["status"], "failed")

    def test_atomic_build_and_concurrent_builds_preserve_vector_db(self):
        index_dir = self.root / ".semantic_index"
        index_dir.mkdir()
        database = index_dir / "index.db"
        database.write_bytes(b"vector-index-sentinel")
        self.router.build(str(self.root))
        original = self.router.memory.load(self.root)
        with patch.object(self.router.memory, "_persist", side_effect=OSError("disk full")):
            self.assertEqual(self.router.build(str(self.root))["status"], "failed")
        self.assertIs(self.router.memory.load(self.root), original)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: StructuralRouter().build(str(self.root)), range(4)))
        self.assertTrue(all(r["status"] == "ok" for r in results), results)
        self.assertIn("pkg.a.Impl", GraphMemory().load(self.root).symbols)
        self.assertEqual(database.read_bytes(), b"vector-index-sentinel")
        self.assertFalse(list(index_dir.glob("*.tmp")))

    def test_cache_eviction_and_normalized_root(self):
        with patch.object(graph_store, "MAX_CACHED_PROJECTS", 1):
            self.router.build(str(self.root))
            self.router.build(str(self.root / "pkg"))
        self.assertEqual(len(self.router.memory._graphs), 1)
        result = self.router.query(str(self.root / "pkg" / ".."), "Impl")
        self.assertEqual(result["status"], "ok")

    def test_semantic_hits_get_graph_traces_without_changing_scores(self):
        hit = {"path": "pkg/a.py", "startLine": 9, "endLine": 11, "symbol": "Impl.run", "score": .9}
        payload = {"results": [hit], "note": "original"}
        self.assertIs(self.router.enrich_results(str(self.root), payload), payload)
        self.router.build(str(self.root))
        enriched = self.router.enrich_results(str(self.root), payload)
        result = enriched["results"][0]
        self.assertEqual(result["score"], .9)
        self.assertEqual(result["structural"]["candidates"], ["pkg.a.Impl.run"])
        self.assertTrue(result["structural"]["traces"][0]["dependencies"])
        self.assertNotIn("structural", hit)


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_requested_mcp_contract_and_live_summary(self):
        from mcp import Client
        from mcp.server.mcpserver import MCPServer
        from code_graph.tool import register

        with tempfile.TemporaryDirectory(prefix="graph_contract_") as td:
            root = Path(td)
            (root / "api.py").write_text("class API:\n    def run(self): pass\n", encoding="utf-8")
            server = MCPServer("graph-contract")
            register(server)
            async with Client(server) as client:
                listed = {t.name: t for t in (await client.list_tools()).tools}
                properties = listed["codebase_query_graph"].input_schema["properties"]
                self.assertEqual(set(properties["relationship_type"]["enum"]),
                                 {"dependencies", "dependents", "hierarchy"})
                nav = listed["codebase_agentic_navigate"].input_schema
                self.assertIn("current_path", nav["required"])
                self.assertEqual(set(nav["properties"]["action"]["enum"]), {"list_dir", "view_file_summary"})
                for name, args in (
                    ("codebase_agentic_navigate", {"current_path": "api.py", "action": "view_file_summary"}),
                    ("codebase_build_relations", {}),
                    ("codebase_query_graph", {"target_symbol": "API", "relationship_type": "hierarchy"}),
                ):
                    response = await client.call_tool(name, {"directory_path": td, **args})
                    self.assertFalse(response.is_error, response)
                response = await client.call_tool("codebase_agentic_navigate", {
                    "directory_path": td, "current_path": "../outside.py", "action": "view_file_summary"})
                self.assertTrue(response.is_error)


if __name__ == "__main__":
    unittest.main()

