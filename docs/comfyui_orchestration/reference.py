"""Reviewable Ollama/ComfyUI implementation; not registered by server.py.

Deployment contract: all GPU users take gpu_session(); the host releases its
inference lease before dispatching a tool. Dedicated ComfyUI, one CUDA GPU,
trusted API-format workflow, local output directory. See PLAN.md before use.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Annotated
from uuid import UUID, uuid4

import anyio
import httpx
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

log = logging.getLogger(__name__)


class Fault(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Settings:
    model: str                         # Exact canonical model name, from host.
    load_options: dict[str, Any]        # Same load-affecting options as host.
    workflow: dict[str, Any]            # Trusted ComfyUI API-format graph.
    output_root: Path                   # Actual ComfyUI --output-directory.
    state_root: Path                    # Shared by every GPU client/process.
    image_vram_bytes: int              # Measured budget including headroom.
    ollama_url: str = "http://127.0.0.1:11434"
    comfy_url: str = "http://127.0.0.1:8188"
    positive_node: str = "6"
    negative_node: str = "7"
    sampler_node: str = "3"
    save_node: str = "9"
    gpu_index: int = 0
    idle_torch_bytes: int = 256 * 1024**2
    reload_headroom_bytes: int = 512 * 1024**2
    placement_tolerance_bytes: int = 64 * 1024**2
    keep_alive: str = "10m"
    lease_seconds: float = 10
    unload_seconds: float = 90
    generation_seconds: float = 600
    cleanup_seconds: float = 300
    drain_seconds: float = 30
    free_seconds: float = 30
    reload_seconds: float = 180
    poll_seconds: float = 1

    def __post_init__(self):
        if not self.model or self.load_options.get("num_ctx", 0) <= 0:
            raise ValueError("Set the host's canonical model and explicit num_ctx")
        if self.image_vram_bytes <= 0:
            raise ValueError("Set a measured image VRAM budget")
        for name in ("lease_seconds", "unload_seconds", "generation_seconds",
                     "cleanup_seconds", "drain_seconds", "free_seconds",
                     "reload_seconds", "poll_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not self.output_root.is_absolute() or not self.state_root.is_absolute():
            raise ValueError("Output and shared state roots must be absolute")
        for value in (self.ollama_url, self.comfy_url):
            u = httpx.URL(value)
            if u.scheme != "http" or u.host not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("This local-only reference requires loopback HTTP")


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


class Orchestrator:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s, self.http = settings, http
        self.journal = Journal(settings.state_root)

    async def request(self, backend: str, method: str, path: str, **kwargs):
        base = self.s.ollama_url if backend == "ollama" else self.s.comfy_url
        response = await self.http.request(method, base.rstrip("/") + path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    async def models(self) -> list[dict]:
        return (await self.request("ollama", "GET", "/api/ps"))["models"]

    async def queue(self) -> tuple[set[str], set[str]]:
        q = await self.request("comfy", "GET", "/queue")
        return ({x[1] for x in q["queue_running"]},
                {x[1] for x in q["queue_pending"]})

    async def snapshot(self) -> dict:
        models = await self.models()
        matches = [m for m in models if m["name"] == self.s.model]
        if len(matches) != 1 or matches[0].get("size_vram", 0) <= 0:
            raise Fault("model_not_resident", "Configured chat model is not GPU-resident")
        original = matches[0]
        if any(m["name"] != self.s.model and m.get("size_vram", 0) > 0 for m in models):
            raise Fault("other_gpu_model", "Move embeddings/other models off this GPU first")
        if original.get("context_length") != self.s.load_options["num_ctx"]:
            raise Fault("context_mismatch", "Host num_ctx differs from resident model")
        if not original.get("digest"):
            raise Fault("missing_digest", "Runner did not report the resident model digest")
        return {"name": original["name"], "digest": original["digest"],
                "size_vram": original["size_vram"],
                "context_length": original["context_length"],
                "options": copy.deepcopy(self.s.load_options),
                "keep_alive": self.s.keep_alive}

    def workflow(self, prompt: str, negative: str, request_id: str) -> dict:
        if not prompt.strip() or len(prompt) > 8000 or len(negative) > 4000:
            raise Fault("invalid_input", "Prompt is blank or exceeds the configured limit")
        w = copy.deepcopy(self.s.workflow)
        try:
            for node in (self.s.positive_node, self.s.negative_node):
                if w[node]["class_type"] != "CLIPTextEncode":
                    raise ValueError("Text nodes must be CLIPTextEncode")
            if w[self.s.sampler_node]["class_type"] != "KSampler":
                raise ValueError("Sampler must be KSampler")
            if w[self.s.save_node]["class_type"] != "SaveImage":
                raise ValueError("Output must be SaveImage")
            w[self.s.positive_node]["inputs"]["text"] = prompt
            w[self.s.negative_node]["inputs"]["text"] = negative
            w[self.s.sampler_node]["inputs"]["seed"] = UUID(request_id).int % (2**63)
            w[self.s.save_node]["inputs"]["filename_prefix"] = f"mcp/{request_id}"
        except (KeyError, TypeError, ValueError) as exc:
            raise Fault("invalid_workflow", "Trusted workflow node mapping is invalid") from exc
        return w

    async def unload(self, snapshot: dict) -> None:
        with anyio.fail_after(self.s.unload_seconds):
            await self.request("ollama", "POST", "/api/generate", json={
                "model": snapshot["name"], "prompt": "", "stream": False, "keep_alive": 0
            }, timeout=httpx.Timeout(self.s.unload_seconds, connect=5))
            while any(m["digest"] == snapshot["digest"] for m in await self.models()):
                await anyio.sleep(self.s.poll_seconds)

    async def wait_memory(self, minimum: int, require_idle: bool) -> None:
        """Single CUDA device only. Subtract PyTorch's reusable reserved memory."""
        consecutive = 0
        with anyio.fail_after(self.s.free_seconds):
            while consecutive < 3:
                stats = await self.request("comfy", "GET", "/system_stats")
                devices = [d for d in stats["devices"]
                           if d["type"] == "cuda" and d["index"] == self.s.gpu_index]
                if len(devices) != 1:
                    raise Fault("gpu_probe_unsupported", "Expected configured CUDA device")
                d = devices[0]
                physical_free = d["vram_free"] - d["torch_vram_free"]
                ok = physical_free >= minimum
                if require_idle:
                    ok = ok and d["torch_vram_total"] <= self.s.idle_torch_bytes
                consecutive = consecutive + 1 if ok else 0
                await anyio.sleep(self.s.poll_seconds)

    async def history(self, prompt_id: str) -> dict | None:
        return (await self.request("comfy", "GET", f"/history/{prompt_id}")).get(prompt_id)

    async def wait_image(self, prompt_id: str) -> dict:
        with anyio.fail_after(self.s.generation_seconds):
            while True:
                try:
                    record = await self.history(prompt_id)
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                        raise
                    # Read-only polling can retry safely within the same deadline.
                    await anyio.sleep(self.s.poll_seconds)
                    continue
                if record:
                    status = record["status"]
                    if status["status_str"] == "error":
                        raise Fault("generation_failed", "ComfyUI recorded execution failure")
                    if status["status_str"] == "success" and status["completed"]:
                        return record
                await anyio.sleep(self.s.poll_seconds)

    async def paths(self, record: dict) -> list[str]:
        descriptors = record.get("outputs", {}).get(self.s.save_node, {}).get("images", [])

        def resolve():
            root = self.s.output_root.resolve(strict=True)
            paths = []
            for item in descriptors:
                if item.get("type") != "output":
                    continue
                filename, folder = item["filename"], item.get("subfolder", "")
                if any(c in filename for c in ("/", "\\", ":")) or ":" in folder:
                    raise Fault("unsafe_output", "Unexpected output filename")
                candidate = (root / folder / filename).resolve(strict=True)
                if not candidate.is_relative_to(root) or not candidate.is_file():
                    raise Fault("unsafe_output", "Image escaped the configured output root")
                if candidate.stat().st_size <= 0:
                    raise Fault("empty_output", "Saved image is empty")
                paths.append(str(candidate))
            if not paths:
                raise Fault("missing_output", "No saved output image found in history")
            return paths

        return await anyio.to_thread.run_sync(resolve)

    async def drain(self, prompt_id: str) -> None:
        with anyio.fail_after(self.s.drain_seconds):
            while True:
                running, pending = await self.queue()
                if (running | pending) - {prompt_id}:
                    raise Fault("foreign_job", "Another ComfyUI job violates exclusive ownership")
                if prompt_id in pending:
                    await self.request("comfy", "POST", "/queue", json={"delete": [prompt_id]})
                if prompt_id in running:
                    # Safe even on legacy servers ONLY under dedicated-instance contract.
                    await self.request("comfy", "POST", "/interrupt", json={"prompt_id": prompt_id})
                if not running and not pending:
                    return
                await anyio.sleep(self.s.poll_seconds)

    async def reload(self, snapshot: dict) -> None:
        with anyio.fail_after(self.s.reload_seconds):
            tags = (await self.request("ollama", "GET", "/api/tags"))["models"]
            if not any(m["name"] == snapshot["name"] and m["digest"] == snapshot["digest"]
                       for m in tags):
                raise Fault("model_changed", "Original local model digest is no longer available")
            for attempt in range(3):
                try:
                    await self.request("ollama", "POST", "/api/generate", json={
                        "model": snapshot["name"], "prompt": "", "stream": False,
                        "keep_alive": snapshot["keep_alive"], "options": snapshot["options"]
                    }, timeout=httpx.Timeout(self.s.reload_seconds, connect=5))
                except (httpx.TransportError, httpx.HTTPStatusError):
                    # A lost preload response may still have loaded the model.
                    log.warning("Ollama preload attempt %d failed", attempt + 1)
                try:
                    resident = [m for m in await self.models() if m["name"] == snapshot["name"]]
                except (httpx.TransportError, httpx.HTTPStatusError):
                    resident = []
                if resident:
                    m = resident[0]
                    if (m["digest"] == snapshot["digest"]
                            and m.get("context_length") == snapshot["context_length"]
                            and m.get("size_vram", 0) > 0
                            and m["size_vram"] >= snapshot["size_vram"] - self.s.placement_tolerance_bytes):
                        return
                await anyio.sleep(2**attempt)
        raise Fault("reload_failed", "Ollama did not restore the original GPU model")

    async def restore(self, job: dict) -> None:
        # Unknown POST acceptance needs a supervisor barrier, not a queue snapshot.
        if job["submission_unknown"]:
            raise Fault("submission_unknown", "Prompt acceptance is uncertain; supervisor recovery required")
        if job["prompt_id"]:
            await self.drain(job["prompt_id"])
            # Recover a saved image after client cancellation or loss of history response.
            record = await self.history(job["prompt_id"])
            if record and record.get("status", {}).get("status_str") == "success":
                try:
                    job["image_paths"] = await self.paths(record)
                except (Fault, OSError):
                    log.warning("Could not recover saved image metadata")
        if job["comfy_touched"]:
            await self.request("comfy", "POST", "/free", json={
                "unload_models": True, "free_memory": True
            })
            await self.wait_memory(job["snapshot"]["size_vram"] + self.s.reload_headroom_bytes, True)
        await self.reload(job["snapshot"])
        job["llm_restored"] = True

    @staticmethod
    def result(job: dict) -> CallToolResult:
        ok = bool(job["image_paths"]) and job["llm_restored"] and not job["error"]
        status = "succeeded" if ok else ("failed" if job["complete"] else "recovery_required")
        payload = {"status": status, "request_id": job["request_id"],
                   "prompt_id": job["prompt_id"], "image_paths": job["image_paths"],
                   "image_path": next(iter(job["image_paths"]), None),
                   "llm_restored": job["llm_restored"], "error": job["error"],
                   "recovery_error": job.get("recovery_error")}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                              structured_content=payload, is_error=not ok)

    async def execute(self, prompt: str, negative: str = "", request_id: str | None = None) -> CallToolResult:
        rid = str(UUID(request_id)) if request_id else str(uuid4())
        workflow = self.workflow(prompt, negative, rid)
        fingerprint = hashlib.sha256(json.dumps({"workflow": workflow, "model": self.s.model,
            "options": self.s.load_options}, sort_keys=True).encode()).hexdigest()
        async with file_lease(self.s.state_root, self.s.lease_seconds):
            rows = await self.journal.rows()
            if any(not r["complete"] for r in rows):
                raise Fault("recovery_required", "An unfinished GPU transaction requires recovery")
            previous = next((r for r in rows if r["request_id"] == rid), None)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise Fault("request_id_conflict", "Request ID was already used with different inputs")
                return self.result(previous)
            if any(await self.queue()):
                raise Fault("comfy_busy", "Dedicated ComfyUI queue must be empty before unloading")
            snapshot = await self.snapshot()
            job = {"request_id": rid, "fingerprint": fingerprint, "snapshot": snapshot,
                   "phase": "prepared", "complete": False, "submission_unknown": False,
                   "comfy_touched": False, "prompt_id": None, "image_paths": [],
                   "llm_restored": False, "error": None, "recovery_error": None}
            await self.journal.save(job)  # No GPU mutation without durable recovery data.
            try:
                job["phase"] = "unloading"
                await self.journal.save(job)
                await self.unload(snapshot)
                job["comfy_touched"] = True
                job["phase"] = "freeing_before_generation"
                await self.journal.save(job)
                await self.request("comfy", "POST", "/free", json={"unload_models": True, "free_memory": True})
                await self.wait_memory(self.s.image_vram_bytes, True)
                job["phase"] = "submitting"
                job["submission_unknown"] = True
                await self.journal.save(job)
                # Never retry this POST automatically. client_id also aids reconciliation.
                reply = await self.request("comfy", "POST", "/prompt", json={
                    "prompt": workflow, "client_id": rid, "extra_data": {"orchestration_id": rid}
                })
                job["prompt_id"] = str(UUID(reply["prompt_id"]))
                job["submission_unknown"] = False
                job["phase"] = "generating"
                await self.journal.save(job)
                record = await self.wait_image(job["prompt_id"])
                job["image_paths"] = await self.paths(record)
                job["phase"] = "image_saved"
                await self.journal.save(job)
            except anyio.get_cancelled_exc_class():
                job["error"] = {"code": "cancelled", "message": "Tool execution cancelled"}
                raise
            except TimeoutError:
                job["error"] = {"code": "timeout", "message": f"Deadline exceeded during {job['phase']}"}
            except httpx.HTTPStatusError as exc:
                # ComfyUI validation rejection conclusively did not enqueue a prompt.
                if job["phase"] == "submitting" and exc.response.status_code == 400:
                    job["submission_unknown"] = False
                job["error"] = {"code": "http_error", "message": f"HTTP {exc.response.status_code} during {job['phase']}"}
            except httpx.TransportError:
                job["error"] = {"code": "transport_error", "message": f"Connection failure during {job['phase']}"}
            except Fault as exc:
                job["error"] = {"code": exc.code, "message": str(exc)}
            except Exception:
                log.exception("Image orchestration failed: %s", rid)
                job["error"] = {"code": "internal_error", "message": "Unexpected orchestration failure"}
            finally:
                # An expired generation/client deadline must not cancel restoration.
                with anyio.CancelScope(shield=True):
                    try:
                        with anyio.fail_after(self.s.cleanup_seconds):
                            job["phase"] = "restoring"
                            await self.restore(job)
                            job["complete"] = True
                    except TimeoutError:
                        job["recovery_error"] = {"code": "restore_timeout", "message": "Recovery deadline exceeded"}
                    except Exception as exc:
                        log.exception("GPU restoration failed: %s", rid)
                        job["recovery_error"] = {"code": getattr(exc, "code", "restore_failed"),
                                                 "message": "GPU restoration requires supervisor recovery"}
                    job["phase"] = "complete" if job["complete"] else "recovery_required"
                    await self.journal.save(job)
            return self.result(job)

    async def recover(self) -> list[dict]:
        """Supervisor/startup entry, never an LLM tool. Does not resubmit images.

        submission_unknown intentionally remains blocked: see PLAN.md for the
        required late-submission barrier and audited journal reconciliation.
        """
        recovered = []
        async with file_lease(self.s.state_root, self.s.lease_seconds):
            for job in await self.journal.rows():
                if job["complete"]:
                    continue
                with anyio.CancelScope(shield=True):
                    with anyio.fail_after(self.s.cleanup_seconds):
                        await self.restore(job)
                        job["complete"] = True
                        job["phase"] = "complete"
                        job["recovery_error"] = None
                        job["error"] = job["error"] or {"code": "process_interrupted", "message": "Recovered an interrupted tool call"}
                        await self.journal.save(job)
                recovered.append(job)
        return recovered


def register(server, service: Orchestrator) -> None:
    @server.tool(name="generate_image", title="Generate a local image", annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ))
    async def generate_image(
        prompt: Annotated[str, Field(min_length=1, max_length=8000)],
        negative_prompt: Annotated[str, Field(max_length=4000)] = "",
        request_id: Annotated[str | None, Field(description="Optional UUID reused only for retries of the same request.")] = None,
    ) -> CallToolResult:
        """Generate an image using the configured local workflow; wait for Ollama restoration.

        Returns execution status, absolute image_path/image_paths, and llm_restored.
        A recovery_required result requires host supervision before further GPU work.
        """
        try:
            return await service.execute(prompt, negative_prompt, request_id)
        except (Fault, ValueError, TimeoutError, httpx.HTTPError, OSError) as exc:
            code = getattr(exc, "code", "preflight_failed")
            payload = {"status": "recovery_required" if code == "recovery_required" else "failed",
                       "request_id": request_id, "prompt_id": None,
                       "image_path": None, "image_paths": [], "llm_restored": None,
                       "error": {"code": code, "message": str(exc)[:300]}, "recovery_error": None}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                                  structured_content=payload, is_error=True)
