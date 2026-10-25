"""Durability, concurrency, retrieval and real MCP contracts; no model services."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_memory.store import MAX_CONTENT, MemoryFault, MemoryStore


class MemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="memory_test_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = str(self.root)
        self.store = MemoryStore()

    def remember(self, key="decisions/database", **fields):
        values = {"title": "Use SQLite", "content": "Keep project state in SQLite for offline use.",
                  "category": "decision", "tags": ["storage", "offline"], "source": "User approved design"}
        values.update(fields)
        return self.store.remember(self.directory, key, **values)

    def test_persistence_across_server_processes_and_models(self):
        saved = self.remember()["memory"]
        loaded = MemoryStore().get(self.directory, saved["key"])["memory"]
        self.assertEqual(saved, loaded)
        program = (
            "import json,sys; from agent_memory.store import MemoryStore; "
            "print(json.dumps(MemoryStore().recall(sys.argv[1], 'SQLite')))"
        )
        result = subprocess.run([sys.executable, "-B", "-c", program, self.directory],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["results"][0]["key"], saved["key"])

    def test_reads_and_missing_mutations_do_not_create_state(self):
        self.assertEqual(self.store.recall(self.directory)["results"], [])
        self.assertEqual(self.store.get(self.directory, "missing")["status"], "not_found")
        self.assertEqual(self.store.update(self.directory, "missing", 1, content="new")["status"], "not_found")
        self.assertEqual(self.store.forget(self.directory, "missing", 1)["outcome"], "already_absent")
        self.assertFalse((self.root / ".agent_memory").exists())

    def test_reads_leave_existing_database_unchanged(self):
        self.remember()
        path = self.store.database_path(self.directory)
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.store.recall(self.directory, "storage")
        self.store.get(self.directory, "decisions/database")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_project_isolation_and_code_index_independence(self):
        (self.root / ".semantic_index").mkdir()
        vector = self.root / ".semantic_index" / "index.db"
        graph = self.root / ".semantic_index" / "graph.json"
        vector.write_bytes(b"vector sentinel")
        graph.write_text("graph sentinel", encoding="utf-8")
        self.remember()
        other = self.root / "other"
        other.mkdir()
        self.assertEqual(self.store.recall(str(other))["results"], [])
        self.assertEqual(vector.read_bytes(), b"vector sentinel")
        self.assertEqual(graph.read_text(), "graph sentinel")
        vector.write_bytes(b"rebuilt vector")
        self.assertEqual(MemoryStore().get(self.directory, "decisions/database")["status"], "ok")

    def test_create_idempotency_and_conflict_never_overwrite(self):
        first = self.remember()["memory"]
        retry = self.remember()
        self.assertEqual(retry["outcome"], "unchanged")
        self.assertEqual(retry["memory"], first)
        conflict = self.remember(content="Replace with PostgreSQL")
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(self.store.get(self.directory, first["key"])["memory"], first)

    def test_update_changes_search_and_preserves_omitted_fields(self):
        first = self.remember()["memory"]
        updated = self.store.update(self.directory, first["key"], first["revision"],
                                    title="Use Postgres", content="New consensus: PostgreSQL for hosting.", tags=[])
        record = updated["memory"]
        self.assertGreater(record["revision"], first["revision"])
        self.assertEqual(record["source"], first["source"])
        self.assertEqual(record["created_at"], first["created_at"])
        self.assertEqual(record["tags"], [])
        self.assertEqual(self.store.recall(self.directory, "SQLite")["results"], [])
        self.assertEqual(self.store.recall(self.directory, "PostgreSQL")["results"][0]["key"], first["key"])
        self.assertEqual(self.store.update(self.directory, first["key"], first["revision"], content="stale")["status"], "conflict")
        unchanged = self.store.update(self.directory, first["key"], record["revision"], title=record["title"])
        self.assertEqual(unchanged["memory"], record)

    def test_forget_removes_note_tags_search_and_protects_recreated_keys(self):
        first = self.remember()["memory"]
        self.assertEqual(self.store.forget(self.directory, first["key"], first["revision"] + 1)["status"], "conflict")
        self.assertEqual(self.store.forget(self.directory, first["key"], first["revision"])["outcome"], "deleted")
        self.assertEqual(self.store.get(self.directory, first["key"])["status"], "not_found")
        self.assertEqual(self.store.recall(self.directory, "SQLite")["results"], [])
        self.assertEqual(self.store.forget(self.directory, first["key"], first["revision"])["outcome"], "already_absent")
        with closing(sqlite3.connect(self.store.database_path(self.directory))) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM memory_tags").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM memory_search").fetchone()[0], 0)
        second = self.remember(content="New decision with the same key")["memory"]
        self.assertGreater(second["revision"], first["revision"])
        self.assertEqual(self.store.forget(self.directory, first["key"], first["revision"])["status"], "conflict")
        self.assertEqual(self.store.update(self.directory, first["key"], first["revision"], content="stale")["status"], "conflict")

    def test_pinned_recent_pagination_and_bounded_previews(self):
        self.remember("old", pinned=True, content="x" * 2000)
        self.remember("middle")
        self.remember("recent")
        first = self.store.recall(self.directory, limit=2)
        self.assertEqual([r["key"] for r in first["results"]], ["old", "recent"])
        self.assertTrue(first["has_more"])
        self.assertEqual(len(first["results"][0]["preview"]), 1000)
        self.assertTrue(first["results"][0]["content_truncated"])
        last = self.store.recall(self.directory, limit=2, offset=first["next_offset"])
        self.assertEqual([r["key"] for r in last["results"]], ["middle"])
        self.assertFalse(last["has_more"])
        self.assertEqual(len(self.store.get(self.directory, "old")["memory"]["content"]), 2000)

    def test_fulltext_filters_stemming_unicode_and_escaped_queries(self):
        self.remember("lesson", title="Rendering fix", category="lesson", tags=["GPU", "gpu", "Engine"],
                      content="Rendered frames fail when alpha is missing. Café rendering notes.")
        self.remember("task", category="task", tags=["gpu"], content="Test render regressions")
        found = self.store.recall(self.directory, "render", category="lesson", tags=["GPU", "engine"])
        self.assertEqual([r["key"] for r in found["results"]], ["lesson"])
        self.assertEqual(found["results"][0]["tags"], ["engine", "gpu"])
        self.assertEqual(self.store.recall(self.directory, "café")["results"][0]["key"], "lesson")
        for query in ('" OR * NEAR(', "'; DROP TABLE memories; --", "title:alpha"):
            self.assertEqual(self.store.recall(self.directory, query)["status"], "ok")
        self.assertEqual(self.store.get(self.directory, "lesson")["status"], "ok")

    def test_concurrent_creation_and_update_conflicts(self):
        def create(index):
            return MemoryStore().remember(self.directory, "shared", "Shared note", f"writer {index}")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(create, range(6)))
        self.assertEqual(sum(r["status"] == "ok" for r in results), 1)
        revision = self.store.get(self.directory, "shared")["memory"]["revision"]
        def update(index):
            return MemoryStore().update(self.directory, "shared", revision, content=f"updated by {index}")
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(update, range(6)))
        self.assertEqual(sum(r["status"] == "ok" for r in results), 1)
        self.assertEqual(len(self.store.recall(self.directory)["results"]), 1)

    def test_failed_index_update_rolls_back_note_and_revision(self):
        original = self.remember()["memory"]
        with patch.object(self.store, "_index", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.Error):
                self.store.update(self.directory, original["key"], original["revision"], content="uncommitted")
            with self.assertRaises(sqlite3.Error):
                self.remember("new")
        self.assertEqual(self.store.get(self.directory, original["key"])["memory"], original)
        self.assertEqual(self.store.get(self.directory, "new")["status"], "not_found")
        self.assertEqual(self.store.recall(self.directory, "uncommitted")["results"], [])

    def test_invalid_inputs_fail_before_creating_storage(self):
        for kwargs in ({"content": " "}, {"content": "x" * (MAX_CONTENT + 1)},
                       {"category": "invalid"}, {"tags": ["x"] * 21}, {"title": "bad\x00title"},
                       {"key": "'bad"}, {"pinned": 2}):
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(ValueError):
                self.remember(**kwargs)
        with self.assertRaises(ValueError):
            self.store.recall(self.directory, "***")
        with self.assertRaises(ValueError):
            self.store.recall(self.directory, limit=1000)
        with self.assertRaises(ValueError):
            self.store.update(self.directory, "key", 1)
        with self.assertRaises(ValueError):
            self.store.get("relative", "key")
        self.assertFalse((self.root / ".agent_memory").exists())
        (self.root / ".agent_memory").write_text("occupied path", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.recall(self.directory)

    def test_future_and_unrelated_databases_are_not_overwritten(self):
        self.remember()
        path = self.store.database_path(self.directory)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("PRAGMA user_version=99")
        before = path.read_bytes()
        for action in (lambda: self.remember("new"), lambda: self.store.recall(self.directory)):
            with self.assertRaises(MemoryFault):
                action()
        self.assertEqual(path.read_bytes(), before)
        unrelated = self.root / "unrelated"
        (unrelated / ".agent_memory").mkdir(parents=True)
        path = unrelated / ".agent_memory" / "memory.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
        with self.assertRaises(MemoryFault):
            self.store.remember(str(unrelated), "key", "title", "body")

    def test_linked_database_is_rejected(self):
        self.remember()
        other = self.root / "other"
        (other / ".agent_memory").mkdir(parents=True)
        os.link(self.store.database_path(self.directory), other / ".agent_memory" / "memory.db")
        with self.assertRaises(ValueError):
            self.store.recall(str(other))


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_schemas_and_complete_lifecycle(self):
        from mcp import Client
        from mcp.server.mcpserver import MCPServer
        from agent_memory.tool import register

        with tempfile.TemporaryDirectory(prefix="memory_mcp_") as directory:
            server = MCPServer("memory-test")
            register(server)
            async with Client(server) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                self.assertEqual(set(tools), {"memory_remember", "memory_recall", "memory_get", "memory_update", "memory_forget"})
                self.assertIn("expected_revision", tools["memory_update"].input_schema["required"])
                self.assertTrue(tools["memory_recall"].annotations.read_only_hint)
                self.assertTrue(tools["memory_forget"].annotations.destructive_hint)
                async def call(name, **kwargs):
                    result = await client.call_tool(name, {"directory_path": directory, **kwargs})
                    wire = result.model_dump(by_alias=True)
                    self.assertEqual(json.loads(wire["content"][0]["text"]), wire["structuredContent"])
                    return result
                empty = await call("memory_recall")
                self.assertEqual(empty.structured_content["results"], [])
                saved = await call("memory_remember", key="session/current", title="Next steps", content="Finish terrain tests.", category="summary")
                self.assertFalse(saved.is_error)
                revision = saved.structured_content["memory"]["revision"]
                recalled = await call("memory_recall", query="terrain")
                self.assertEqual(recalled.structured_content["results"][0]["key"], "session/current")
                loaded = await call("memory_get", key="session/current")
                self.assertEqual(loaded.structured_content["memory"]["content"], "Finish terrain tests.")
                changed = await call("memory_update", key="session/current", expected_revision=revision, content="Terrain tests passed.", tags=[])
                self.assertFalse(changed.is_error)
                stale = await call("memory_forget", key="session/current", expected_revision=revision)
                self.assertTrue(stale.is_error)
                self.assertEqual(stale.structured_content["status"], "conflict")
                removed = await call("memory_forget", key="session/current", expected_revision=changed.structured_content["memory"]["revision"])
                self.assertFalse(removed.is_error)
                self.assertEqual((await call("memory_recall", query="terrain")).structured_content["results"], [])

    async def test_storage_errors_are_diagnostics(self):
        from agent_memory.tool import _call
        with tempfile.TemporaryDirectory(prefix="memory_error_") as directory:
            path = Path(directory) / ".agent_memory"
            path.mkdir()
            (path / "memory.db").write_bytes(b"not a database")
            result = await _call(MemoryStore(), "recall", directory_path=directory)
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content["error"]["code"], "memory_storage_error")
            with patch.object(MemoryStore, "recall", side_effect=sqlite3.OperationalError("database is locked")):
                result = await _call(MemoryStore(), "recall", directory_path=directory)
                self.assertEqual(result.structured_content["error"]["code"], "memory_busy")


if __name__ == "__main__":
    unittest.main()
