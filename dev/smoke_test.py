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
    print("SMOKE TEST PASSED")
