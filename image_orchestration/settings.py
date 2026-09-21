"""Operator-owned image orchestration settings, validated at server startup.

The feature is disabled while ``IMAGE_MODEL`` is unset (``load_settings``
returns ``None``). Once enabled, a malformed configuration fails fast at
startup rather than mid-transaction.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class Settings:
    model: str                          # Exact canonical model name, from host.
    load_options: dict[str, Any]        # Same load-affecting options as host.
    workflow: dict[str, Any]            # Trusted ComfyUI API-format graph.
    output_root: Path                   # Actual ComfyUI --output-directory.
    state_root: Path                    # Shared by every GPU client/process.
    image_vram_bytes: int               # Measured budget including headroom.
    ollama_url: str = "http://127.0.0.1:11434"
    comfy_url: str = "http://127.0.0.1:8188"
    positive_node: str = "6"
    negative_node: str = "7"
    sampler_node: str = "3"
    save_node: str = "9"
    gpu_index: int = 0
    nvidia_smi_path: str | None = None   # Optional physical VRAM probe for Windows/WDDM.
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
        if (not isinstance(self.model, str) or not self.model.strip()
                or not isinstance(self.load_options, dict)
                or type(self.load_options.get("num_ctx")) is not int
                or self.load_options["num_ctx"] <= 0):
            raise ValueError("Set the host's canonical model and explicit num_ctx")
        if type(self.image_vram_bytes) is not int or self.image_vram_bytes <= 0:
            raise ValueError("Set a measured image VRAM budget")
        for name in ("lease_seconds", "unload_seconds", "generation_seconds",
                     "cleanup_seconds", "drain_seconds", "free_seconds",
                     "reload_seconds", "poll_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("gpu_index", "idle_torch_bytes", "reload_headroom_bytes", "placement_tolerance_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not isinstance(self.keep_alive, str) or not (
            self.keep_alive == "-1" or
            (re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ns|us|ms|s|m|h))+", self.keep_alive)
             and any(float(n) > 0 for n in re.findall(r"\d+(?:\.\d+)?", self.keep_alive)))
        ):
            raise ValueError("keep_alive must be a positive duration or -1 (never unload automatically)")
        if not self.output_root.is_absolute() or not self.state_root.is_absolute():
            raise ValueError("Output and shared state roots must be absolute")
        for value in (self.ollama_url, self.comfy_url):
            u = httpx.URL(value)
            if u.scheme != "http" or u.host not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("This local-only feature requires loopback HTTP")
            if u.userinfo or u.query or u.fragment or u.path not in ("", "/"):
                raise ValueError("Backend URLs must be plain loopback origins without credentials")
        self.workflow_bindings()
        if not self.output_root.is_dir():
            raise ValueError("Output root must be an existing ComfyUI output directory")
        if self.nvidia_smi_path is not None:
            probe = Path(self.nvidia_smi_path)
            if not probe.is_absolute() or not probe.is_file():
                raise ValueError("IMAGE_NVIDIA_SMI_PATH must name an existing absolute executable")

    def workflow_bindings(self) -> tuple[tuple[str, str], ...]:
        """Validate and resolve positive, negative, seed and filename inputs.

        Qwen Image 2.1 encodes both prompts in one node; sharing that node is
        safe because the two bindings address different input fields.
        """
        roles = (
            (self.positive_node, {"CLIPTextEncode": "text", "TextEncodeQwenImage21": "prompt"}),
            (self.negative_node, {"CLIPTextEncode": "text", "TextEncodeQwenImage21": "negative_prompt"}),
            (self.sampler_node, {"KSampler": "seed"}),
            (self.save_node, {"SaveImage": "filename_prefix"}),
        )
        bindings = []
        try:
            for node, supported in roles:
                entry = self.workflow[node]
                input_name = supported[entry["class_type"]]
                if not isinstance(entry["inputs"], dict) or input_name not in entry["inputs"]:
                    raise ValueError("Workflow node mapping does not match the API graph")
                bindings.append((node, input_name))
        except (KeyError, TypeError) as exc:
            raise ValueError("Workflow node mapping does not match the API graph") from exc
        if len(set(bindings)) != len(bindings):
            raise ValueError("Workflow input mappings must be distinct")
        return tuple(bindings)


def _env_float(name: str, default: str) -> float:
    return float(os.environ.get(name, default))


def load_settings() -> Settings | None:
    """Build validated Settings from the operator environment; None disables."""
    model = os.environ.get("IMAGE_MODEL", "").strip()
    if not model:
        return None  # Feature disabled: generate_image stays unadvertised.
    try:
        load_options = json.loads(os.environ.get("IMAGE_LOAD_OPTIONS", "{}"))
        workflow = json.loads(
            Path(os.environ["IMAGE_WORKFLOW_PATH"]).read_text(encoding="utf-8"))
    except (KeyError, OSError, json.JSONDecodeError) as exc:
        raise ValueError("IMAGE_WORKFLOW_PATH/IMAGE_LOAD_OPTIONS are invalid") from exc
    if not isinstance(load_options, dict) or not isinstance(workflow, dict):
        raise ValueError("Image load options and workflow must be JSON objects")
    return Settings(
        model=model,
        load_options=load_options,
        workflow=workflow,
        output_root=Path(os.environ["IMAGE_OUTPUT_ROOT"]),
        state_root=Path(os.environ["IMAGE_STATE_ROOT"]),
        image_vram_bytes=int(os.environ["IMAGE_VRAM_BUDGET_BYTES"]),
        ollama_url=os.environ.get("IMAGE_OLLAMA_URL", "http://127.0.0.1:11434"),
        comfy_url=os.environ.get("IMAGE_COMFY_URL", "http://127.0.0.1:8188"),
        positive_node=os.environ.get("IMAGE_POSITIVE_NODE", "6"),
        negative_node=os.environ.get("IMAGE_NEGATIVE_NODE", "7"),
        sampler_node=os.environ.get("IMAGE_SAMPLER_NODE", "3"),
        save_node=os.environ.get("IMAGE_SAVE_NODE", "9"),
        gpu_index=int(os.environ.get("IMAGE_GPU_INDEX", "0")),
        nvidia_smi_path=os.environ.get("IMAGE_NVIDIA_SMI_PATH") or None,
        idle_torch_bytes=int(os.environ.get("IMAGE_IDLE_TORCH_BYTES", str(256 * 1024**2))),
        reload_headroom_bytes=int(os.environ.get("IMAGE_RELOAD_HEADROOM_BYTES", str(512 * 1024**2))),
        placement_tolerance_bytes=int(os.environ.get("IMAGE_PLACEMENT_TOLERANCE_BYTES", str(64 * 1024**2))),
        keep_alive=os.environ.get("IMAGE_KEEP_ALIVE", "10m"),
        lease_seconds=_env_float("IMAGE_LEASE_SECONDS", "10"),
        unload_seconds=_env_float("IMAGE_UNLOAD_SECONDS", "90"),
        generation_seconds=_env_float("IMAGE_GENERATION_SECONDS", "600"),
        cleanup_seconds=_env_float("IMAGE_CLEANUP_SECONDS", "300"),
        drain_seconds=_env_float("IMAGE_DRAIN_SECONDS", "30"),
        free_seconds=_env_float("IMAGE_FREE_SECONDS", "30"),
        reload_seconds=_env_float("IMAGE_RELOAD_SECONDS", "180"),
        poll_seconds=_env_float("IMAGE_POLL_SECONDS", "1"),
    )
