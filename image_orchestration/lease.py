"""Cooperative cross-process GPU lease and the embedding-side admission gate.

Every GPU-using call acquires the same OS advisory file lease. While the
journal holds an unfinished transaction, admission raises ``recovery_required``
instead of letting an embedding request reload the LLM onto the GPU. When the
feature is disabled (no ``IMAGE_MODEL``) the gate is a pass-through.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import anyio

from .errors import Fault
from .journal import Journal
from .settings import Settings


@asynccontextmanager
async def file_lease(root: Path, seconds: float) -> AsyncIterator[None]:
    """Cooperative OS lock. Same absolute path for host, tools and embeddings."""
    await anyio.to_thread.run_sync(lambda: root.mkdir(parents=True, exist_ok=True))
    handle = await anyio.to_thread.run_sync(lambda: (root / "gpu.lock").open("a+b"))
    acquired = False
    try:
        with anyio.fail_after(seconds):
            while not acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (BlockingIOError, PermissionError):
                    await anyio.sleep(0.1)
        yield
    finally:
        # close() releases this process's lock even on cancellation.
        handle.close()


@asynccontextmanager
async def gpu_session(settings: Settings) -> AsyncIterator[None]:
    """Host/embedding entry gate. Never hold this while dispatching MCP tools."""
    async with file_lease(settings.state_root, settings.lease_seconds):
        if any(not r["complete"] for r in await Journal(settings.state_root).rows()):
            raise Fault("recovery_required", "GPU recovery must finish before inference")
        yield


class InferenceGate:
    """Admission gate for embedding/search work sharing the GPU with images.

    Disabled (no image feature configured): pass-through. Enabled: every session
    takes the shared lease and is rejected outright while a transaction is under
    recovery, so no embedding request can undo the unload.
    """

    def __init__(self, settings: Settings | None):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings is not None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        if self.settings is None:
            yield
            return
        async with gpu_session(self.settings):
            yield
