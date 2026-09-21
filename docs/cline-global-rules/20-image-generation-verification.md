# Image generation and visual verification

Apply this rule whenever generating images with the MCP server's `generate_image`
tool, in any project. Use Cline's MCP tool interface and the connected server
that advertises `generate_image` and `crop_zoom`.

## Generate

- Every `generate_image` call MUST include the exact phrase
  `transparent background` in its `prompt` argument, which is the positive
  prompt. This also applies to every revision and regeneration.
- Preserve the requested subject, style, and composition. Use `negative_prompt`
  for unwanted features and artifacts; do not put `transparent background` in
  the negative prompt.
- `generate_image` runs the configured ComfyUI workflow, restores the original
  Ollama model, and returns execution status and absolute `image_path` /
  `image_paths`. Check the result before proceeding. If it reports
  `recovery_required`, report the issue and stop further GPU work until recovery.
- The optional `request_id` is for retries of the same request. Omit it for a
  revised prompt or a new generation.

Example arguments; replace the subject description with the actual request:

```json
{
  "prompt": "[Requested subject, style, and composition], transparent background",
  "negative_prompt": "extra fingers, fused fingers, malformed hands, extra limbs, distorted anatomy, stray artifacts"
}
```

## Inspect with crop_zoom

- Inspect the full image, then ALWAYS call `crop_zoom` before accepting it.
  Verify each image intended for delivery.
- Pass the generated image's absolute path as `image_path`. Choose crops from
  the actual image, covering every visible hand and other relevant details:
  fingers, faces, eyes, teeth, ears, feet, joints, overlapping limbs, held
  objects, text, and cutout edges. For subjects without anatomy, inspect fine
  details, object boundaries, and any suspicious areas.
- `regions` maps descriptive labels to `[x, y, width, height]`, measured from
  the top-left. Use `units="Relative"` for fractions of the source dimensions
  or `units="Pixels"` for integer coordinates. Keep every crop inside the image.
- Start with `scale=3` and `include_images=true`. Each call accepts up to eight
  regions; make more calls when needed. If previews are omitted because of
  size limits, reduce the crop size or scale, or open the saved crop files.
- `crop_zoom` uses the CPU to save enlarged PNG crops and return previews and
  paths. It does not detect or repair defects automatically. You MUST visually
  examine the returned crops; a successful tool call alone is not verification.

## Correct and verify

- Check for extra, missing, fused, or distorted fingers and limbs; unnatural
  joints or grips; malformed facial features; unintended asymmetry; intersecting
  objects; garbled text; and stray pixels or halos. Judge anatomy against the
  intended subject, pose, occlusion, and style.
- If visible defects remain, revise the prompt, regenerate, and repeat both
  the full-image inspection and `crop_zoom` inspection on the replacement.
  Keep `transparent background` in every positive prompt.
- Check actual background transparency where possible, including the alpha
  channel. Prompt wording, a solid background, or a painted checkerboard is
  not proof of transparency.
- Deliver the final image with a brief statement of the checks actually
  performed. If tools, visual inspection, or transparency verification are
  unavailable, disclose that limitation. Never label an uninspected image as
  verified or imply that visible defects have been resolved when they remain.
