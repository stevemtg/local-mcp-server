"""MCP registration for the operator-enabled ``generate_image`` tool."""
from __future__ import annotations

import json
from typing import Annotated

import httpx
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .errors import Fault
from .orchestrator import Orchestrator


def register(server) -> None:
    """Register generate_image; it stays disabled while lifespan has no orchestrator."""

    @server.tool(
        name="generate_image",
        title="Generate a local image",
        description=(
            "Save the list of all running Ollama models and unload them, generate an image with "
            "the configured dedicated ComfyUI workflow, then reload the saved models, including "
            "embedding and completion models. Returns execution status, absolute "
            "image_path/image_paths, and llm_restored (true only after all saved models are restored). "
            "A recovery_required result blocks further "
            "GPU work until host supervision resolves the journal. "
            "After generation, inspect the image and use crop_zoom for closer checks of hands, "
            "faces, anatomy, text, or other small details. Pass the returned image_path as "
            "crop_zoom's image_path and choose named regions as [x,y,width,height] with "
            "units='Relative' (0-1 fractions) or units='Pixels'. crop_zoom returns enlarged "
            "PNG previews and saved paths; visually assess those crops before accepting the image."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                    idempotent_hint=False, open_world_hint=False),
    )
    async def generate_image(
        ctx: Context,
        prompt: Annotated[str, Field(min_length=1, max_length=8000,
                                     description="Positive ComfyUI prompt.")],
        negative_prompt: Annotated[str, Field(max_length=4000,
                                              description="Negative ComfyUI prompt.")] = "",
        request_id: Annotated[str | None, Field(
            description="Optional UUID reused only for retries of the same request.")] = None,
    ) -> CallToolResult:
        try:
            orchestrator: Orchestrator | None = (
                ctx.request_context.lifespan_context.get("orchestrator")
            )
            if orchestrator is None:
                raise Fault("disabled", "Image orchestration is not enabled on this server")
            return await orchestrator.execute(prompt, negative_prompt, request_id)
        except (Fault, ValueError, TimeoutError, httpx.HTTPError, OSError) as exc:
            code = getattr(exc, "code", "preflight_failed")
            payload = {"status": "recovery_required" if code == "recovery_required" else "failed",
                       "request_id": request_id, "prompt_id": None,
                       "image_path": None, "image_paths": [], "llm_restored": None,
                       "error": {"code": code, "message": str(exc)[:300]}, "recovery_error": None}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                                  structured_content=payload, is_error=True)
