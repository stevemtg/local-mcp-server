# MCP tool reference

[Back to the README](../README.md)

## Generate an image

`generate_image(prompt, negative_prompt?, request_id?)` is available when
`IMAGE_MODEL` is configured. It runs the configured ComfyUI workflow and returns
status, absolute `image_path` / `image_paths`, and `llm_restored`. Optional
`request_id` is a UUID reused only for retries of the same request. See the
[image generation guide](image-generation.md) for setup and recovery behavior.

## Always-available tools

| Tool | Read-only? | Parameters | Returns |
| --- | --- | --- | --- |
| `index_codebase` | no (writes `.semantic_index/`) | `directory_path: string` | `{files_embedded, files_unchanged_skipped, files_removed, files_failed, total_chunks, full_reindex, indexed_at}` |
| `semantic_search` | yes | `query: string`, `directory_path: string`, `limit?`, `path_filter?`, `min_score?` | `{results: [{path, absolutePath, startLine, endLine, symbol, score, snippet, reason}]}` |
| `semantic_search_similar` | yes | `directory_path: string`, `path: string`, `start_line?`, `end_line?`, `limit?` | same `results` shape, ranked by similarity to the **current** source at `path` |
| `get_symbol_context` | yes | `directory_path: string`, `path: string`, `line: int`, `max_references?` (default 12, clamped 1–50) | context pack for the live symbol at `path:line`: `{symbol: {name, kind, startLine, endLine, declaration}, snippet, imports, references, relatedTests, indexState, note}` — built from **live source**; `references` are lexical matches, not proven callers/callees |
| `get_index_status` | yes | `directory_path: string` | `{indexed, num_files, num_chunks, embedding_model, embedding_dim, created_at, last_indexed_at}` |
| `crop_zoom` | no (writes new crop PNGs) | `image_path: string`, `regions?`, `units?`, `scale?`, `output_dir?`, `interpolation?`, `include_images?` | source dimensions, crop bounds/dimensions/absolute paths, and inline PNG previews |
| `check_transparency` | yes | `image_path: string` | `{status, source: {path, width, height, pixel_format, alpha_channel}, corners, alpha: {pixels, transparent, partial, opaque, min_alpha, max_alpha, transparent_fraction, partial_fraction, opaque_fraction}, content_bounds, note}`; fractions are 0–1 |
| `remove_bg` | no (writes a NEW `<source-stem>_transparent.png`; rejects existing output paths) | `image_path: string`, `out?`, `threshold?` (0–255, default 225), `edge_width?` (0–16, default 2) | `{status, source, output, settings: {threshold, edge_width}, note}`; each image report contains `path`, `width`, `height`, `pixel_format`, `alpha_channel`, `corners`, `alpha`, and `content_bounds` |

## Structural graph tools

The complementary structural tools are `codebase_build_relations(directory_path)`,
`codebase_query_graph(directory_path, target_symbol, relationship_type, limit?)`,
and `codebase_agentic_navigate(directory_path, current_path, action)`. They need no
Ollama or vector index. See the [structural graph guide](structural-graph.md) for
their contracts, semantic-result integration, limits, and examples.

## Inspect generated image details

`crop_zoom` is available to every project using this MCP server, even when
`generate_image` is disabled. It uses Windows PowerShell and System.Drawing
on the server host, with no GPU, Ollama, ComfyUI, index, or extra Python imaging
dependency. Reconnect the MCP client after updating the server to discover it.

After `generate_image`, pass its returned `image_path` to `crop_zoom` and select
details to inspect. The generation tool's published description includes this
guidance. For example:

```json
{
  "image_path": "C:/art/project/generated.png",
  "regions": {
    "hand": [0.20, 0.36, 0.32, 0.36],
    "face": [0.35, 0.10, 0.30, 0.25]
  },
  "units": "Relative",
  "scale": 3
}
```

Regions are caller-selected `[x, y, width, height]` rectangles from the top-left;
the example coordinates are not automatic hand/face detection. `Relative` uses
fractions of source dimensions and rounds bounds outward; `Pixels` uses integer
pixel coordinates. Omit `regions` to zoom the full image. `NearestNeighbor` is
the default interpolation; `HighQualityBilinear` and `HighQualityBicubic` provide
smoother previews. Use `scale: 1` for unscaled crops.

The caller visually checks the PNGs; the tool does not judge anatomy or quality.
It supports up to 8 regions and 16 million total output pixels per call, and
rejects invalid/out-of-bounds crops before writing a batch. Each call saves a
new batch folder inside `output_dir` (an absolute path), or `crops` beside the
source by default. Results include absolute paths and pixel bounds for every
crop. Inline PNGs are limited to 8 MiB per response; `preview_included` identifies
which are attached. Reduce crop size/scale if a preview is omitted, or set
`include_images: false` for paths and metadata only.

The same crop implementation is also usable directly in PowerShell:

```powershell
.\tools\crop-zoom.ps1 -Path .\generated.png -Regions @{ detail = @(100, 200, 160, 180) } -Units Pixels -Scale 3 -OutDir .\inspection
```

The script defaults to `crops` beside the source, with filenames
`<source-stem>_<region>.png`; direct script reruns replace matching crop files.
Use `Get-Help .\tools\crop-zoom.ps1 -Full` for all options.

## Verify alpha transparency

`check_transparency` is available to every project using this MCP server, even
when `generate_image` is disabled. Like `crop_zoom` it uses Windows PowerShell
and System.Drawing on the server host with no GPU, Ollama, ComfyUI, index, or
extra Python imaging dependency, and it is strictly read-only: it never writes
files or alters the inspected image.

After `generate_image`, pass the same `image_path` to `check_transparency`. It
scans every pixel and reports the source `pixel_format` and whether its format
or palette carries alpha, the four corner pixel colors, the counts of fully
transparent (`A=0`), partially transparent (`0<A<255`), and fully opaque
(`A=255`) pixels with fractions from 0 to 1, and the bounding box of all pixels
with `A>0` (`content_bounds`, `null` for a fully transparent image). Counts and
corners alone cannot prove that the intended background is transparent; inspect
the image too. Artwork can legitimately touch a corner. A painted checkerboard
does not imply transparency.

```json
{"image_path": "C:/art/project/generated.png"}
```

The same inspection is also usable directly in PowerShell:

```powershell
.\tools\check-transparency.ps1 -Path .\sprite.png
```

Use `Get-Help .\tools\check-transparency.ps1 -Full` for all options.

## Remove a white background

`remove_bg` is available to every project using this MCP server, even when
`generate_image` is disabled, and like the other two image tools it needs no
index, ComfyUI, Ollama, or GPU. It is CPU-only and writes only the new output
file: the source image is never modified.

When inspection confirms a generated sprite still has an opaque
white/near-white background, pass that `image_path` to `remove_bg`. It
flood-fills the border-connected near-white background (including diagonal
gaps) to fully transparent, then un-mattes the narrow antialiased edge band
by removing the white contribution from RGB as well as alpha. Enclosed white
sprite details and translucent artwork are preserved. The output defaults to
`<source-stem>_transparent.png` beside the source; `threshold` tunes what
counts as background and `edge_width: 0` cuts hard with no edge correction.
Existing output paths are rejected, so supply a new `.png` path in `out` for
repeat calls. Edge cleanup estimates foreground coverage and can change light
foreground edges; inspect the cutout visually as well as checking its alpha.
The result reports source/output metadata with alpha counts and
`content_bounds` — re-verify the new file with `check_transparency`.

```json
{"image_path": "C:/art/project/generated.png"}
```

The same removal is also usable directly in PowerShell:

```powershell
.\tools\remove-bg.ps1 -Path .\sprite.png -Out .\sprite-transparent.png
```

Use `Get-Help .\tools\remove-bg.ps1 -Full` for all options.

## Choosing and calling a search tool

- Use `semantic_search` for one behavior described in words, such as
  `retry failed requests with exponential backoff`. Use
  `semantic_search_similar` for implementations resembling an existing file
  or complete method. Use exact search for known identifiers and strings.
- Both searches need an existing index and the configured Ollama embedding
  model. They never create or refresh a project index. A missing/empty index
  returns an empty result with guidance; an incompatible model raises an
  error explaining how to re-index. `get_index_status` reports stored index
  metadata; it does not check every live file for changes.
- `limit` accepts **1–50** (default **8**, configurable). `path_filter` accepts
  project-relative globs or segments, such as `src/auth/*`, `*.cs`, or `tests`.
  `min_score` is an optional cosine threshold from **-1 to 1**; omit it initially.
  Scores measure similarity, not confidence. Semantic search balances
  relevance and diversity, so results need not have descending scores.
- Similarity search requires a project-relative `path` and reads live source,
  even if that file is unindexed. Bounds are **1-based and inclusive**;
  `start_line` defaults to 1 and `end_line` defaults to EOF independently.
  Invalid, reversed, or out-of-file ranges raise errors. The returned `range`
  contains resolved bounds, or `null` when both were omitted.
- Similarity search excludes overlapping chunks in the query file; a whole-file
  query excludes that entire file, including stale chunks past the current EOF.
  Results are deduplicated using their best cosine score. Large queries use at
  most `SIMILAR_QUERY_MAX_PARTS` chunks; the response `note` reports any cap.
- Result symbols, snippets, and line numbers come from the stored index.
  Existing indexes can retain older chunk labels after a parser update.
  Follow promising hits with `get_symbol_context` and read the live file.
  Empty results do not establish absence: check index state, broaden filters,
  or rephrase the query.

All three discovery/context tools publish parameter descriptions, input examples,
and MCP `readOnlyHint`/`openWorldHint` annotations through `tools/list`.
Search limits and line numbers carry validation constraints; `max_references`
keeps its documented clamping behavior.
