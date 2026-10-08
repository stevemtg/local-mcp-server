# Image generation and GPU coordination

[Back to the README](../README.md) · [Workflow setup](../workflows/README.md)

Set `IMAGE_MODEL` to enable `generate_image`. Leave it unset to keep the code
tools and the `crop_zoom`, `check_transparency`, and `remove_bg` image tools
available and disable GPU admission gating. The feature uses a dedicated
local ComfyUI server and automatically manages the running Ollama models. Required settings:

| Variable | Meaning |
| --- | --- |
| `IMAGE_MODEL` | Exact canonical **chat** model name from the agent host, not `EMBED_MODEL`. |
| `IMAGE_LOAD_OPTIONS` | JSON object with the host's load options, including a positive integer `num_ctx`. |
| `IMAGE_WORKFLOW_PATH` | Trusted ComfyUI API-format JSON graph. The default mappings are text nodes 6/7, KSampler 3, SaveImage 9. |
| `IMAGE_OUTPUT_ROOT` | Existing absolute ComfyUI output directory shared with this server. |
| `IMAGE_STATE_ROOT` | Absolute local state directory shared by every cooperating GPU client. |
| `IMAGE_VRAM_BUDGET_BYTES` | Measured positive image-generation VRAM budget, including headroom. |

`IMAGE_OLLAMA_URL` and `IMAGE_COMFY_URL` default to loopback ports 11434 and
8188. `IMAGE_NVIDIA_SMI_PATH` optionally supplies an absolute NVIDIA utility
path for a conservative physical VRAM check on Windows/WDDM. Additional
budgets, deadlines, and node mappings are defined in
[settings.py](../image_orchestration/settings.py). Configure an overall MCP tool
deadline that includes generation **and** restoration; see the
[architecture plan](comfyui_orchestration/PLAN.md).

The server gates its three embedding/indexing tools, but cannot control an
external agent host's Ollama calls. Every GPU client must acquire the same
`gpu_session(settings)` lease and check its recovery journal. The host must
release its inference lease before dispatching an MCP tool.
Do not submit unrelated jobs to the dedicated ComfyUI instance.

Before generation, the tool queries Ollama's running-model list and saves every
resident model in the durable journal, including embedding, completion, and
CPU-only models. It unloads each model and confirms Ollama is empty before
submitting the image. Afterward it releases ComfyUI's
models and reloads the saved list, using the embedding API for embedding models.
An empty list is supported; the configured chat model need not already be loaded.
`llm_restored` means **all** saved models passed the final residency check.

The configured chat model retains `IMAGE_LOAD_OPTIONS`; other models retain
their reported context length and CPU-only placement, with other options using
Ollama's model defaults. Ollama does not report all original load options.
Restored models use `IMAGE_KEEP_ALIVE` (default `10m`). GPU placement is verified
against each model's original VRAM usage. Allow enough `IMAGE_CLEANUP_SECONDS`
for the entire list to reload. Cleanup also runs after generation errors or cancellation.

Recovery is attempted on startup. A lost unload or submission acknowledgement
keeps the journal unfinished, blocking GPU admission until a supervisor proves
that no late backend operation can execute. Recovery also verifies the original
backend URLs, GPU index, output root, and SaveImage mapping. Older unfinished
journals without this metadata require audited reconciliation; they are not
automatically trusted or deleted. Known-job cleanup treats history/output
discovery as best effort so that it cannot prevent releasing VRAM and reloading
the saved models. Existing journals containing a single model remain recoverable.
