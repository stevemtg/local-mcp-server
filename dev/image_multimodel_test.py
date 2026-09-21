"""Offline tests of discovery, unloading, and recovery of all Ollama models."""
from __future__ import annotations

import copy
import json
import logging
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import anyio
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_orchestration_test import FakeUpstream, MODEL, DIGEST, GB, make_settings
from image_orchestration.orchestrator import Orchestrator

logging.getLogger("image_orchestration.orchestrator").addHandler(logging.NullHandler())
logging.getLogger("image_orchestration.orchestrator").propagate = False

EMBED = "nomic-embed-text:latest"
CODER = "qwen2.5-coder:1.5b-base"
CPU = "cpu-helper:latest"


class MultiUpstream(FakeUpstream):
    def __init__(self):
        super().__init__(str(uuid4()))
        self.available = {
            name: {"name": name, "digest": digest, "size_vram": vram, "context_length": ctx}
            for name, digest, vram, ctx in (
                (MODEL, DIGEST, 5 * GB, 4096),
                (EMBED, "embed-digest", GB, 8192),
                (CODER, "coder-digest", 2 * GB, 2048),
                (CPU, "cpu-digest", 0, 1024),
            )
        }
        self.loaded = copy.deepcopy(self.available)
        self.loads = []
        self.fail_load = None
        self.evict_on_last_load = False
        self.lose_unload_ack = None
        self.journal_check = None

    def handler(self, request):
        path = request.url.path
        if request.url.port == 11434:
            if path == "/api/ps":
                return httpx.Response(200, json={"models": list(self.loaded.values())})
            if path == "/api/tags":
                return httpx.Response(200, json={"models": list(self.available.values())})
            body = json.loads(request.content)
            name = body["model"]
            if path == "/api/show":
                caps = ["embedding"] if name == EMBED else ["completion"]
                return httpx.Response(200, json={"capabilities": caps})
            if path in {"/api/generate", "/api/embed"}:
                if body["keep_alive"] == 0:
                    if self.journal_check:
                        self.journal_check()
                    self.loaded.pop(name, None)
                    self.events.append(("unload", name))
                    if name == self.lose_unload_ack:
                        raise httpx.ReadTimeout("unload acknowledgement lost")
                else:
                    self.loads.append((path, body))
                    if name == self.fail_load:
                        raise httpx.ConnectError("model unavailable")
                    assert path == ("/api/embed" if name == EMBED else "/api/generate")
                    if name == EMBED:
                        assert body["input"] == ""
                    assert body["options"]["num_ctx"] == self.available[name]["context_length"]
                    if name == CPU:
                        assert body["options"]["num_gpu"] == 0
                    self.loaded[name] = copy.deepcopy(self.available[name])
                    self.events.append(("reload", name))
                    if self.evict_on_last_load and name == CPU:
                        self.loaded.pop(MODEL, None)
                return httpx.Response(200, json={})
        if path == "/prompt":
            assert not self.loaded, "Image submitted while Ollama models were loaded"
        return super().handler(request)


class MultiModelTests(unittest.TestCase):
    def run_case(self, callback):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                settings = make_settings(Path(td))
                fake = MultiUpstream()
                async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
                    orch = Orchestrator(settings, http)
                    await callback(orch, fake)
        anyio.run(run)

    def test_all_models_are_journaled_unloaded_and_restored(self):
        async def case(orch, fake):
            expected = set(fake.loaded)
            def check_journal():
                job = orch.journal._operation("read")[0]
                self.assertEqual({s["name"] for s in job["snapshots"]}, expected)
                self.assertTrue(job["unload_unknown"])
            fake.journal_check = check_journal
            result = (await orch.execute("test")).structured_content
            self.assertEqual(result["status"], "succeeded", result)
            self.assertTrue(result["llm_restored"])
            self.assertEqual(set(fake.loaded), expected)
            prompt_index = fake.events.index("comfy_prompt")
            for name in expected:
                self.assertLess(fake.events.index(("unload", name)), prompt_index)
                self.assertGreater(fake.events.index(("reload", name)), prompt_index)
            job = (await orch.journal.rows())[0]
            self.assertTrue(job["complete"])
            before = list(fake.events)
            await orch.execute("test", request_id=result["request_id"])
            self.assertEqual(fake.events, before)
        self.run_case(case)

    def test_missing_configured_model_and_empty_list(self):
        for names in ((EMBED, CODER), ()):
            with self.subTest(names=names):
                async def case(orch, fake):
                    fake.loaded = {n: fake.loaded[n] for n in names}
                    result = (await orch.execute("test")).structured_content
                    self.assertEqual(result["status"], "succeeded", result)
                    self.assertEqual(set(fake.loaded), set(names))
                self.run_case(case)

    def test_generation_error_restores_every_model(self):
        async def case(orch, fake):
            async def fail(_):
                raise RuntimeError("generation failed")
            orch.wait_image = fail
            result = (await orch.execute("test")).structured_content
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["llm_restored"])
            self.assertEqual(fake.loaded, fake.available)
        self.run_case(case)

    def test_cancellation_restores_every_model(self):
        async def case(orch, fake):
            started = anyio.Event()
            async def wait(_):
                started.set()
                await anyio.sleep_forever()
            orch.wait_image = wait
            async with anyio.create_task_group() as group:
                group.start_soon(orch.execute, "test")
                await started.wait()
                group.cancel_scope.cancel()
            self.assertEqual(fake.loaded, fake.available)
            self.assertTrue((await orch.journal.rows())[0]["complete"])
        self.run_case(case)

    def test_reload_failure_still_attempts_remaining_models_and_can_recover(self):
        async def case(orch, fake):
            fake.fail_load = MODEL
            orch.s = replace(orch.s, reload_seconds=.03)
            result = (await orch.execute("test")).structured_content
            self.assertEqual(result["status"], "recovery_required")
            self.assertFalse(result["llm_restored"])
            self.assertEqual(set(fake.loaded), {EMBED, CODER, CPU})
            self.assertTrue(result["image_path"])
            fake.fail_load = None
            await orch.recover()
            self.assertEqual(fake.loaded, fake.available)
            self.assertTrue((await orch.journal.rows())[0]["complete"])
        self.run_case(case)

    def test_final_check_detects_eviction_by_later_reload(self):
        async def case(orch, fake):
            fake.evict_on_last_load = True
            result = (await orch.execute("test")).structured_content
            self.assertEqual(result["status"], "recovery_required")
            self.assertEqual(result["recovery_error"]["code"], "reload_failed")
            self.assertFalse(result["llm_restored"])
        self.run_case(case)

    def test_second_unload_lost_ack_retains_whole_list_for_recovery(self):
        async def case(orch, fake):
            fake.lose_unload_ack = EMBED
            result = (await orch.execute("test")).structured_content
            self.assertEqual(result["recovery_error"]["code"], "unload_unknown")
            self.assertEqual(fake.events, [("unload", MODEL), ("unload", EMBED)])
            job = (await orch.journal.rows())[0]
            self.assertEqual(len(job["snapshots"]), 4)
            self.assertFalse(job["complete"])
        self.run_case(case)

    def test_failure_after_acknowledged_partial_unload_restores_whole_list(self):
        async def case(orch, fake):
            original = orch.unload
            async def unload(snapshot, job):
                await original(snapshot, job)
                if snapshot["name"] == EMBED:
                    raise TimeoutError("residency polling timed out")
            orch.unload = unload
            result = (await orch.execute("test")).structured_content
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["llm_restored"])
            self.assertEqual(fake.loaded, fake.available)
            self.assertNotIn("comfy_prompt", fake.events)
        self.run_case(case)

    def test_new_model_before_submission_prevents_generation(self):
        async def case(orch, fake):
            original = orch.wait_memory
            async def wait_memory(minimum, require_idle):
                await original(minimum, require_idle)
                fake.loaded[CPU] = copy.deepcopy(fake.available[CPU])
            orch.wait_memory = wait_memory
            result = (await orch.execute("test")).structured_content
            self.assertEqual(result["error"]["code"], "models_reloaded")
            self.assertNotIn("comfy_prompt", fake.events)
            self.assertTrue(result["llm_restored"])
        self.run_case(case)


if __name__ == "__main__":
    unittest.main()
