# Local ComfyUI image setup

`image.json` uses the installed `AnythingXL_xl.safetensors` checkpoint at:

`E:\Comfy-Desktop\ComfyUI-Shared\models\checkpoints\AnythingXL_xl.safetensors`

The safetensors header was inspected: it contains the two SDXL text encoders,
diffusion weights, and VAE. ComfyUI 0.34.3 advertises this checkpoint through
`CheckpointLoaderSimple`. The graph generates one 1024 x 1024 image in 25 steps.
Outputs go to `E:\Comfy-Desktop\ComfyUI-Shared\output\mcp`.

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
admission budget is 8 GiB. A real 1024 x 1024 generation completed with this
configuration. Driver samples showed approximately 8,356 MiB total GPU usage
during generation (sampling is not an exact peak).
`IMAGE_NVIDIA_SMI_PATH` enables the NVIDIA driver probe because ComfyUI/PyTorch
reported considerably more free memory than the driver during setup. Admission
uses the lower reading. Never lower the budget just to bypass other GPU users.

The server's settings, tool discovery, and successful image response were
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
