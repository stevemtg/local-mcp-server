"""Offline regressions for C# scopes and read-only symbol context."""

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from semantic_index.chunker import CodeChunk, chunk_code, csharp_symbols
from semantic_index.crawler import hash_file
from semantic_index.indexer import Indexer
from semantic_index.store import StoredChunk, VectorStore


SOURCE = (Path(__file__).parent / "fixtures" / "scopes.cs").read_text(encoding="utf-8")


class CSharpContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="csharp_context_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "Renderer.cs"
        self.path.write_text(SOURCE, encoding="utf-8")
        self.indexer = Indexer()

    def context(self, line):
        return self.indexer.symbol_context(str(self.root), "Renderer.cs", line)

    def test_method_context_and_read_only_freshness(self):
        self.assertEqual(self.context(23)["indexState"], "not_indexed")
        self.assertFalse((self.root / ".semantic_index").exists())
        store = VectorStore(self.root)
        store.replace_file(
            "Renderer.cs", hash_file(self.path), 1.0, self.path.stat().st_size,
            "csharp", [StoredChunk("Renderer.cs", 1, 40, "csharp", "block", "", SOURCE)],
            [[0.0]],
        )
        store.close()
        # Use a checkpointed rollback-journal fixture so even SQLite's shared
        # WAL reader bookkeeping is outside the read-only filesystem check.
        with closing(sqlite3.connect(self.root / ".semantic_index" / "index.db")) as db:
            db.execute("PRAGMA journal_mode=DELETE")
        before = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob("*") if p.is_file()}
        # The declaration, nested control flow, recursion and closing brace
        # must all resolve to the same complete method definition.
        for line in (23, 24, 25, 27, 29, 30, 31, 32, 33):
            with self.subTest(line=line):
                result = self.context(line)
                self.assertEqual(result["symbol"], {
                    "name": "Render", "kind": "method", "startLine": 23,
                    "endLine": 33, "declaration": "private void Render(bool enabled)",
                })
                self.assertEqual(result["snippet"], {
                    "startLine": 23, "endLine": 33,
                    "text": "\n".join(SOURCE.splitlines()[22:33]),
                })
                self.assertEqual(result["references"], [
                    {"path": "Renderer.cs", "line": 20, "preview": "Render(true);"},
                    {"path": "Renderer.cs", "line": 37, "preview": "Render(false);"},
                ])
                self.assertEqual(result["imports"], [
                    {"line": 1, "text": "using System;"},
                    {"line": 2, "text": "using System.Collections.Generic;"},
                ])
                self.assertEqual(result["relatedTests"], [])
                self.assertEqual(result["indexState"], "current")
        after = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_initializer_scopes_do_not_become_methods(self):
        for line in (13, 15, 18, 20, 21):
            self.assertEqual(self.context(line)["symbol"], {
                "name": "Prepare", "kind": "method", "startLine": 13,
                "endLine": 21, "declaration": "private void Prepare()",
            })
        for line in (6, 8, 10, 11, 39):
            self.assertEqual(self.context(line)["symbol"], {
                "name": "Renderer", "kind": "class", "startLine": 6,
                "endLine": 39, "declaration": "public sealed class Renderer",
            })
        self.assertEqual(self.context(37)["symbol"]["name"], "Refresh")

    def test_large_method_keeps_full_symbol_range(self):
        source = SOURCE.replace('                    Render(false);',
                                '                    Render(false);\n' + '                    Work();\n' * 200)
        self.path.write_text(source, encoding="utf-8")
        result = self.context(200)
        self.assertEqual(result["symbol"], {
            "name": "Render", "kind": "method", "startLine": 23,
            "endLine": 234, "declaration": "private void Render(bool enabled)",
        })
        self.assertEqual([r["line"] for r in result["references"]], [20, 238])
        self.assertLessEqual(len(result["snippet"]["text"].splitlines()), 40)
        chunks = chunk_code(source, "csharp")
        self.assertTrue(any(c.name.startswith("Render") and c.kind == "method" for c in chunks))
        self.assertTrue(all(c.end_line - c.start_line + 1 <= 180 for c in chunks))

    def test_named_symbol_beats_shorter_generic_window(self):
        self.path = self.root / "example.py"
        self.path.write_text("def work():\n    if True:\n        return 1\n", encoding="utf-8")
        candidates = [
            CodeChunk("        return 1", 3, 3, "block", "lines-3-3"),
            CodeChunk(self.path.read_text(), 1, 3, "function", "work"),
        ]
        with patch("semantic_index.symbol_context.chunk_code", return_value=candidates):
            result = self.indexer.symbol_context(str(self.root), "example.py", 3)
        self.assertEqual(result["symbol"]["name"], "work")
        self.assertEqual(result["symbol"]["kind"], "function")


class CSharpChunkTests(unittest.TestCase):
    def test_declaration_styles_and_initializer_expressions(self):
        source = """namespace Sample;
public partial class Widget {
    private Options field = new Options() { Count = 1 };
    public Options Property { get; } = new Options() { Count = 2 };
    public Options Factory() => new Options() { Count = 3 };
    private Func<Options> callback = () => new Options() { Count = 4 };
    public Widget() : base() { Work(); }
    private static List<T> Build<T>(
        Func<T> create,
        int count = 1)
        where T : new()
    {
        if (count > 0) { for (int i = 0; i < count; i++) { create(); } }
        return new List<T> { create() };
    }
    class Nested {
        void Run() { Build<object>(() => new object()); }
    }
}
"""
        self.assertEqual(
            [(c.name, c.kind, c.start_line, c.end_line) for c in csharp_symbols(source)],
            [("Widget", "class", 2, 19), ("Widget", "method", 7, 7),
             ("Build", "method", 8, 15), ("Nested", "class", 16, 18),
             ("Run", "method", 17, 17)],
        )
        # Formatting never determines scope membership.
        unindented = "\n".join(line.lstrip() for line in source.splitlines())
        self.assertEqual(
            [(c.name, c.start_line, c.end_line) for c in csharp_symbols(unindented)],
            [(c.name, c.start_line, c.end_line) for c in csharp_symbols(source)],
        )

    def test_embedding_chunks_follow_real_method_boundaries(self):
        chunks = chunk_code(SOURCE, "csharp")
        self.assertEqual(
            [(c.name, c.start_line, c.end_line) for c in chunks if c.kind == "method"],
            [("Prepare", 13, 21), ("Render", 23, 33), ("Refresh", 35, 38)],
        )

    def test_other_language_chunking(self):
        samples = {
            "python": ("def work():\n    return 1\n", "function"),
            "javascript": ("function work() {\n  return 1;\n}\n", "function"),
            "typescript": ("export function work(): number {\n  return 1;\n}\n", "function"),
            "go": ("func work() int {\n  return 1\n}\n", "function"),
            "rust": ("pub fn work() -> i32 {\n  1\n}\n", "function"),
            "java": ("public int work() {\n  return 1;\n}\n", "method"),
            "kotlin": ("class work {\n}\n", "class"),
            "c": ("int work() {\n  return 1;\n}\n", "function"),
            "cpp": ("int work() {\n  return 1;\n}\n", "function"),
        }
        for language, (source, kind) in samples.items():
            with self.subTest(language=language):
                chunks = chunk_code(source, language)
                self.assertEqual(len(chunks), 1)
                self.assertEqual((chunks[0].name, chunks[0].kind), ("work", kind))
                self.assertEqual((chunks[0].start_line, chunks[0].end_line),
                                 (1, len(source.splitlines())))


if __name__ == "__main__":
    unittest.main()
