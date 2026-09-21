"""Tests for image_orchestration: lease, gate, journal, orchestrator, tool.

Plain asserts + asyncio.run; run with dev/run.ps1. No live Ollama/ComfyUI:
all upstream traffic hits an in-process httpx.MockTransport fake.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from image_orchestration import tool as image_tool
from image_orchestration.errors import Fault
from image_orchestration.journal import Journal
from image_orchestration.lease import InferenceGate, file_lease
from image_orchestration.orchestrator import Orchestrator
from image_orchestration.settings import Settings

MODEL = "qwen2.5-coder:3b"
DIGEST = "sha256:aaaabbbbcccc"
GB = 1024**3


class FakeUpstream:
    """Stand-ins for Ollama (port 11434) and ComfyUI (port 8188)."""

    def __init__(self, prompt_id: str, busy: bool = False):
        self.resident = True
        self.prompt_id = prompt_id
        self.busy = busy
        self.events: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.port == 11434:
            if path == "/api/ps":
                models = ([{"name": MODEL, "digest": DIGEST, "size_vram": 5 * GB,
                           "context_length": 4096}] if self.resident else [])
                return httpx.Response(200, json={"models": models})
            if path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": MODEL, "digest": DIGEST}]})
            if path == "/api/show":
                return httpx.Response(200, json={"capabilities": ["completion"]})
            if path == "/api/generate":
                body = json.loads(request.content)
                if body["keep_alive"] == 0:
                    self.resident = False
                    self.events.append("ollama_unload")
                else:
                    self.resident = True
                    self.events.append("ollama_reload")
                return httpx.Response(200, json={})
        else:
            if path == "/queue":
                running = [[999, "other-job"]] if self.busy else []
                return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
            if path == "/system_stats":
                return httpx.Response(200, json={"devices": [{"type": "cuda", "index": 0,
                    "vram_free": 30 * GB, "torch_vram_free": 1, "torch_vram_total": 1024}]})
            if path == "/free":
                self.events.append("comfy_free")
                return httpx.Response(200, json={})
            if path == "/prompt":
                self.events.append("comfy_prompt")
                return httpx.Response(200, json={"prompt_id": self.prompt_id})
            if path.startswith("/history/"):
                pid = path.rsplit("/", 1)[1]
                return httpx.Response(200, json={pid: {"status": {"status_str": "success",
                    "completed": True}, "outputs": {"9": {"images": [
                    {"type": "output", "filename": "img.png", "subfolder": ""}]}}}})
        raise AssertionError(f"unexpected request {request.method} {request.url}")


def make_settings(tmp: Path) -> Settings:
    out = tmp / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "img.png").write_bytes(b"not-a-png")
    return Settings(
        model=MODEL, load_options={"num_ctx": 4096},
        workflow={
            "3": {"class_type": "KSampler", "inputs": {"seed": 0}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": ""}},
        },
        output_root=out, state_root=tmp / "state", image_vram_bytes=8 * GB,
        ollama_url="http://127.0.0.1:11434", comfy_url="http://127.0.0.1:8188",
        poll_seconds=0.01, lease_seconds=10, unload_seconds=10,
        generation_seconds=20, cleanup_seconds=20, drain_seconds=10,
        free_seconds=10, reload_seconds=20,
    )


def make_orchestrator(tmp: Path, busy: bool = False):
    settings = make_settings(Path(tmp))
    fake = FakeUpstream(str(uuid4()), busy=busy)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return Orchestrator(settings, http), fake, settings


class DummyServer:
    """Captures the decorated tool function without a real MCPServer."""

    def __init__(self):
        self.fn = None

    def tool(self, **kwargs):
        def deco(fn):
            self.fn = fn
            return fn
        return deco


def ctx_for(orch, settings):
    lc = {"orchestrator": orch, "gate": InferenceGate(settings)}
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lc))


async def seed_unfinished(orch) -> dict:
    job = {"request_id": str(uuid4()), "fingerprint": "f",
           "recovery_target": orch.recovery_target(), "unload_unknown": False,
           "snapshot": {"name": MODEL, "digest": DIGEST, "size_vram": 5 * GB,
                        "context_length": 4096, "options": {"num_ctx": 4096},
                        "keep_alive": "10m"},
           "phase": "unloading", "complete": False, "submission_unknown": False,
           "comfy_touched": False, "prompt_id": None, "image_paths": [],
           "llm_restored": False, "error": None, "recovery_error": None}
    await orch.journal.save(job)
    return job


def test_gate_pass_through_when_disabled():
    gate = InferenceGate(None)
    assert not gate.enabled

    async def run():
        async with gate.session():
            pass

    asyncio.run(run())


def test_lease_is_exclusive_and_releasable():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        async def run():
            async with file_lease(root, 0.3):
                try:
                    async with file_lease(root, 0.3):
                        raise AssertionError("lease re-acquired while held")
                except TimeoutError:
                    pass
            async with file_lease(root, 0.3):  # released, so this succeeds
                pass

        asyncio.run(run())


def test_execute_happy_path_and_idempotency():
    with tempfile.TemporaryDirectory() as td:
        orch, fake, settings = make_orchestrator(Path(td))

        async def run():
            rid = str(uuid4())
            res = await orch.execute("a cat", "", rid)
            sc = res.structured_content
            assert sc["status"] == "succeeded", sc
            assert sc["request_id"] == rid and sc["llm_restored"] is True
            assert sc["image_path"].endswith("img.png")
            assert sc["image_paths"] == [sc["image_path"]] and res.is_error is False
            assert fake.resident is True  # chat model restored to the GPU
            order = ["ollama_unload", "comfy_free", "comfy_prompt", "ollama_reload"]
            idx = [fake.events.index(e) for e in order]
            assert idx == sorted(idx), fake.events
            before = list(fake.events)
            res2 = await orch.execute("a cat", "", rid)
            assert res2.structured_content == sc  # replay, no side effects
            assert fake.events == before
            rows = await orch.journal.rows()
            assert len(rows) == 1 and rows[0]["complete"] is True

        asyncio.run(run())


def test_recovery_blocks_admission():
    with tempfile.TemporaryDirectory() as td:
        orch, fake, settings = make_orchestrator(Path(td))

        async def run():
            await seed_unfinished(orch)
            gate = InferenceGate(settings)
            try:
                async with gate.session():
                    raise AssertionError("gate opened while journal unfinished")
            except Fault as exc:
                assert exc.code == "recovery_required"
            try:
                await orch.execute("a cat")
                raise AssertionError("execute admitted while journal unfinished")
            except Fault as exc:
                assert exc.code == "recovery_required"
            assert fake.events == []  # failed closed without touching the GPU

        asyncio.run(run())


def test_comfy_busy_blocks_before_any_gpu_change():
    with tempfile.TemporaryDirectory() as td:
        orch, fake, settings = make_orchestrator(Path(td), busy=True)

        async def run():
            try:
                await orch.execute("a cat")
                raise AssertionError("busy ComfyUI admitted work")
            except Fault as exc:
                assert exc.code == "comfy_busy"
            assert fake.resident is True and fake.events == []

        asyncio.run(run())


def test_recover_completes_interrupted_transaction():
    with tempfile.TemporaryDirectory() as td:
        orch, fake, settings = make_orchestrator(Path(td))

        async def run():
            await seed_unfinished(orch)
            fake.resident = False  # simulate the interrupted mid-transaction state
            recovered = await orch.recover()
            assert len(recovered) == 1 and recovered[0]["phase"] == "complete"
            assert recovered[0]["error"]["code"] == "process_interrupted"
            assert fake.resident is True and "ollama_reload" in fake.events
            rows = await orch.journal.rows()
            assert len(rows) == 1 and rows[0]["complete"] is True

        asyncio.run(run())


def test_tool_handler_wraps_errors():
    with tempfile.TemporaryDirectory() as td:
        orch, fake, settings = make_orchestrator(Path(td))
        dummy = DummyServer()
        image_tool.register(dummy)

        async def run():
            ctx = ctx_for(orch, settings)
            rid = str(uuid4())
            res = await dummy.fn(ctx, "a cat", "", rid)
            assert res.structured_content["status"] == "succeeded"
            off = SimpleNamespace(request_context=SimpleNamespace(
                lifespan_context={"orchestrator": None}))
            res = await dummy.fn(off, "a cat")
            assert res.is_error and res.structured_content["status"] == "failed"
            assert res.structured_content["error"]["code"] == "disabled"
            res = await dummy.fn(ctx, "a dog", "", rid)  # reuse with new inputs
            assert res.structured_content["error"]["code"] == "request_id_conflict"

        asyncio.run(run())


def test_server_disabled_without_image_model():
    from image_orchestration.settings import load_settings
    with patch.dict(os.environ, {"IMAGE_MODEL": ""}):
        assert load_settings() is None


def main():
    tests = [
        test_gate_pass_through_when_disabled,
        test_lease_is_exclusive_and_releasable,
        test_execute_happy_path_and_idempotency,
        test_recovery_blocks_admission,
        test_comfy_busy_blocks_before_any_gpu_change,
        test_recover_completes_interrupted_transaction,
        test_tool_handler_wraps_errors,
        test_server_disabled_without_image_model,
    ]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"OK: {len(tests)} tests passed")


if __name__ == "__main__":
    main()








