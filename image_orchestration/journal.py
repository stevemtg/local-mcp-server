"""Durable unfinished-job journal shared by every GPU client process."""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import anyio


class Journal:
    """SQLite FULL synchronous commits before remote side effects; no prompts."""

    def __init__(self, root: Path):
        self.root = root

    def _operation(self, action: str, value: dict | None = None):
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.root / "jobs.sqlite3", timeout=5)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS jobs "
                       "(id TEXT PRIMARY KEY, complete INTEGER, body TEXT)")
            if action == "save":
                assert value is not None
                db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?)",
                           (value["request_id"], int(value["complete"]),
                            json.dumps(value, sort_keys=True)))
                return None
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM jobs")]

    async def rows(self) -> list[dict]:
        return await anyio.to_thread.run_sync(self._operation, "read")

    async def save(self, job: dict) -> None:
        # Do not abandon an in-flight durable write on cancellation.
        await anyio.to_thread.run_sync(self._operation, "save", copy.deepcopy(job))
