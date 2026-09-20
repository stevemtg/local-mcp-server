"""Offline regressions for dense source exceeding embedding context limits."""

import ast
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from semantic_index.chunker import chunk_code
from semantic_index.config import MAX_CHUNK_CHARS, MAX_CHUNK_LINES
from semantic_index.embedder import EmbeddingError
from semantic_index.indexer import Indexer
from semantic_index.store import VectorStore


class BoundedEmbedder:
    model = "bounded-fixture"

    def __init__(self):
        self.requests = []

    def embed_many(self, texts):
        self.requests.extend(texts)
        if any(len(text) > MAX_CHUNK_CHARS + 256 for text in texts):
            raise EmbeddingError("the input length exceeds the context length")
        return [[1.0, 0.0] for _ in texts]


class ChunkSizeTests(unittest.TestCase):
    def assert_bounded(self, chunks, source, max_chars=MAX_CHUNK_CHARS,
                       max_lines=MAX_CHUNK_LINES):
        self.assertTrue(chunks)
        lines = source.splitlines()
        for chunk in chunks:
            self.assertLessEqual(len(chunk.content), max_chars)
            self.assertLessEqual(chunk.end_line - chunk.start_line + 1, max_lines)
            self.assertGreaterEqual(chunk.start_line, 1)
            self.assertLessEqual(chunk.end_line, len(lines))
            original = "\n".join(lines[chunk.start_line - 1:chunk.end_line])
            self.assertIn(chunk.content, original)

    def test_reported_search_test_class_splits_at_methods(self):
        source = Path(__file__).with_name("search_test.py").read_text(encoding="utf-8")
        chunks = chunk_code(source, "python")
        self.assert_bounded(chunks, source)
        self.assertGreater(len(chunks), 3)
        definition = next(node for node in ast.parse(source).body
                          if isinstance(node, ast.ClassDef) and node.name == "SearchTests")
        self.assertEqual([c.name for c in chunks if c.kind == "method"],
                         [node.name for node in definition.body if isinstance(node, ast.FunctionDef)])
        method = next(c for c in chunks if c.name == "test_filters_threshold_limit_and_result_fields")
        self.assertEqual(method.kind, "method")
        self.assertIn('self.assertEqual(hit["score"], 1.0)', method.content)

    def test_dense_classes_split_at_methods_below_line_limit(self):
        body = "x" * (MAX_CHUNK_CHARS // 3)
        samples = {
            "python": "class Dense:\n" + "".join(
                f"    def method_{i}(self):\n        return '{body}'\n" for i in range(4)),
            "typescript": "export class Dense {\n" + "".join(
                f"  method_{i}() {{\n    return '{body}';\n  }}\n" for i in range(4)) + "}",
        }
        for language, source in samples.items():
            with self.subTest(language=language):
                self.assertLess(len(source.splitlines()), MAX_CHUNK_LINES)
                chunks = chunk_code(source, language)
                self.assert_bounded(chunks, source)
                self.assertEqual([c.name for c in chunks if c.kind == "method"],
                                 [f"method_{i}" for i in range(4)])
                self.assertEqual("\n".join(c.content for c in chunks), source.rstrip("\n"))

    def test_oversized_inner_method_is_bounded_after_class_split(self):
        source = "class Dense:\n    def large(self):\n" + (
            "        value = '" + "x" * 100 + "'\n") * 100
        chunks = chunk_code(source, "python")
        self.assert_bounded(chunks, source)
        methods = [c for c in chunks if c.kind == "method"]
        self.assertGreater(len(methods), 1)
        self.assertTrue(all(c.name.startswith("large (part ") for c in methods))
        self.assertEqual("\n".join(c.content for c in chunks), source.rstrip("\n"))

    def test_decorated_dense_class_keeps_nested_helpers_in_their_method(self):
        source = ("@decorate\nclass Dense:\n"
                  "    def first(self):\n"
                  "        def helper():\n"
                  "            return 1\n"
                  "        return helper()\n"
                  "    def second(self):\n        return 2\n")
        with patch("semantic_index.chunker.MAX_CHUNK_CHARS", 140):
            chunks = chunk_code(source, "python")
            self.assert_bounded(chunks, source, max_chars=140)
            self.assertEqual([c.name for c in chunks], ["Dense", "first", "second"])
            self.assertIn("return helper()", chunks[1].content)
            self.assertEqual("\n".join(c.content for c in chunks), source.rstrip("\n"))

    def test_dense_functions_and_fallback_windows_keep_every_line(self):
        body = "x" * 100
        samples = {
            "python": "def work():\n" + f"    value = '{body}'\n" * 70,
            "javascript": "function work() {\n" + f"  value = '{body}';\n" * 70 + "}",
            "csharp": "public class Dense {\n  void Work() {\n" + f"    Value(\"{body}\");\n" * 70 + "  }\n}",
            "unknown": f"value = '{body}'\n" * 70,
        }
        for language, source in samples.items():
            with self.subTest(language=language):
                chunks = chunk_code(source, language)
                self.assert_bounded(chunks, source)
                self.assertGreater(len(chunks), 1)
                self.assertEqual("\n".join(c.content for c in chunks), source.rstrip("\n"))

    def test_oversized_single_line_keeps_all_characters_and_line_number(self):
        for language in ("python", "javascript", "csharp", "unknown"):
            with self.subTest(language=language):
                source = "payload = '" + "\u00e9\u4e2d\U0001f600" * MAX_CHUNK_CHARS + "'"
                chunks = chunk_code(source, language)
                self.assert_bounded(chunks, source)
                self.assertEqual("".join(c.content for c in chunks), source)
                self.assertTrue(all((c.start_line, c.end_line) == (1, 1) for c in chunks))
                self.assertEqual(len({c.name for c in chunks}), len(chunks))

    def test_character_limit_includes_newlines_and_accepts_exact_fit(self):
        source = "abcd\nefghi"
        with patch("semantic_index.chunker.MAX_CHUNK_CHARS", 10):
            chunks = chunk_code(source, "unknown")
            self.assertEqual(len(chunks), 1)
            self.assertEqual(chunks[0].content, source)
            source += "\nj"
            chunks = chunk_code(source, "unknown")
            self.assert_bounded(chunks, source, max_chars=10)
            self.assertEqual([c.content for c in chunks], ["abcd\nefghi", "j"])
            self.assertEqual([(c.start_line, c.end_line) for c in chunks], [(1, 2), (3, 3)])

    def test_line_limit_applies_to_inner_methods_and_fallbacks(self):
        body = "        pass\n" * 10
        for language, source in (("python", "class Dense:\n    def work(self):\n" + body),
                                 ("unknown", body)):
            with self.subTest(language=language), patch("semantic_index.chunker.MAX_CHUNK_LINES", 4):
                chunks = chunk_code(source, language)
                self.assert_bounded(chunks, source, max_lines=4)
                self.assertEqual("\n".join(c.content for c in chunks), source.rstrip("\n"))

    def test_small_symbols_keep_content_names_and_ranges(self):
        source = "def small():\n    return 1\n"
        chunks = chunk_code(source, "python")
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0].content, chunks[0].name, chunks[0].kind,
                          chunks[0].start_line, chunks[0].end_line),
                         (source.rstrip("\n"), "small", "function", 1, 2))

    def test_reported_file_indexes_and_is_skipped_when_unchanged(self):
        source = Path(__file__).with_name("search_test.py").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="dense_index_") as directory:
            root = Path(directory)
            (root / "search_test.py").write_text(source, encoding="utf-8")
            embedder = BoundedEmbedder()
            indexer = Indexer(embedder)
            result = indexer.index_directory(directory)
            self.assertEqual(result["files_failed"], 0)
            self.assertEqual(result["files_embedded"], 1)
            self.assertEqual(result["total_chunks"], len(embedder.requests))
            self.assertGreater(result["total_chunks"], 3)
            store = VectorStore(root, read_only=True)
            try:
                self.assertIn("search_test.py", store.indexed_files())
            finally:
                store.close()
            embedder.requests.clear()
            result = indexer.index_directory(directory)
            self.assertEqual(result["files_unchanged_skipped"], 1)
            self.assertEqual(embedder.requests, [])


if __name__ == "__main__":
    unittest.main()
