"""The generate_image transaction: unload -> generate -> verify -> restore.

Only journal-prepared flows mutate GPU state. Recovery restores all resident models
when ownership is known; uncertain transactions keep GPU admission closed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import subprocess
from typing import Any
from uuid import UUID, uuid4

import anyio
import httpx
from mcp.types import CallToolResult, TextContent

from .errors import Fault
from .journal import Journal
from .lease import file_lease
from .settings import Settings

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s, self.http = settings, http
        self.journal = Journal(settings.state_root)

    def recovery_target(self) -> dict:
        """Bind persisted work to the services and output mapping it modified."""
        return {"ollama_url": self.s.ollama_url.rstrip("/"),
                "comfy_url": self.s.comfy_url.rstrip("/"),
                "gpu_index": self.s.gpu_index,
                "output_root": str(self.s.output_root.resolve()),
                "save_node": self.s.save_node}

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

    async def snapshot(self) -> list[dict]:
        """Record every running Ollama model before changing any residency."""
        snapshots = []
        for original in await self.models():
            name = original["name"]
            if not original.get("digest"):
                raise Fault("missing_digest", f"Runner did not report the digest for {name}")
            context = original.get("context_length")
            options = {}
            if name == self.s.model:
                if context != self.s.load_options["num_ctx"]:
                    raise Fault("context_mismatch", "Host num_ctx differs from resident model")
                options = copy.deepcopy(self.s.load_options)
            elif context is not None and context > 0:
                options["num_ctx"] = context
            size_vram = original.get("size_vram", 0)
            if size_vram == 0:
                options["num_gpu"] = 0
            info = await self.request("ollama", "POST", "/api/show", json={"model": name})
            capabilities = info.get("capabilities", [])
            endpoint = ("/api/embed" if "embedding" in capabilities
                        and "completion" not in capabilities else "/api/generate")
            snapshots.append({"name": name, "digest": original["digest"],
                              "size_vram": size_vram, "context_length": context,
                              "options": options, "keep_alive": self.s.keep_alive,
                              "load_endpoint": endpoint})
        return snapshots

    @staticmethod
    def job_snapshots(job: dict) -> list[dict]:
        # Preserve recovery of journals written before multi-model support.
        return job["snapshots"] if "snapshots" in job else [job["snapshot"]]

    def workflow(self, prompt: str, negative: str, request_id: str) -> dict:
        if not prompt.strip() or len(prompt) > 8000 or len(negative) > 4000:
            raise Fault("invalid_input", "Prompt is blank or exceeds the configured limit")
        w = copy.deepcopy(self.s.workflow)
        try:
            values = (prompt, negative, UUID(request_id).int % (2**63), f"mcp/{request_id}")
            for (node, input_name), value in zip(self.s.workflow_bindings(), values, strict=True):
                w[node]["inputs"][input_name] = value
        except (KeyError, TypeError, ValueError) as exc:
            raise Fault("invalid_workflow", "Trusted workflow node mapping is invalid") from exc
        return w

    async def unload(self, snapshot: dict, job: dict) -> None:
        job["unload_unknown"] = True
        await self.journal.save(job)
        with anyio.fail_after(self.s.unload_seconds):
            await self.request("ollama", "POST", "/api/generate", json={
                "model": snapshot["name"], "prompt": "", "stream": False, "keep_alive": 0
            }, timeout=httpx.Timeout(self.s.unload_seconds, connect=5))
            # A failed residency poll is different from an unacknowledged POST.
            job["unload_unknown"] = False
            await self.journal.save(job)
            while any(m["name"] == snapshot["name"] for m in await self.models()):
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
                if self.s.nvidia_smi_path:
                    # WDDM/PyTorch may report virtual/reusable memory rather than
                    # device-wide free VRAM. Use the lower driver measurement.
                    probe = await anyio.run_process([
                        self.s.nvidia_smi_path, f"--id={self.s.gpu_index}",
                        "--query-gpu=memory.free", "--format=csv,noheader,nounits"
                    ], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    try:
                        driver_free = int(probe.stdout.decode().strip()) * 1024**2
                        if driver_free < 0:
                            raise ValueError("Negative driver VRAM measurement")
                    except ValueError as exc:
                        raise Fault("gpu_probe_failed", "NVIDIA driver did not report usable free VRAM") from exc
                    physical_free = min(physical_free, driver_free)
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

    def restored(self, snapshot: dict, models: list[dict]) -> bool:
        for model in models:
            if model["name"] != snapshot["name"] or model["digest"] != snapshot["digest"]:
                continue
            if (snapshot.get("context_length") is not None
                    and model.get("context_length") != snapshot["context_length"]):
                continue
            vram = model.get("size_vram", 0)
            if snapshot["size_vram"] == 0:
                return vram == 0
            return vram > 0 and vram >= snapshot["size_vram"] - self.s.placement_tolerance_bytes
        return False

    async def reload(self, snapshot: dict) -> None:
        with anyio.fail_after(self.s.reload_seconds):
            tags = (await self.request("ollama", "GET", "/api/tags"))["models"]
            if not any(m["name"] == snapshot["name"] and m["digest"] == snapshot["digest"]
                       for m in tags):
                raise Fault("model_changed", "Original local model digest is no longer available")
            for attempt in range(3):
                try:
                    endpoint = snapshot.get("load_endpoint", "/api/generate")
                    payload = {
                        "model": snapshot["name"],
                        "keep_alive": -1 if snapshot["keep_alive"] == "-1" else snapshot["keep_alive"],
                        "options": snapshot["options"]
                    }
                    payload.update({"input": ""} if endpoint == "/api/embed"
                                   else {"prompt": "", "stream": False})
                    await self.request("ollama", "POST", endpoint, json=payload,
                                       timeout=httpx.Timeout(self.s.reload_seconds, connect=5))
                except (httpx.TransportError, httpx.HTTPStatusError):
                    # A lost preload response may still have loaded the model.
                    log.warning("Ollama preload attempt %d failed", attempt + 1)
                try:
                    resident = [m for m in await self.models() if m["name"] == snapshot["name"]]
                except (httpx.TransportError, httpx.HTTPStatusError):
                    resident = []
                if self.restored(snapshot, resident):
                    return
                await anyio.sleep(2**attempt)
        raise Fault("reload_failed", f"Ollama did not restore {snapshot['name']}")

    async def restore(self, job: dict) -> None:
        snapshots = self.job_snapshots(job)
        if job.get("recovery_target") != self.recovery_target():
            raise Fault("recovery_target_changed", "Recovery requires the original backend and output configuration")
        if job.get("unload_unknown", True):
            raise Fault("unload_unknown", "Unload acceptance is uncertain; supervisor recovery required")
        # Unknown POST acceptance needs a supervisor barrier, not a queue snapshot.
        if job["submission_unknown"]:
            raise Fault("submission_unknown", "Prompt acceptance is uncertain; supervisor recovery required")
        if job["prompt_id"]:
            await self.drain(job["prompt_id"])
            # Recover a saved image after client cancellation or loss of history response.
            # Artifact discovery is best effort and must not consume the GPU
            # cleanup budget or prevent release/reload once the queue is drained.
            if not job["image_paths"]:
                try:
                    with anyio.fail_after(min(5, self.s.cleanup_seconds / 10)):
                        record = await self.history(job["prompt_id"])
                        if record and record.get("status", {}).get("status_str") == "success":
                            job["image_paths"] = await self.paths(record)
                except Exception:
                    log.warning("Could not recover saved image metadata", exc_info=True)
        if job["comfy_touched"]:
            await self.request("comfy", "POST", "/free", json={
                "unload_models": True, "free_memory": True
            })
            # Previously restored models may still be resident during recovery.
            resident = await self.models()
            needed = sum(s["size_vram"] for s in snapshots if not self.restored(s, resident))
            await self.wait_memory(needed + self.s.reload_headroom_bytes, True)
        failures = []
        for snapshot in snapshots:
            try:
                await self.reload(snapshot)
            except Exception as exc:
                # Still attempt the remaining models if one cannot be restored.
                log.exception("Could not restore %s", snapshot["name"])
                failures.append(exc)
        if failures:
            raise failures[0]
        resident = await self.models()
        missing = [s["name"] for s in snapshots if not self.restored(s, resident)]
        if missing:
            raise Fault("reload_failed", f"Models not resident after restoration: {', '.join(missing)}")
        job["llm_restored"] = True

    @staticmethod
    def result(job: dict) -> CallToolResult:
        ok = (job["complete"] and bool(job["image_paths"]) and job["llm_restored"]
              and not job["error"] and not job.get("recovery_error"))
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
            snapshots = await self.snapshot()
            job = {"request_id": rid, "fingerprint": fingerprint, "snapshots": snapshots,
                   "recovery_target": self.recovery_target(), "unload_unknown": False,
                   "phase": "prepared", "complete": False, "submission_unknown": False,
                   "comfy_touched": False, "prompt_id": None, "image_paths": [],
                   "llm_restored": False, "error": None, "recovery_error": None}
            await self.journal.save(job)  # No GPU mutation without durable recovery data.
            try:
                job["phase"] = "unloading"
                for snapshot in snapshots:
                    await self.unload(snapshot, job)
                job["comfy_touched"] = True
                job["phase"] = "freeing_before_generation"
                await self.journal.save(job)
                await self.request("comfy", "POST", "/free", json={"unload_models": True, "free_memory": True})
                await self.wait_memory(self.s.image_vram_bytes, True)
                if await self.models():
                    raise Fault("models_reloaded", "Ollama models became resident before image submission")
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
                        if not job["error"] and not job["image_paths"]:
                            job["error"] = {"code": "process_interrupted", "message": "Recovered an interrupted tool call"}
                        await self.journal.save(job)
                recovered.append(job)
        return recovered




