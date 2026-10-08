"""Offline crop zoom checks through MCP, plus the PowerShell pixel regressions."""
from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import anyio
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_orchestration import crop_tool
from image_orchestration.tool import register as register_generation


def write_fixture(path: Path):
    """A tiny opaque RGBA PNG, without adding an imaging dependency to tests."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(
        b"\0" + b"".join(bytes((x * 20, y * 20, 80, 255)) for x in range(10)) for y in range(8)
    )
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 10, 8, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class Discovery(unittest.TestCase):
    def test_generation_points_to_inspection_and_schema_is_documented(self):
        async def run():
            server = MCPServer("crop-discovery")
            crop_tool.register(server)
            register_generation(server)
            async with Client(server) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                self.assertIn("crop_zoom", tools["generate_image"].description)
                tool = tools["crop_zoom"]
                self.assertEqual(tool.input_schema["required"], ["image_path"])
                self.assertTrue(all(p.get("description") for p in tool.input_schema["properties"].values()))
                self.assertFalse(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
                for kwargs in ({"scale": 0}, {"regions": {}}, {"regions": {"bad": [0, 0, 1]}}, {"units": "guess"}):
                    result = await client.call_tool("crop_zoom", {"image_path": "missing.png", **kwargs})
                    self.assertTrue(result.is_error)
        anyio.run(run)


@unittest.skipUnless(shutil.which("powershell.exe"), "Requires Windows PowerShell/System.Drawing")
class CropZoom(unittest.TestCase):
    def test_powershell_pixel_regressions(self):
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(ROOT / "dev/crop_zoom_test.ps1")], capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors="replace"))

    def test_named_crops_inline_images_and_non_overwriting_batches(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                source = Path(td) / "sprite $(exit) [\u00e9].png"
                write_fixture(source)
                original = source.read_bytes()
                server = MCPServer("crop-test")
                crop_tool.register(server)
                async with Client(server) as client:
                    arguments = {"image_path": str(source), "regions": {"hand": [2, 2, 3, 2], "face": [0, 0, 2, 2]},
                                 "units": "Pixels", "scale": 2, "output_dir": str(Path(td) / "inspection")}
                    result = await client.call_tool("crop_zoom", arguments)
                    self.assertFalse(result.is_error, result)
                    payload = result.structured_content
                    self.assertEqual(payload["source"]["path"], str(source.resolve()))
                    self.assertEqual(len(payload["images"]), 2)
                    previews = [c for c in result.content if c.type == "image"]
                    self.assertEqual(len(previews), 2)
                    for crop, preview in zip(payload["images"], previews):
                        raw = base64.b64decode(preview.data)
                        self.assertEqual(preview.mime_type, "image/png")
                        self.assertEqual(raw, Path(crop["path"]).read_bytes())
                        self.assertEqual(struct.unpack(">II", raw[16:24]), (crop["width"], crop["height"]))
                    self.assertEqual(payload["images"][0]["crop"], {"x": 2, "y": 2, "width": 3, "height": 2})
                    self.assertEqual((payload["images"][0]["width"], payload["images"][0]["height"]), (6, 4))
                    again = await client.call_tool("crop_zoom", {**arguments, "include_images": False})
                    self.assertFalse(again.is_error, again)
                    self.assertNotEqual(payload["output_dir"], again.structured_content["output_dir"])
                    self.assertFalse(any(c.type == "image" for c in again.content))
                    self.assertEqual(source.read_bytes(), original)
                    with patch.object(crop_tool, "MAX_INLINE_BYTES", 1):
                        limited = await client.call_tool("crop_zoom", arguments)
                    self.assertFalse(limited.is_error, limited)
                    self.assertFalse(any(c.type == "image" for c in limited.content))
                    self.assertTrue(all(not c["preview_included"] and Path(c["path"]).is_file()
                                        for c in limited.structured_content["images"]))
        anyio.run(run)

    def test_bad_regions_are_errors_without_partial_output(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                source = Path(td) / "sprite.png"
                write_fixture(source)
                output = Path(td) / "invalid"
                server = MCPServer("crop-errors")
                crop_tool.register(server)
                async with Client(server) as client:
                    for changes in ({"regions": {"valid": [0, 0, 1, 1], "bad": [0.9, 0, 0.2, 1]}},
                                    {"regions": {"../escape": [0, 0, 1, 1]}}, {"scale": 1000},
                                    {"regions": {"hand": [0, 0, 1, 1], "HAND": [0, 0, 1, 1]}},
                                    {"image_path": "relative.png"}, {"image_path": str(Path(td) / "missing.png")},
                                    {"output_dir": "relative"}):
                        result = await client.call_tool("crop_zoom", {"image_path": str(source), "output_dir": str(output), **changes})
                        self.assertTrue(result.is_error, result)
                        self.assertEqual(result.structured_content["status"], "failed")
                        self.assertFalse(output.exists())
        anyio.run(run)

    def test_actual_server_stdio_from_another_project_without_generation(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                source = Path(td) / "another-project.png"
                write_fixture(source)
                params = StdioServerParameters(command=sys.executable, args=["-B", str(ROOT / "server.py")],
                                               cwd=td, env={**os.environ, "IMAGE_MODEL": ""})
                async with Client(params) as client:
                    tools = {t.name for t in (await client.list_tools()).tools}
                    self.assertIn("crop_zoom", tools)
                    self.assertNotIn("generate_image", tools)
                    result = await client.call_tool("crop_zoom", {"image_path": str(source), "scale": 1})
                    self.assertFalse(result.is_error, result)
                    self.assertEqual(result.structured_content["images"][0]["width"], 10)
                    self.assertTrue(any(c.type == "image" for c in result.content))
        anyio.run(run)


if __name__ == "__main__":
    unittest.main()
