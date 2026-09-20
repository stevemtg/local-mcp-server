"""Offline recovery and MCP integration regressions; never access a real GPU."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from unittest.mock import AsyncMock

import anyio
import httpx
from mcp import Client
from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_orchestration_test import make_orchestrator, make_settings, seed_unfinished
from image_orchestration.errors import Fault
from image_orchestration.lease import InferenceGate
from image_orchestration.orchestrator import Orchestrator
from image_orchestration.tool import register

logging.getLogger("image_orchestration.orchestrator").addHandler(logging.NullHandler())
logging.getLogger("image_orchestration.orchestrator").propagate = False
logging.getLogger("httpx").setLevel(logging.WARNING)


class Regressions(unittest.TestCase):
    def run_case(self, callback):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                orch, fake, settings = make_orchestrator(Path(td))
                async with orch.http:
                    await callback(orch, fake, settings)
        anyio.run(run)

    def test_history_failure_during_cleanup_does_not_prevent_reload(self):
        async def case(orch, fake, settings):
            async def fail_history(_):
                raise httpx.ConnectError("history unavailable")
            orch.history = fail_history
            orch.s = replace(settings, generation_seconds=.03)
            result = (await orch.execute("test")).structured_content
            self.assertEqual(result["error"]["code"], "timeout")
            self.assertTrue(result["llm_restored"])
            self.assertTrue(fake.resident)
        self.run_case(case)

    def test_history_hang_has_separate_bounded_cleanup_budget(self):
        async def case(orch, fake, settings):
            async def hang(_):
                await anyio.sleep_forever()
            orch.history = hang
            orch.s = replace(settings, generation_seconds=.03, cleanup_seconds=1)
            result = (await orch.execute("test")).structured_content
            self.assertTrue(result["llm_restored"])
        self.run_case(case)

    def test_lost_unload_acknowledgement_blocks_late_mutation_race(self):
        async def case(orch, fake, settings):
            original = orch.request
            async def lose_ack(backend, method, path, **kwargs):
                if path == "/api/generate" and kwargs["json"]["keep_alive"] == 0:
                    await original(backend, method, path, **kwargs)
                    raise httpx.ReadTimeout("unload acknowledgement lost")
                return await original(backend, method, path, **kwargs)
            orch.request = lose_ack
            result = (await orch.execute("test")).structured_content
            self.assertEqual(result["status"], "recovery_required")
            self.assertEqual(result["recovery_error"]["code"], "unload_unknown")
            self.assertEqual(fake.events, ["ollama_unload"])
            with self.assertRaises(Fault):
                await orch.recover()
            with self.assertRaises(Fault):
                async with InferenceGate(settings).session():
                    self.fail("Dirty GPU gate opened")
        self.run_case(case)

    def test_recovery_rejects_changed_backend_without_side_effects(self):
        async def case(orch, fake, settings):
            await seed_unfinished(orch)
            changed = Orchestrator(replace(settings, comfy_url="http://127.0.0.1:9999"), orch.http)
            with self.assertRaises(Fault) as caught:
                await changed.recover()
            self.assertEqual(caught.exception.code, "recovery_target_changed")
            self.assertEqual(fake.events, [])
        self.run_case(case)

    def test_legacy_unfinished_journal_requires_audited_reconciliation(self):
        async def case(orch, fake, settings):
            job = await seed_unfinished(orch)
            del job["recovery_target"]
            await orch.journal.save(job)
            with self.assertRaises(Fault):
                await orch.recover()
            self.assertEqual(fake.events, [])
        self.run_case(case)

    def test_reload_failure_preserves_saved_image(self):
        async def case(orch, fake, settings):
            original = orch.request
            async def fail_reload(backend, method, path, **kwargs):
                if path == "/api/generate" and kwargs["json"]["keep_alive"] != 0:
                    raise httpx.ConnectError("runner unavailable")
                return await original(backend, method, path, **kwargs)
            orch.request = fail_reload
            orch.s = replace(settings, reload_seconds=.05)
            result = (await orch.execute("test")).structured_content
            self.assertEqual(result["status"], "recovery_required")
            self.assertTrue(Path(result["image_path"]).is_file())
            self.assertFalse(result["llm_restored"])
            self.assertFalse((await orch.journal.rows())[0]["complete"])
        self.run_case(case)

    def test_cancellation_waits_for_restoration(self):
        async def case(orch, fake, settings):
            started = anyio.Event()
            scopes = []
            async def wait_image(_):
                started.set()
                await anyio.sleep_forever()
            orch.wait_image = wait_image
            async def caller():
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    await orch.execute("test")
            async with anyio.create_task_group() as group:
                group.start_soon(caller)
                await started.wait()
                scopes[0].cancel()
            self.assertTrue(fake.resident)
            job = (await orch.journal.rows())[0]
            self.assertTrue(job["complete"])
            self.assertEqual(job["error"]["code"], "cancelled")
        self.run_case(case)

    def test_real_mcp_context_lifespan_and_serialized_result(self):
        async def case(orch, fake, settings):
            @asynccontextmanager
            async def lifespan(_):
                yield {"orchestrator": orch, "gate": InferenceGate(settings)}
            server = MCPServer("test-images", lifespan=lifespan)
            register(server)
            async with Client(server) as client:
                result = await client.call_tool("generate_image", {"prompt": "test"})
                wire = result.model_dump(by_alias=True)
                self.assertFalse(wire["isError"])
                self.assertTrue(wire["structuredContent"]["llm_restored"])
        self.run_case(case)

    def test_existing_async_tools_keep_context_out_of_schema(self):
        async def case(orch, fake, settings):
            with patch.dict(os.environ, {"IMAGE_MODEL": ""}):
                import server
            with patch.object(server, "image_settings", None), patch.object(
                server.indexer, "index_directory", return_value={"test": "indexed"}
            ), patch.object(server.indexer, "semantic_search", return_value={"results": []}), patch.object(
                server.indexer, "similar", return_value={"results": []}
            ):
                async with Client(server.server) as client:
                    listed = await client.list_tools()
                    self.assertEqual(len(listed.tools), 5)
                    for tool in listed.tools:
                        self.assertNotIn("ctx", tool.input_schema.get("properties", {}))
                    for name, args in (("index_codebase", {"directory_path": str(settings.output_root)}),
                        ("semantic_search", {"directory_path": str(settings.output_root), "query": "test"}),
                        ("semantic_search_similar", {"directory_path": str(settings.output_root), "path": "img.png"})):
                        result = await client.call_tool(name, args)
                        self.assertFalse(result.is_error, result)
        self.run_case(case)

    def test_invalid_settings_fail_before_unloading(self):
        with tempfile.TemporaryDirectory() as td:
            settings = make_settings(Path(td))
            cases = [{"poll_seconds": float("nan")}, {"cleanup_seconds": float("inf")},
                     {"reload_headroom_bytes": -1}, {"keep_alive": "0"}, {"keep_alive": "0s"},
                     {"load_options": {"num_ctx": True}}, {"load_options": {"num_ctx": "4096"}},
                     {"positive_node": "7"}, {"workflow": {}},
                     {"comfy_url": "http://127.0.0.1:8188/?wrong=true"}]
            for kwargs in cases:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    replace(settings, **kwargs)

    def test_unfinished_result_cannot_report_success(self):
        job = {"request_id": "test", "prompt_id": "test", "image_paths": ["saved.png"],
               "llm_restored": True, "error": None, "complete": False,
               "recovery_error": {"code": "restore_failed"}}
        result = Orchestrator.result(job)
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["status"], "recovery_required")

    def test_driver_probe_prevents_admission_when_comfy_overstates_free_memory(self):
        async def case(orch, fake, settings):
            orch.s = replace(settings, nvidia_smi_path=sys.executable, free_seconds=.03)
            result = type("ProbeResult", (), {"stdout": b"1024\n"})()
            with patch("image_orchestration.orchestrator.anyio.run_process", new=AsyncMock(return_value=result)):
                with self.assertRaises(TimeoutError):
                    await orch.wait_memory(8 * 1024**3, True)
            self.assertNotIn("comfy_prompt", fake.events)
        self.run_case(case)

    def test_cpu_embeddings_set_placement_without_changing_default_requests(self):
        from semantic_index.embedder import OllamaEmbedder
        for placement in (None, 0):
            with self.subTest(placement=placement):
                def respond(request, timeout):
                    payload = json.loads(request.data)
                    if placement is None:
                        self.assertNotIn("options", payload)
                    else:
                        self.assertEqual(payload["options"], {"num_gpu": 0})
                    return io.BytesIO(b'{"embedding":[0.1,0.2]}')
                with patch("semantic_index.embedder.urllib.request.urlopen", side_effect=respond):
                    self.assertEqual(OllamaEmbedder(num_gpu=placement).embed_one("test"), [0.1, 0.2])

    def test_indefinite_keepalive_is_numeric_on_the_wire(self):
        async def case(orch, fake, settings):
            orch.s = replace(settings, keep_alive="-1")
            original = orch.request
            async def check(backend, method, path, **kwargs):
                if path == "/api/generate" and kwargs["json"]["keep_alive"] != 0:
                    self.assertEqual(kwargs["json"]["keep_alive"], -1)
                    self.assertIs(type(kwargs["json"]["keep_alive"]), int)
                return await original(backend, method, path, **kwargs)
            orch.request = check
            result = await orch.execute("test")
            self.assertFalse(result.is_error)
        self.run_case(case)

    def test_recovery_preserves_successful_generation_after_reload_failure(self):
        async def case(orch, fake, settings):
            job = await seed_unfinished(orch)
            job["image_paths"] = [str(settings.output_root / "img.png")]
            job["recovery_error"] = {"code": "reload_failed"}
            await orch.journal.save(job)
            recovered = (await orch.recover())[0]
            self.assertFalse(orch.result(recovered).is_error)
        self.run_case(case)


if __name__ == "__main__":
    unittest.main()
