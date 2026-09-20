"""Offline failure-injection tests: python -m unittest discover -s this_directory."""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import anyio
import httpx

from reference import Fault, Journal, Orchestrator, Settings, file_lease, gpu_session
from reference import register
from mcp.server.mcpserver import MCPServer

logging.getLogger("reference").addHandler(logging.NullHandler())
logging.getLogger("reference").propagate = False
logging.getLogger("httpx").setLevel(logging.WARNING)


class Backend:
    def __init__(self, output: Path):
        self.output = output
        self.loaded = True
        self.generation_error = False
        self.stalled = False
        self.reload_error = False
        self.ambiguous_submit = False
        self.reject_submit = False
        self.bad_path = False
        self.busy = False
        self.calls = []
        self.prompt_id = str(uuid4())
        self.saved = None
        self.started = None
        self.snapshot = {"name": "test:latest", "digest": "test-digest",
                         "size_vram": 1024**3, "context_length": 4096}

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        data = {}
        if path == "/api/ps":
            data = {"models": [self.snapshot] if self.loaded else []}
        elif path == "/api/tags":
            data = {"models": [self.snapshot]}
        elif path == "/api/generate":
            if body["keep_alive"] == 0:
                self.loaded = False
            else:
                if self.reload_error:
                    return httpx.Response(500, json={"error": "load failed"})
                self.loaded = True
            data = {"done": True}
        elif path == "/queue" and request.method == "GET":
            running = [[0, self.prompt_id]] if self.busy else []
            data = {"queue_running": running, "queue_pending": []}
        elif path == "/system_stats":
            data = {"devices": [{"type": "cuda", "index": 0,
                "vram_free": 16 * 1024**3, "torch_vram_free": 0, "torch_vram_total": 0}]}
        elif path == "/prompt":
            if self.reject_submit:
                return httpx.Response(400, json={"error": "invalid workflow", "node_errors": {}})
            if self.ambiguous_submit:
                raise httpx.ReadTimeout("acknowledgement lost", request=request)
            prefix = body["prompt"]["9"]["inputs"]["filename_prefix"]
            self.saved = self.output / (prefix + "_00001_.png")
            self.saved.parent.mkdir(parents=True, exist_ok=True)
            self.saved.write_bytes(b"test-image")
            self.busy = self.stalled
            if self.started:
                self.started.set()
            data = {"prompt_id": self.prompt_id, "node_errors": {}}
        elif path.startswith("/history/"):
            if self.saved and not self.busy:
                data = {self.prompt_id: {
                    "status": {"status_str": "error" if self.generation_error else "success",
                               "completed": not self.generation_error},
                    "outputs": {"9": {"images": [{"type": "output",
                        "filename": self.saved.name,
                        "subfolder": "../../escape" if self.bad_path else "mcp"}]}}
                }}
        elif path == "/interrupt":
            self.busy = False
            self.generation_error = True
        elif path not in {"/free", "/queue"}:
            raise AssertionError(f"Unexpected API request: {path}")
        return httpx.Response(200, json=data)


class ReferenceTests(unittest.TestCase):
    def run_case(self, callback):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                workflow = json.loads(Path(__file__).with_name("workflow.example.json").read_text())
                settings = Settings(model="test:latest", load_options={"num_ctx": 4096},
                    workflow=workflow, output_root=root, state_root=root / "state",
                    image_vram_bytes=2 * 1024**3, poll_seconds=.002,
                    generation_seconds=.06, free_seconds=1,
                    reload_seconds=.08, cleanup_seconds=2)
                backend = Backend(root)
                async with httpx.AsyncClient(transport=httpx.MockTransport(backend.respond)) as http:
                    await callback(Orchestrator(settings, http), backend)
        anyio.run(run)

    def test_success_order_and_idempotent_replay(self):
        async def case(service, backend):
            rid = str(uuid4())
            result = await service.execute("a lighthouse", request_id=rid)
            self.assertFalse(result.is_error)
            self.assertTrue(Path(result.structured_content["image_path"]).is_file())
            routes = [path for path, _ in backend.calls]
            unload = next(i for i, (p, b) in enumerate(backend.calls)
                          if p == "/api/generate" and b["keep_alive"] == 0)
            reload = max(i for i, (p, _) in enumerate(backend.calls) if p == "/api/generate")
            self.assertLess(unload, routes.index("/prompt"))
            self.assertLess(max(i for i, p in enumerate(routes) if p == "/free"), reload)
            replay = await service.execute("a lighthouse", request_id=rid)
            self.assertEqual(replay.structured_content, result.structured_content)
            self.assertEqual(sum(p == "/prompt" for p, _ in backend.calls), 1)
            with self.assertRaises(Fault):
                await service.execute("different", request_id=rid)
        self.run_case(case)

    def test_generation_error_still_restores(self):
        async def case(service, backend):
            backend.generation_error = True
            result = (await service.execute("a lighthouse")).structured_content
            self.assertEqual(result["error"]["code"], "generation_failed")
            self.assertTrue(result["llm_restored"])
            self.assertEqual(result["image_paths"], [])
        self.run_case(case)

    def test_timeout_interrupts_before_reload(self):
        async def case(service, backend):
            backend.stalled = True
            result = (await service.execute("a lighthouse")).structured_content
            self.assertEqual(result["error"]["code"], "timeout")
            self.assertTrue(result["llm_restored"])
            routes = [p for p, _ in backend.calls]
            self.assertLess(routes.index("/interrupt"), max(i for i, p in enumerate(routes) if p == "/free"))
        self.run_case(case)

    def test_reload_failure_preserves_image_and_blocks_gpu_then_recovers(self):
        async def case(service, backend):
            backend.reload_error = True
            result = (await service.execute("a lighthouse")).structured_content
            self.assertEqual(result["status"], "recovery_required")
            self.assertTrue(Path(result["image_path"]).is_file())
            self.assertFalse(result["llm_restored"])
            with self.assertRaises(Fault):
                async with gpu_session(service.s):
                    self.fail("GPU must remain blocked")
            backend.reload_error = False
            await service.recover()
            async with gpu_session(service.s):
                self.assertTrue(backend.loaded)
        self.run_case(case)

    def test_ambiguous_submission_never_requeues_or_reloads(self):
        async def case(service, backend):
            backend.ambiguous_submit = True
            result = (await service.execute("a lighthouse")).structured_content
            self.assertEqual(result["status"], "recovery_required")
            self.assertEqual(result["recovery_error"]["code"], "submission_unknown")
            self.assertFalse(backend.loaded)
            with self.assertRaises(Fault):
                await service.execute("another image")
            with self.assertRaises(Fault):
                await service.recover()
            self.assertEqual(sum(p == "/prompt" for p, _ in backend.calls), 1)
        self.run_case(case)

    def test_validation_rejection_restores(self):
        async def case(service, backend):
            backend.reject_submit = True
            result = (await service.execute("a lighthouse")).structured_content
            self.assertTrue(result["llm_restored"])
            self.assertEqual(result["status"], "failed")
        self.run_case(case)

    def test_cancellation_waits_for_restoration(self):
        async def case(service, backend):
            backend.stalled = True
            backend.started = anyio.Event()
            scopes = []
            async def caller():
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    await service.execute("a lighthouse")
            async with anyio.create_task_group() as group:
                group.start_soon(caller)
                await backend.started.wait()
                scopes[0].cancel()
            self.assertTrue(backend.loaded)
            jobs = await Journal(service.s.state_root).rows()
            self.assertTrue(jobs[0]["complete"])
            self.assertEqual(jobs[0]["error"]["code"], "cancelled")
        self.run_case(case)

    def test_output_escape_is_rejected_and_runner_restored(self):
        async def case(service, backend):
            backend.bad_path = True
            result = (await service.execute("a lighthouse")).structured_content
            self.assertEqual(result["image_paths"], [])
            self.assertTrue(result["llm_restored"])
            self.assertNotEqual(result["status"], "succeeded")
        self.run_case(case)

    def test_busy_comfy_leaves_runner_loaded(self):
        async def case(service, backend):
            backend.busy = True
            with self.assertRaises(Fault):
                await service.execute("a lighthouse")
            self.assertTrue(backend.loaded)
            self.assertFalse(any(p == "/api/generate" for p, _ in backend.calls))
        self.run_case(case)

    def test_lease_deadline_applies_only_to_acquisition(self):
        async def case(service, backend):
            async with file_lease(service.s.state_root, .01):
                await anyio.sleep(.03)
        self.run_case(case)

    def test_lease_excludes_a_second_caller(self):
        async def case(service, backend):
            async with file_lease(service.s.state_root, 1):
                with self.assertRaises(TimeoutError):
                    async with file_lease(service.s.state_root, .02):
                        self.fail("Second caller acquired occupied lease")
        self.run_case(case)

    def test_context_mismatch_fails_before_unload(self):
        async def case(service, backend):
            backend.snapshot["context_length"] = 8192
            with self.assertRaises(Fault):
                await service.execute("a lighthouse")
            self.assertTrue(backend.loaded)
        self.run_case(case)

    def test_mcp_registration_and_error_wire_shape(self):
        async def case(service, backend):
            server = MCPServer("image-test")
            register(server, service)
            tools = await server.list_tools()
            tool = next(t for t in tools if t.name == "generate_image")
            self.assertFalse(tool.annotations.read_only_hint)
            result = await server.call_tool("generate_image", {"prompt": "a lighthouse"})
            wire = result.model_dump(by_alias=True)
            self.assertFalse(wire["isError"])
            self.assertTrue(wire["structuredContent"]["image_path"])
            backend.busy = True
            result = await server.call_tool("generate_image", {"prompt": "another image"})
            self.assertTrue(result.model_dump(by_alias=True)["isError"])
        self.run_case(case)


if __name__ == "__main__":
    unittest.main()
