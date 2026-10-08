# ComfyUI workflows

[Back to the README](../README.md) · [GPU coordination and recovery](../docs/image-generation.md)

These files are ComfyUI **API-format** graphs. Choose a graph, install its model
files and required nodes in your ComfyUI instance, then point
`IMAGE_WORKFLOW_PATH` at its absolute path. Model weights are not included.
Filenames in the graphs must match the files installed in ComfyUI.

## Included workflows

All three graphs request one 1024 × 1024 image, use 25 sampling steps, and save
through node `9`. The server supplies the prompts, seed, and output filename
prefix for each job; dimensions and sampler settings come from the graph.

| Workflow | Architecture | Positive / negative nodes | Sampler / save nodes |
| --- | --- | --- | --- |
| [`image-simple.json`](image-simple.json) | SDXL checkpoint | `6` / `7` | `3` / `9` |
| [`image.json`](image.json) | Qwen Image 2.1, separate loaders | `6` / `6` | `3` / `9` |
| [`image-krea2-turbo.json`](image-krea2-turbo.json) | Krea 2 Turbo, separate loaders | `6` / `7` | `3` / `9` |

### SDXL

`image-simple.json` uses `CheckpointLoaderSimple` with
`AnythingXL_xl.safetensors` in ComfyUI's `models/checkpoints/` directory.
You can edit `ckpt_name` to select another installed compatible SDXL checkpoint.
It uses Euler sampling, the normal scheduler, CFG 6, and denoise 1.

### Qwen Image 2.1

`image.json` expects these files relative to ComfyUI's model directory:

| Loader | Model file |
| --- | --- |
| `UNETLoader` | `diffusion_models/qwen_image_2.1_int8_convrot.safetensors` |
| `CLIPLoader` (`qwen_image`) | `text_encoders/qwen3vl_8b_int8_convrot.safetensors` |
| `VAELoader` | `vae/qwen_image_2.1_vae_bf16.safetensors` |

This graph also requires `TextEncodeQwenImage21`. That node emits both positive
and negative conditioning, so **both prompt mappings must point to node `6`**:

```json
{
  "IMAGE_WORKFLOW_PATH": "C:/path/to/local-mcp-server/workflows/image.json",
  "IMAGE_POSITIVE_NODE": "6",
  "IMAGE_NEGATIVE_NODE": "6",
  "IMAGE_SAMPLER_NODE": "3",
  "IMAGE_SAVE_NODE": "9"
}
```

The adapter updates the node's `prompt` and `negative_prompt` inputs separately.
The graph uses Euler sampling, the simple scheduler, CFG 1, and denoise 1. At
CFG 1, changing the negative prompt does not provide classifier-free guidance.

### Krea 2 Turbo

`image-krea2-turbo.json` expects these files:

| Loader | Model file |
| --- | --- |
| `UNETLoader` | `diffusion_models/krea2_turbo_fp8_scaled.safetensors` |
| `CLIPLoader` (`krea2`) | `text_encoders/qwen3vl_4b_fp8_scaled.safetensors` |
| `VAELoader` | `vae/Wan2_1_VAE_bf16.safetensors` |

It uses Euler sampling, the simple scheduler, CFG 1, and denoise 1. Use node `7`
for `IMAGE_NEGATIVE_NODE`, as with SDXL. Confirm your ComfyUI build supports the
listed loader types and model formats before use.

## Validate your setup

Run these commands from the repository root with its Python environment.
Validate all bundled graphs' server mappings without contacting ComfyUI:

```powershell
python -B dev/validate_workflows.py
```

Check their nodes, installed model names, and links against a running ComfyUI
instance using read-only schema requests:

```powershell
python -B dev/validate_workflows.py --comfy-url http://127.0.0.1:8188
```

To validate only the workflow you installed:

```powershell
python -B dev/validate_workflows.py --comfy-url http://127.0.0.1:8188 image-simple.json
```

These checks do not queue images or measure VRAM. Use ComfyUI's actual output
directory for `IMAGE_OUTPUT_ROOT`. Reconnect the MCP server after changing
workflow files or settings because the graph is loaded at startup.
`IMAGE_MODEL` always names your Ollama chat model, not the image model.

## GPU memory and verification status

An earlier local SDXL test completed a real 1024 × 1024 generation on an RTX 5070
with approximately 12 GiB VRAM and an 8 GiB admission budget. That is one tested
configuration, not a universal memory requirement. The Qwen workflow's nodes and
links were checked against a local ComfyUI 0.37.0 installation, but a live Qwen
generation has not been verified in this project's setup notes. The Krea graph
is retained as an alternative; offline validation alone does not establish live
generation compatibility.

Measure a budget for your workflow, including headroom. On Windows/WDDM,
`IMAGE_NVIDIA_SMI_PATH` can add a physical VRAM check; admission uses the lower
of the driver and ComfyUI readings. Allow enough tool timeout for generation
and all saved models to reload.

Pause external Ollama callers such as autocomplete before generation unless
they use the shared lease. The server automatically unloads and restores the
models it finds; you do not need to manually stop each cached model.
`EMBED_NUM_GPU=0` affects this server's embedding calls only.

If a transaction remains unfinished, inspect the recovery error and journal.
Do not delete `.image-orchestration/jobs.sqlite3` to bypass the GPU gate. See
[recovery behavior](../docs/image-generation.md) for details.
