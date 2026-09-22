"""CPU-only alpha-inspection MCP adapter for the reusable Windows PowerShell script."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import anyio
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check-transparency.ps1"


async def inspect_transparency(image_path: str) -> dict:
    """Pass data through a JSON file; never interpolate caller text into code."""
    try:
        source = Path(image_path)
        if not source.is_absolute():
            raise ValueError("image_path must be an absolute local image path")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("image_path must name an existing image file")
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            raise OSError("check_transparency requires Windows PowerShell and System.Drawing on the server host")
        request = {"image_path": str(source)}
        with tempfile.TemporaryDirectory(prefix="check-transparency-request-") as temporary:
            request_path = Path(temporary) / "request.json"
            request_path.write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
            # The override applies only to this noninteractive child process;
            # no execution policy is changed in the caller or on the machine.
            with anyio.fail_after(60):
                process = await anyio.run_process(
                    [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-File", str(SCRIPT), "-RequestPath", str(request_path)],
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            if process.returncode:
                message = process.stderr.decode("utf-8", errors="replace").strip()
                raise ValueError(message[:1500] or "Transparency check failed without an error message")
            payload = json.loads(process.stdout.decode("utf-8-sig"))
        payload["note"] = (
            "Measured from every pixel of the live file: alpha counts, corner pixels, and the "
            "bounding box of pixels with A>0. This tool only reports measurements; it does not "
            "repair or alter the image. A painted checkerboard or solid background is only "
            "transparent if these counts show alpha 0 outside the sprite."
        )
        return payload
    except (OSError, ValueError, TimeoutError) as exc:
        return {"status": "failed", "error": {
            "code": "transparency_check_failed",
            "message": "Transparency check exceeded 60 seconds" if isinstance(exc, TimeoutError) else str(exc),
        }}


def register(server) -> None:
    """Advertise transparency inspection independently of the GPU generation tool."""

    @server.tool(
        name="check_transparency", title="Check image alpha transparency",
        description=(
            "Report whether a local image is actually transparent by inspecting its alpha channel "
            "pixel by pixel. Use after generate_image: pass the same absolute image_path here. "
            "Works with images from any project on this server host; no index, ComfyUI, Ollama, "
            "or GPU is required. Read-only: it never writes files or alters the image. Returns "
            "status, source dimensions and pixel format, whether the format or palette carries alpha, the "
            "four corner pixel colors (A/R/G/B), counts of fully transparent (A=0), partially "
            "transparent (0<A<255), and fully opaque (A=255) pixels with fractions from 0 to 1, and the "
            "bounding box of all A>0 pixels (content_bounds, null when fully transparent). "
            "Counts and corners alone cannot prove that the intended background is transparent; "
            "inspect the image too. Painted checkerboards do not imply transparency. "
            "Example: {\"image_path\":\"C:/art/sprite.png\"}"
        ),
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                    idempotent_hint=True, open_world_hint=False),
    )
    async def check_transparency(
        image_path: Annotated[str, Field(
            min_length=1,
            description="Absolute local image path; use generate_image's image_path.")],
    ) -> CallToolResult:
        payload = await inspect_transparency(image_path)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload, is_error=payload.get("status") == "failed",
        )
