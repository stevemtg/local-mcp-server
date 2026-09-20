"""One real MCP image test. Requires the operator to pause other Ollama work."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_orchestration.lease import gpu_session
from image_orchestration.settings import load_settings


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--continue-paused", action="store_true", required=True)
    parser.parse_args()
    config = json.loads((Path.home() / ".cline/data/settings/cline_mcp_settings.json").read_text())
    server = config["mcpServers"]["codebase-semantic-index"]
    os.environ.update(server["env"])
    settings = load_settings()
    assert settings is not None
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "samples": []}
    done = asyncio.Event()
    async with httpx.AsyncClient(timeout=15, trust_env=False) as http:
        async with gpu_session(settings):
            q = (await http.get(settings.comfy_url + "/queue")).raise_for_status().json()
            if q["queue_running"] or q["queue_pending"]:
                raise RuntimeError("ComfyUI is busy; test will not interrupt it")
            before = (await http.get(settings.ollama_url + "/api/ps")).raise_for_status().json()
            report["models_before"] = before["models"]
            allowed = {"nomic-embed-text:latest", "qwen2.5-coder:1.5b-base"}
            foreign = [m["name"] for m in before["models"]
                       if m["name"] != settings.model and m.get("size_vram", 0) > 0 and m["name"] not in allowed]
            if foreign:
                raise RuntimeError(f"Unexpected GPU models; refusing to unload them: {foreign}")
            for model in before["models"]:
                if model["name"] in allowed:
                    r = await http.post(settings.ollama_url + "/api/generate", json={
                        "model": model["name"], "prompt": "", "stream": False, "keep_alive": 0
                    }, timeout=90)
                    r.raise_for_status()
                    print("Unloaded paused helper:", model["name"], flush=True)

        async def monitor():
            while not done.is_set():
                try:
                    ps = (await http.get(settings.ollama_url + "/api/ps")).raise_for_status().json()
                    p = await asyncio.create_subprocess_exec(
                        settings.nvidia_smi_path or "nvidia-smi",
                        "--query-gpu=memory.used,memory.free", "--format=csv,noheader,nounits",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    stdout, _ = await p.communicate()
                    sample = {"at": datetime.now(timezone.utc).isoformat(),
                              "models": [m["name"] for m in ps["models"]],
                              "gpu_used_free_mib": stdout.decode().strip()}
                    report["samples"].append(sample)
                    print("GPU:", json.dumps(sample), flush=True)
                except Exception as exc:
                    print("Monitor:", type(exc).__name__, flush=True)
                try:
                    await asyncio.wait_for(done.wait(), timeout=10)
                except TimeoutError:
                    pass

        watcher = asyncio.create_task(monitor())
        started = time.monotonic()
        try:
            params = StdioServerParameters(command=server["command"], args=["-B", *server["args"]],
                                           env={**os.environ, **server["env"]})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.call_tool("generate_image", {
                        "prompt": "A small red robot reading a book beside a potted plant, clean detailed illustration, soft daylight, cheerful colors",
                        "negative_prompt": "blurry, distorted, watermark, text",
                        "request_id": str(uuid4())
                    }, read_timeout_seconds=1200)
                    report["result"] = result.model_dump(by_alias=True)
                    print("RESULT:", json.dumps(result.structured_content), flush=True)
        finally:
            done.set()
            await watcher
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            report["models_after"] = (await http.get(settings.ollama_url + "/api/ps")).raise_for_status().json()["models"]
            destination = settings.state_root / "hardware-test-report.json"
            destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print("Report:", destination, flush=True)
    if report.get("result", {}).get("isError", True):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
