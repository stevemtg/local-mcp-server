"""CPU-only crop/zoom MCP adapter for the reusable Windows PowerShell script."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import anyio
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "crop-zoom.ps1"
MAX_INLINE_BYTES = 8 * 1024 * 1024
Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Region = tuple[Coordinate, Coordinate, Coordinate, Coordinate]


async def crop_image(
    image_path: str,
    regions: dict[str, Region] | None,
    units: str,
    scale: float,
    output_dir: str | None,
    interpolation: str,
    include_images: bool,
) -> CallToolResult:
    """Pass data through a JSON file; never interpolate caller text into code."""
    try:
        source = Path(image_path)
        if not source.is_absolute():
            raise ValueError("image_path must be an absolute local image path")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("image_path must name an existing image file")
        base = Path(output_dir) if output_dir is not None else source.parent / "crops"
        if not base.is_absolute():
            raise ValueError("output_dir must be an absolute local directory")
        # Separate concurrent calls and repeated inspections, including calls
        # from different projects. Never overwrite an earlier inspection.
        destination = base.resolve() / ("crop-" + uuid4().hex)
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            raise OSError("crop_zoom requires Windows PowerShell and System.Drawing on the server host")
        request = {
            "image_path": str(source), "output_dir": str(destination),
            "regions": regions, "units": units, "scale": scale,
            "interpolation": interpolation,
        }
        with tempfile.TemporaryDirectory(prefix="crop-zoom-request-") as temporary:
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
                raise ValueError(message[:1500] or "Crop process failed without an error message")
            payload = json.loads(process.stdout.decode("utf-8-sig"))

        content = []
        inline_bytes = 0
        for crop in payload["images"]:
            crop["preview_included"] = False
            if include_images:
                png = await anyio.Path(crop["path"]).read_bytes()
                if inline_bytes + len(png) <= MAX_INLINE_BYTES:
                    content.extend([
                        TextContent(type="text", text=f"Crop: {crop['name']} ({crop['width']}x{crop['height']})"),
                        ImageContent(type="image", mime_type="image/png", data=base64.b64encode(png).decode("ascii")),
                    ])
                    inline_bytes += len(png)
                    crop["preview_included"] = True
        payload["note"] = (
            "Inspect the returned crops visually for hands, faces, text, or other details. "
            "This tool does not automatically judge image quality. PNG paths are always returned; "
            "inline previews are limited to 8 MiB in total."
        )
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload)), *content],
            structured_content=payload, is_error=False,
        )
    except (OSError, ValueError, TimeoutError) as exc:
        payload = {"status": "failed", "error": {
            "code": "crop_failed",
            "message": "Crop processing exceeded 60 seconds" if isinstance(exc, TimeoutError) else str(exc),
        }}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                              structured_content=payload, is_error=True)


def register(server) -> None:
    """Advertise inspection independently of the optional GPU generation tool."""

    @server.tool(
        name="crop_zoom", title="Crop and zoom image details",
        description=(
            "Create enlarged PNG crops of an existing local image for visual inspection, including "
            "hands, faces, anatomy, text, or artifacts. Use after generate_image: pass its absolute "
            "image_path as image_path here. Works with images from any project on this server host; "
            "no index, ComfyUI, Ollama, or GPU is required. Requires Windows PowerShell/System.Drawing. "
            "regions maps labels to [x,y,width,height], measured from the top-left. units='Relative' "
            "uses 0-1 fractions; units='Pixels' uses integer pixels. Omit regions to zoom the whole "
            "image. Example: {\"image_path\":\"C:/art/image.png\",\"regions\":{\"detail\":[0.2,0.3,0.3,0.4]},\"scale\":3}. "
            "Returns PNG image content for you to inspect and structured source/crop bounds, sizes, "
            "and absolute paths. It does not automatically detect malformed anatomy. Writes new files "
            "in a unique subfolder of output_dir (default: crops beside the source). "
            "At most 8 regions and 16 million output pixels per call; out-of-bounds crops are rejected. "
            "Inline previews have an 8 MiB total limit; use smaller regions/scale if a preview is omitted."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                    idempotent_hint=False, open_world_hint=False),
    )
    async def crop_zoom(
        image_path: Annotated[str, Field(min_length=1, description="Absolute local image path; use generate_image's image_path.")],
        regions: Annotated[dict[str, Region] | None, Field(
            min_length=1, max_length=8,
            description="Named crops, each [x, y, width, height]. Omit for the entire image; labels become filename components.")
        ] = None,
        units: Annotated[Literal["Relative", "Pixels"], Field(
            description="Relative uses fractions of source dimensions; Pixels requires integers. Coordinates start at the top-left.")
        ] = "Relative",
        scale: Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True,
                                     description="Positive zoom factor; 3 enlarges each crop threefold, 1 saves an unscaled crop.")] = 3,
        output_dir: Annotated[str | None, Field(min_length=1,
            description="Absolute parent directory for a new crop batch folder; defaults to crops beside the source.")] = None,
        interpolation: Annotated[Literal["NearestNeighbor", "HighQualityBilinear", "HighQualityBicubic"], Field(
            description="NearestNeighbor shows exact pixel boundaries; the high-quality modes produce smoother previews.")
        ] = "NearestNeighbor",
        include_images: Annotated[bool, Field(description="Return inline PNG previews as well as saved paths; disable for metadata only.")] = True,
    ) -> CallToolResult:
        return await crop_image(image_path, regions, units, scale, output_dir, interpolation, include_images)
