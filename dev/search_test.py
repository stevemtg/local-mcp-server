"""Deterministic search contracts, independent of Ollama's model rankings."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from semantic_index.indexer import Indexer
from semantic_index.store import StoredChunk, VectorStore, cosine


SOURCE = "def first():\n    return 'alpha'\n\n\ndef second():\n    return 'beta'\n"


class RecordingEmbedder:
    model = "fixture-model"

    def __init__(self):
        self.requests = []

    def embed_one(self, text):
        self.requests.append(text)
        return [0.0, 1.0] if "beta" in text else [1.0, 0.0]

    def embed_many(self, texts):
        return [self.embed_one(text) for text in texts]


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="search_contract_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "query.py").write_text(SOURCE, encoding="utf-8")
        self.embedder = RecordingEmbedder()
        self.indexer = Indexer(self.embedder)

    def populate(self):
        store = VectorStore(self.root)
        try:
            store.set_meta("embedding_model", self.embedder.model)
            store.set_meta("embedding_dim", 2)
            entries = [
                ("query.py", [
                    StoredChunk("query.py", 1, 4, "python", "function", "first", SOURCE.split("def second")[0]),
                    StoredChunk("query.py", 5, 6, "python", "function", "second", "def second():\n    return 'beta'"),
                ], [[1.0, 0.0], [0.0, 1.0]]),
                ("src/retry.py", [StoredChunk("src/retry.py", 1, 2, "python", "function", "retry", "def retry():\n    return 'alpha'")], [[1.0, 0.0]]),
                ("src/other.py", [StoredChunk("src/other.py", 1, 2, "python", "function", "other", "def other():\n    return 'mixed'")], [[0.6, 0.8]]),
                ("tests/retry_test.py", [StoredChunk("tests/retry_test.py", 1, 2, "python", "function", "test_retry", "def test_retry():\n    return 'alpha'")], [[1.0, 0.0]]),
            ]
            for path, chunks, vectors in entries:
                target = self.root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                if path != "query.py":
                    target.write_text(chunks[0].content, encoding="utf-8")
                store.replace_file(path, "fixture-hash", 1.0, target.stat().st_size,
                                   "python", chunks, vectors)
        finally:
            store.close()

    def search(self, query="alpha", **kwargs):
        return self.indexer.semantic_search(str(self.root), query, **kwargs)

    def similar(self, **kwargs):
        return self.indexer.similar(str(self.root), "query.py", **kwargs)

    def test_missing_index_is_read_only_and_does_not_embed(self):
        for result in (self.search(), self.similar(), self.indexer.status(str(self.root))):
            self.assertFalse(result.get("results", []))
        self.assertIn("index_codebase", self.search()["note"])
        self.assertEqual(self.embedder.requests, [])
        self.assertFalse((self.root / ".semantic_index").exists())

    def test_filters_threshold_limit_and_result_fields(self):
        self.populate()
        result = self.search(path_filter="src/*", min_score=0.9, limit=1)
        self.assertEqual(len(result["results"]), 1)
        hit = result["results"][0]
        self.assertEqual(hit["path"], "src/retry.py")
        self.assertEqual(hit["absolutePath"], str(self.root / "src/retry.py"))
        self.assertEqual((hit["startLine"], hit["endLine"], hit["symbol"]), (1, 2, "retry"))
        self.assertEqual(hit["score"], 1.0)
        self.assertIn("def retry():", hit["snippet"])
        self.assertIn("Rank 1", hit["reason"])
        self.assertEqual([h["path"] for h in self.search(path_filter="tests")["results"]],
                         ["tests/retry_test.py"])
        self.assertEqual(self.search(path_filter="*.cs")["results"], [])
        self.assertEqual(self.search(path_filter="src/other.py", min_score=0.9)["results"], [])

    def test_invalid_inputs_fail_before_embedding_or_creating_index(self):
        calls = [lambda: self.search("  "), lambda: self.search(path_filter=" ")]
        calls += [lambda limit=limit: self.search(limit=limit) for limit in (0, -1, 51)]
        calls += [lambda score=score: self.search(min_score=score)
                  for score in (-1.1, 1.1, float("nan"), float("inf"))]
        calls += [lambda bounds=bounds: self.similar(**bounds) for bounds in (
            {"start_line": 0}, {"end_line": 0}, {"start_line": -1},
            {"start_line": 5, "end_line": 2}, {"end_line": 7}, {"start_line": 7},
            {"limit": 0}, {"limit": 51},
        )]
        calls += [lambda path=path: self.indexer.similar(str(self.root), path)
                  for path in (str(self.root / "query.py"), "../outside.py", "missing.py")]
        for call in calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()
        self.assertFalse((self.root / ".semantic_index").exists())
        self.assertEqual(self.embedder.requests, [])

    def test_independent_bounds_and_same_file_exclusion(self):
        self.populate()
        result = self.similar(end_line=4)
        self.assertEqual(result["range"], [1, 4])
        self.assertEqual(len(self.embedder.requests), 1)
        self.assertIn("lines 1-3", self.embedder.requests[0])
        self.assertNotIn("second", self.embedder.requests[0])
        self.assertIn(("query.py", 5), [(h["path"], h["startLine"]) for h in result["results"]])
        self.assertNotIn(("query.py", 1), [(h["path"], h["startLine"]) for h in result["results"]])
        self.embedder.requests.clear()
        result = self.similar(start_line=5)
        self.assertEqual(result["range"], [5, 6])
        self.assertIn("lines 5-6", self.embedder.requests[0])
        self.assertNotIn("first", self.embedder.requests[0])
        self.assertNotIn(("query.py", 5), [(h["path"], h["startLine"]) for h in result["results"]])

    def test_whole_file_deduplicates_and_keeps_best_score(self):
        self.populate()
        result = self.similar()
        self.assertIsNone(result["range"])
        self.assertEqual(len(self.embedder.requests), 2)
        hits = result["results"]
        self.assertTrue(all(h["path"] != "query.py" for h in hits))
        self.assertEqual(len(hits), len({(h["path"], h["startLine"], h["endLine"]) for h in hits}))
        self.assertEqual(next(h["score"] for h in hits if h["path"] == "src/other.py"), 0.8)
        self.assertEqual([h["score"] for h in hits], sorted([h["score"] for h in hits], reverse=True))

    def test_similar_uses_live_source_even_when_not_indexed(self):
        self.populate()
        live = self.root / "new.py"
        live.write_text("def changed():\n    return 'beta'\n", encoding="utf-8")
        result = self.indexer.similar(str(self.root), "new.py", limit=1)
        self.assertEqual(result["results"][0]["symbol"], "second")
        (self.root / "query.py").write_text(live.read_text(), encoding="utf-8")
        result = self.similar(limit=1)
        self.assertIn("beta", self.embedder.requests[-1])
        self.assertEqual(result["results"][0]["symbol"], "other")
        self.assertTrue(all(h["path"] != "query.py" for h in result["results"]))

    def test_query_cap_is_reported_to_caller(self):
        self.populate()
        with patch("semantic_index.indexer.SIMILAR_QUERY_MAX_PARTS", 1):
            result = self.similar()
        self.assertEqual(len(self.embedder.requests), 1)
        self.assertIn("first 1 of 2 chunks", result["note"])
        self.assertIn("start_line/end_line", result["note"])

    def test_incompatible_models_and_dimensions_are_rejected(self):
        self.populate()
        self.embedder.model = "different-model"
        for call in (self.search, self.similar):
            with self.assertRaisesRegex(ValueError, "index_codebase"):
                call()
        self.assertEqual(self.embedder.requests, [])
        with self.assertRaisesRegex(ValueError, "different dimensions"):
            cosine([1.0, 0.0], [1.0])

    def test_existing_index_records_are_read_only(self):
        self.populate()
        db = self.root / ".semantic_index/index.db"
        def snapshot():
            connection = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
            try:
                return list(connection.iterdump())
            finally:
                connection.close()
        before = snapshot()
        self.search()
        self.similar()
        self.indexer.status(str(self.root))
        self.assertEqual(snapshot(), before)
        store = VectorStore(self.root, read_only=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.set_meta("forbidden", "write")
        finally:
            store.close()

    def test_empty_source_is_rejected(self):
        (self.root / "query.py").write_text("  \n\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.similar()
        self.assertEqual(self.embedder.requests, [])


if __name__ == "__main__":
    unittest.main()
