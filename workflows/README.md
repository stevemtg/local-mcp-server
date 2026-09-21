# Local ComfyUI image setup

`image.json` is the ComfyUI API-format Qwen-Image-2.1 workflow, converted from
`E:\Comfy-Desktop\image_qwen_image_2_1_t2i.json`. It uses the installed files:

- `UNETLoader`: `models\diffusion_models\qwen_image_2.1_int8_convrot.safetensors`
- `CLIPLoader` (type `qwen_image`): `models\text_encoders\qwen3vl_8b_int8_convrot.safetensors`
- `VAELoader`: `models\vae\qwen_image_2.1_vae_bf16.safetensors`

These paths are under `E:\Comfy-Desktop\ComfyUI-Shared`. This model uses separate
diffusion, text encoder, and VAE loaders rather than a single checkpoint.
The graph keeps the sample's one 1024 x 1024 image, 25 steps, CFG 1, Euler
sampler, simple scheduler, and denoise 1. The UI subgraph is flattened, the
resolution selector becomes fixed dimensions, and `SaveImage` writes PNGs to
`E:\Comfy-Desktop\ComfyUI-Shared\output\mcp`. Sample prompts are replaced by
the MCP tool's prompt and negative prompt on every call.

`TextEncodeQwenImage21` produces positive and negative conditioning from one
node. Both mappings must select node `"6"`; the adapter writes `prompt` and
`negative_prompt` separately. The sampler remains `"3"` and output remains `"9"`.
These values belong in the MCP server's `env`:

```json
{
  "IMAGE_WORKFLOW_PATH": "E:/local-mcp-semantic-index/workflows/image.json",
  "IMAGE_POSITIVE_NODE": "6",
  "IMAGE_NEGATIVE_NODE": "6",
  "IMAGE_SAMPLER_NODE": "3",
  "IMAGE_SAVE_NODE": "9"
}
```

`IMAGE_MODEL` still names the Ollama chat model to restore, not Qwen-Image-2.1.
At CFG 1, the sampler does not apply negative conditioning; changing the
negative prompt alone will not provide classifier-free guidance.

The Krea 2 graph is preserved as `image-krea2-turbo.json` and the SDXL
`AnythingXL_xl.safetensors` graph as `image-simple.json`. To switch to either,
change `IMAGE_WORKFLOW_PATH` and set `IMAGE_NEGATIVE_NODE` back to `"7"`.
Reconnect the MCP server after changing its workflow or settings because
the graph is loaded at startup.

Validate all three workflows without generating or loading models:

```powershell
python -B dev\validate_workflows.py --comfy-url http://127.0.0.1:8188
```

Qwen's nodes, model names, and links were checked against the local ComfyUI
0.37.0 schemas. Offline tests cover prompt injection, seed and output naming,
and the unload/generate/restore lifecycle with simulated backends. This Qwen
workflow has not yet had a live generation test. Its approximately 6.76 GiB
diffusion weights and 8.71 GiB text encoder cannot fit in 12 GiB simultaneously;
runtime memory management and generation duration need verification on this
GPU. If increasing `IMAGE_GENERATION_SECONDS`, also allow enough Cline tool
timeout for unloading and cleanup/restoration.

The active Cline settings are at
`C:\Users\garre\.cline\data\settings\cline_mcp_settings.json`.
They select `qwen3.8-flash-next:125b-a6b-q4_K_M`, `num_ctx=131072`, a
20-minute tool timeout, and the installed Python 3.13 interpreter. Continue's
configuration was not changed.

## Before generating

Pause Continue autocomplete and other Ollama requests. Pausing prevents new
requests but does not release cached model weights. Once those requests have
finished, unload the cached auxiliary models in PowerShell:

```powershell
ollama stop qwen2.5-coder:1.5b-base
ollama stop nomic-embed-text:latest
```

Keep the Cline Qwen chat model loaded. Then ask Cline to call `generate_image`.
Resume Continue after image generation and model restoration finish; it can
load its models again when needed. This manual pause remains necessary:
Continue's requests do not participate in the MCP server's GPU lease.

`EMBED_NUM_GPU=0` keeps this MCP server's own embedding requests on CPU. It does
not change requests made by Continue. If another application reloads Nomic on
the GPU, image preflight rejects it until it is unloaded again.

## Memory settings

The installed GPU is an RTX 5070 with approximately 12 GiB VRAM. The image
admission budget is 8 GiB. The earlier SDXL workflow completed a real
1024 x 1024 generation. Driver samples showed approximately 8,356 MiB total GPU usage
during generation (sampling is not an exact peak).
`IMAGE_NVIDIA_SMI_PATH` enables the NVIDIA driver probe because ComfyUI/PyTorch
reported considerably more free memory than the driver during setup. Admission
uses the lower reading. Never lower the budget just to bypass other GPU users.

For that earlier SDXL test, settings, tool discovery, and the image response were
verified over real MCP stdio. The test initially exposed a keep-alive encoding
bug; it was fixed and the existing image was recovered without regenerating it.
Qwen's original digest, 131072-token context, and 6741074901-byte GPU allocation
were restored. Its cold reload took approximately 87 seconds. A live embedding
request returned 768 dimensions with zero GPU allocation.

The test image is
`E:\Comfy-Desktop\ComfyUI-Shared\output\mcp\f0398ba2-4861-4441-bf55-ce9c758d0e40_00001_.png`.
Detailed results are in `.image-orchestration/hardware-test-report.json`.

Future image tests require the pause described above. An unfinished
transaction leaves the GPU gate closed; inspect its recovery error instead of
deleting `.image-orchestration/jobs.sqlite3`.
