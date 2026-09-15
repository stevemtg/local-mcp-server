"""Offline smoke test: chunker correctness + store round-trip with fake vectors."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from semantic_index.chunker import chunk_code  # noqa: E402
from semantic_index.store import VectorStore  # noqa: E402

py_src = '''
"""Module docstring."""
import os


# a helper comment
def add(a, b):
    return a + b


class Greeter:
    def hello(self, name):
        return f"hi {name}"

    def bye(self):
        return "bye"


@decorator
async def fetch(url):
    data = {"brace": "not { a block }"}  # // fake /* comment
    return data
'''

chunks = chunk_code(py_src, "python")
print("PYTHON CHUNKS:")
for c in chunks:
    print(f"  [{c.kind}] {c.name!r} lines {c.start_line}-{c.end_line}")
names = [c.name for c in chunks]
assert "add" in names and "Greeter" in names or any(n == "hello" for n in names)

js_src = """
export function greet(name) {
  const msg = `hello {${name}}`; // brace in string
  return msg;
}

export class Foo {
  bar() {
    return 1;
  }
}

const arrow = (x) => x * 2;
"""
jchunks = chunk_code(js_src, "typescript")
print("TS CHUNKS:")
for c in jchunks:
    print(f"  [{c.kind}] {c.name!r} lines {c.start_line}-{c.end_line}")

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    store = VectorStore(root)
    from semantic_index.store import StoredChunk

    store.set_meta("embedding_model", "fake")
    store.replace_file(
        "a.py",
        "hash1",
        1.0,
        10,
        "python",
        [StoredChunk("a.py", 1, 5, "python", "function", "add", "def add...")],
        [[0.1, 0.2, 0.3]],
    )
    store.replace_file(
        "b.ts", "hash2", 2.0, 20, "typescript",
        [StoredChunk("b.ts", 1, 3, "typescript", "function", "greet", "export...")],
        [[1.0, 0.0, 0.0]],
    )
    hits = store.search([1.0, 0.0, 0.0], limit=2)
    assert hits[0].name == "greet", hits
    assert hits[0].score > 0.9
    # path_filter restricts candidates by relative path pattern
    hits = store.search([1.0, 0.0, 0.0], limit=2, path_filter="*.py")
    assert hits and hits[0].name == "add", hits
    # exclude drops chunks overlapping the excluded range
    hits = store.search([0.1, 0.2, 0.3], limit=5, exclude=[("a.py", 1, 5)])
    assert all(h.name != "add" for h in hits), hits
    # min_score drops weak matches
    hits = store.search([1.0, 0.0, 0.0], limit=5, min_score=0.9)
    assert all(h.name == "greet" for h in hits) and hits, hits
    store.remove_file("a.py")
    assert store.count_chunks() == 1
    print("STORE STATS:", store.stats())
    store.close()

# ---- crawler ignores generated Unity Library files -------------------------
from semantic_index.crawler import hash_file, iter_source_files  # noqa: E402

with tempfile.TemporaryDirectory(prefix="unity_project_") as td:
    unity_root = Path(td)
    (unity_root / "Assets" / "Scripts").mkdir(parents=True)
    (unity_root / "Assets" / "Scripts" / "Player.cs").write_text(
        "public class Player {}\n", encoding="utf-8"
    )
    (unity_root / "Library" / "PackageCache").mkdir(parents=True)
    (unity_root / "Library" / "PackageCache" / "Generated.cs").write_text(
        "public class Generated {}\n", encoding="utf-8"
    )

    crawled_paths = {item.rel_path for item in iter_source_files(unity_root)}
    assert crawled_paths == {"Assets/Scripts/Player.cs"}, crawled_paths

# ---- get_symbol_context (offline: no Ollama, no embedding at all) ----------
from semantic_index.indexer import Indexer  # noqa: E402

CTX_SERVICE = (
    '"""Sample service module."""\n'
    '\n'
    '\n'
    'def compute_retry_delay(attempt: int) -> float:\n'
    '    """Exponential backoff with jitter."""\n'
    '    return 2 ** attempt\n'
)
CTX_CALLER = "import service\n\n\ndef job():\n    return service.compute_retry_delay(2)\n"
CTX_TEST = (
    "import service\n\n\ndef test_compute_retry_delay():\n"
    "    assert service.compute_retry_delay(1) == 2\n"
)
CTX_NOTES = "# just a comment\n# another comment\n"
CTX_MULTILINE = (
    "def combine(\n"
    "    left: int,\n"
    "    right: int,\n"
    ") -> int:\n"
    "    total = left + right\n"
    "    return total\n"
    "\n"
    "\n"
    "def after():\n"
    "    return 0\n"
)

ctx_root = Path(tempfile.mkdtemp(prefix="ctx_project_"))
(ctx_root / "service.py").write_text(CTX_SERVICE, encoding="utf-8")
(ctx_root / "caller.py").write_text(CTX_CALLER, encoding="utf-8")
(ctx_root / "notes.py").write_text(CTX_NOTES, encoding="utf-8")
(ctx_root / "multiline.py").write_text(CTX_MULTILINE, encoding="utf-8")
(ctx_root / "tests").mkdir()
(ctx_root / "tests" / "test_service.py").write_text(CTX_TEST, encoding="utf-8")

ix = Indexer()  # constructor is offline; the symbol_context path never embeds
res = ix.symbol_context(str(ctx_root), "service.py", 6)
assert res["directory"] == str(ctx_root)
assert res["path"] == "service.py"
assert res["absolutePath"] == str(ctx_root / "service.py")
assert res["requestedLine"] == 6 and res["language"] == "python"
assert res["symbol"]["name"] == "compute_retry_delay"
assert res["symbol"]["kind"] == "function"
assert [res["symbol"]["startLine"], res["symbol"]["endLine"]] == [4, 6]
assert res["symbol"]["declaration"] == "def compute_retry_delay(attempt: int) -> float:"
assert [res["snippet"]["startLine"], res["snippet"]["endLine"]] == [4, 6]
assert "return 2 ** attempt" in res["snippet"]["text"]
assert res["imports"] == []
# lexical references found in other files, sorted by path then line...
assert [(r["path"], r["line"]) for r in res["references"]] == [
    ("caller.py", 5),
    ("tests/test_service.py", 5),
]
# ...the definition occurrence inside the symbol range is excluded...
assert all(r["path"] != "service.py" for r in res["references"])
# ...and the test-file hit is grouped as a related test.
assert res["relatedTests"] == [
    {
        "path": "tests/test_service.py",
        "referenceLines": [5],
        "reason": "Exact symbol reference in a test-like path",
    }
]
assert res["note"] == (
    "Live source is authoritative. References are lexical matches, not proven "
    "callers or callees."
)
# not_indexed, and calling the tool must NOT create .semantic_index.
assert res["indexState"] == "not_indexed"
assert not (ctx_root / ".semantic_index").exists()

# a file's own imports are surfaced as {line, text} entries
res_c = ix.symbol_context(str(ctx_root), "caller.py", 5)
assert res_c["imports"] == [{"line": 1, "text": "import service"}]

# unnamed fallback block: line 1 sits above the parsed def block (4-6)
res_fb = ix.symbol_context(str(ctx_root), "service.py", 1)
assert res_fb["symbol"]["name"] is None and res_fb["symbol"]["kind"] == "block"
assert [res_fb["symbol"]["startLine"], res_fb["symbol"]["endLine"]] == [1, 6]
assert res_fb["references"] == []
# generic window-chunk names ('lines-1-2') count as unnamed too
res_notes = ix.symbol_context(str(ctx_root), "notes.py", 1)
assert res_notes["symbol"]["name"] is None and res_notes["symbol"]["kind"] == "block"

# multiline signatures remain part of the symbol, and an interior body line
# resolves to that symbol rather than an unnamed fallback window.
res_multiline = ix.symbol_context(str(ctx_root), "multiline.py", 5)
assert res_multiline["symbol"]["name"] == "combine"
assert res_multiline["symbol"]["kind"] == "function"
assert [
    res_multiline["symbol"]["startLine"],
    res_multiline["symbol"]["endLine"],
] == [1, 8]
assert [
    res_multiline["snippet"]["startLine"],
    res_multiline["snippet"]["endLine"],
] == [1, 8]
assert "return total" in res_multiline["snippet"]["text"]

# reference limit is enforced (max_references clamped to >= 1)
res_lim = ix.symbol_context(str(ctx_root), "service.py", 6, max_references=1)
assert [(r["path"], r["line"]) for r in res_lim["references"]] == [("caller.py", 5)]
assert ix.symbol_context(str(ctx_root), "service.py", 6, max_references=0)[
    "references"
] == res_lim["references"]

# invalid inputs are rejected with ValueError
for bad_line in (0, 7, 9999):
    try:
        ix.symbol_context(str(ctx_root), "service.py", bad_line)
        raise AssertionError(f"line {bad_line} accepted")
    except ValueError:
        pass
try:
    ix.symbol_context(str(ctx_root), "../outside.py", 1)
    raise AssertionError("path traversal accepted")
except ValueError:
    pass
try:
    ix.symbol_context(str(ctx_root), str(ctx_root / "service.py"), 1)
    raise AssertionError("absolute path accepted")
except ValueError:
    pass
try:
    ix.symbol_context(str(ctx_root / "missing_dir"), "service.py", 1)
    raise AssertionError("missing directory accepted")
except ValueError:
    pass

# index freshness: stale (hash mismatch) then current (hash match)
store2 = VectorStore(ctx_root)
ctx_chunk = StoredChunk(
    "service.py", 4, 6, "python", "function", "compute_retry_delay", "def ..."
)
store2.replace_file("service.py", "deadbeef", 1.0, 10, "python", [ctx_chunk], [[0.0]])
assert ix.symbol_context(str(ctx_root), "service.py", 6)["indexState"] == "stale"
store2.replace_file(
    "service.py", hash_file(ctx_root / "service.py"), 1.0, 10, "python",
    [ctx_chunk], [[0.0]],
)
assert ix.symbol_context(str(ctx_root), "service.py", 6)["indexState"] == "current"
store2.close()
print("SYMBOL CONTEXT TESTS PASSED")

print("SMOKE TEST PASSED")
