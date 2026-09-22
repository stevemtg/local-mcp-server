"""CPU-only background-removal MCP adapter for the reusable Windows PowerShell script."""
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


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "remove-bg.ps1"


async def remove_background(image_path: str, out: str | None, threshold: int, edge_width: int) -> dict:
    """Pass data through a JSON file; never interpolate caller text into code."""
    try:
        source = Path(image_path)
        if not source.is_absolute():
            raise ValueError("image_path must be an absolute local image path")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("image_path must name an existing image file")
        request: dict = {"image_path": str(source), "threshold": threshold, "edge_width": edge_width}
        if out is not None:
            destination = Path(out)
            if not destination.is_absolute():
                raise ValueError("out must be an absolute local path for the new PNG")
            request["out"] = str(destination)
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            raise OSError("remove_bg requires Windows PowerShell and System.Drawing on the server host")
        with tempfile.TemporaryDirectory(prefix="remove-bg-request-") as temporary:
            request_path = Path(temporary) / "request.json"
            request_path.write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
            # The override applies only to this noninteractive child process;
            # no execution policy is changed in the caller or on the machine.
            with anyio.fail_after(120):
                process = await anyio.run_process(
                    [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-File", str(SCRIPT), "-RequestPath", str(request_path)],
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            if process.returncode:
                message = process.stderr.decode("utf-8", errors="replace").strip()
                raise ValueError(message[:1500] or "Background removal failed without an error message")
            payload = json.loads(process.stdout.decode("utf-8-sig"))
        payload["note"] = (
            "The near-white background connected to the image border was flood-filled and set to "
            "alpha 0 in a NEW PNG at output.path; the source image was never modified. Enclosed "
            "white sprite details are preserved and cannot be distinguished from background holes. "
            "Verify the result with check_transparency on output.path."
        )
        return payload
    except (OSError, ValueError, TimeoutError) as exc:
        return {"status": "failed", "error": {
            "code": "background_removal_failed",
            "message": "Background removal exceeded 120 seconds" if isinstance(exc, TimeoutError) else str(exc),
        }}


def register(server) -> None:
    """Advertise background removal independently of the GPU generation tool."""

    @server.tool(
        name="remove_bg", title="Remove white image background",
        description=(
            "Cut a flat white/near-white background out of a local sprite into a NEW transparent PNG "
            "and clean white matte from its outline. Use for a white/near-white background confirmed "
            "by visual inspection. Flood-fills background connected to the image border (including "
            "diagonal gaps) and sets it fully transparent; estimates antialias coverage in a narrow "
            "edge band to un-matte the white fringe from RGB and alpha, preserving enclosed white "
            "sprite details and translucent artwork. Never modifies the source: writes the source "
            "stem plus _transparent.png beside it unless out is given. Existing output paths are "
            "rejected; choose another out path for a repeat call. Edge cleanup is a heuristic; "
            "inspect the result for changes to light foreground edges. Works for images from any "
            "project on this server host; no index, ComfyUI, Ollama, or GPU is required. Returns "
            "source/output metadata with alpha counts and content_bounds; verify the result with "
            "check_transparency on output.path. Example: {\"image_path\":\"C:/art/sprite.png\"}"
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                    idempotent_hint=True, open_world_hint=False),
    )
    async def remove_bg(
        image_path: Annotated[str, Field(
            min_length=1,
            description="Absolute local image path; use generate_image's image_path.")],
        out: Annotated[str | None, Field(
            min_length=1,
            description="Absolute path for a new .png file; existing paths are rejected. Defaults to "
                        "<source-stem>_transparent.png beside the source.")] = None,
        threshold: Annotated[int, Field(
            ge=0, le=255,
            description="Minimum R, G, and B channel values considered background (default 225).")] = 225,
        edge_width: Annotated[int, Field(
            ge=0, le=16,
            description="Width in pixels of the antialiased edge band to un-matte (default 2); "
                        "0 cuts hard with no edge color correction.")] = 2,
    ) -> CallToolResult:
        payload = await remove_background(image_path, out, threshold, edge_width)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload, is_error=payload.get("status") == "failed",
        )
