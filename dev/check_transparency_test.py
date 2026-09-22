"""Offline check_transparency checks through MCP, plus the PowerShell regressions."""
from __future__ import annotations

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

import anyio
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_orchestration import transparency_tool
from image_orchestration.tool import register as register_generation


def write_fixture(path: Path):
    """A tiny RGBA PNG: a 6x4 block at (2,2), one pixel at A=128, no imaging dependency."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    def pixel(x, y):
        if not (2 <= x < 8 and 2 <= y < 6):
            return bytes((0, 0, 0, 0))
        if (x, y) == (5, 3):
            return bytes((200, 200, 200, 128))
        return bytes((x * 20, y * 20, 80, 255))
    rows = b"".join(b"\0" + b"".join(pixel(x, y) for x in range(10)) for y in range(8))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 10, 8, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def write_fully_transparent(path: Path):
    """A 4x4 PNG whose every pixel has alpha 0."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + b"\x00\x00\x00\x00" * 4 for y in range(4))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class Discovery(unittest.TestCase):
    def test_generation_points_to_transparency_and_schema_is_documented(self):
        async def run():
            server = MCPServer("transparency-discovery")
            transparency_tool.register(server)
            register_generation(server)
            async with Client(server) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                self.assertIn("check_transparency", tools["generate_image"].description)
                tool = tools["check_transparency"]
                self.assertEqual(tool.input_schema["required"], ["image_path"])
                self.assertTrue(all(p.get("description") for p in tool.input_schema["properties"].values()))
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
                for path in ("relative.png", str(Path("<missing>") / "missing.png")):
                    result = await client.call_tool("check_transparency", {"image_path": path})
                    self.assertTrue(result.is_error)
        anyio.run(run)


@unittest.skipUnless(shutil.which("powershell.exe"), "Requires Windows PowerShell/System.Drawing")
class CheckTransparency(unittest.TestCase):
    def test_indexed_transparency_and_opaque_rgb(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        async def run():
            with tempfile.TemporaryDirectory() as td:
                for indexed in (True, False):
                    source = Path(td) / ("indexed.png" if indexed else "rgb.png")
                    png = b"\x89PNG\r\n\x1a\n" + chunk(
                        b"IHDR", struct.pack(">IIBBBBB", 3, 1, 8, 3 if indexed else 2, 0, 0, 0))
                    if indexed:
                        png += chunk(b"PLTE", bytes((255, 255, 255, 255, 0, 0, 0, 0, 255)))
                        png += chunk(b"tRNS", bytes((0, 128, 255)))
                        pixels = bytes((0, 1, 2))
                    else:
                        pixels = bytes((255, 255, 255, 255, 0, 0, 0, 0, 255))
                    source.write_bytes(png + chunk(b"IDAT", zlib.compress(b"\0" + pixels)) + chunk(b"IEND", b""))
                    payload = await transparency_tool.inspect_transparency(str(source))
                    self.assertEqual(payload["status"], "succeeded", payload)
                    self.assertEqual(payload["source"]["alpha_channel"], indexed)
                    counts = payload["alpha"]
                    self.assertEqual((counts["transparent"], counts["partial"], counts["opaque"]),
                                     (1, 1, 1) if indexed else (0, 0, 3))
                    self.assertAlmostEqual(sum(counts[k] for k in (
                        "transparent_fraction", "partial_fraction", "opaque_fraction")), 1.0)
                    self.assertEqual(payload["corners"]["top_left"]["a"], 0 if indexed else 255)
                    self.assertEqual(payload["content_bounds"],
                                     {"x": 1 if indexed else 0, "y": 0, "width": 2 if indexed else 3, "height": 1})
        anyio.run(run)

    def test_powershell_regressions(self):
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(ROOT / "dev/check_transparency_test.ps1")], capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors="replace"))

    def test_reports_alpha_statistics_and_content_bounds(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                source = Path(td) / "sprite $(exit) [\u00e9].png"
                write_fixture(source)
                original = source.read_bytes()
                server = MCPServer("transparency-test")
                transparency_tool.register(server)
                async with Client(server) as client:
                    result = await client.call_tool("check_transparency", {"image_path": str(source)})
                    self.assertFalse(result.is_error, result)
                    payload = result.structured_content
                    self.assertEqual(payload["status"], "succeeded")
                    self.assertEqual(payload["source"]["path"], str(source.resolve()))
                    self.assertEqual((payload["source"]["width"], payload["source"]["height"]), (10, 8))
                    self.assertTrue(payload["source"]["alpha_channel"])
                    self.assertEqual(payload["corners"]["top_left"], {"a": 0, "r": 0, "g": 0, "b": 0})
                    self.assertEqual(payload["alpha"]["pixels"], 80)
                    self.assertEqual(payload["alpha"]["transparent"], 56)
                    self.assertEqual(payload["alpha"]["partial"], 1)
                    self.assertEqual(payload["alpha"]["opaque"], 23)
                    self.assertEqual(payload["alpha"]["min_alpha"], 0)
                    self.assertEqual(payload["alpha"]["max_alpha"], 255)
                    self.assertEqual(payload["content_bounds"], {"x": 2, "y": 2, "width": 6, "height": 4})
                    self.assertEqual(json.loads(result.content[0].text), payload)
                    self.assertEqual(source.read_bytes(), original)

                    blank = Path(td) / "blank.png"
                    write_fully_transparent(blank)
                    result = await client.call_tool("check_transparency", {"image_path": str(blank)})
                    self.assertFalse(result.is_error, result)
                    payload = result.structured_content
                    self.assertIsNone(payload["content_bounds"])
                    self.assertEqual(payload["alpha"]["transparent"], 16)
                    self.assertEqual(payload["alpha"]["opaque"], 0)
                    self.assertEqual(payload["alpha"]["transparent_fraction"], 1.0)
                    self.assertIn("note", payload)
        anyio.run(run)

    def test_bad_image_paths_are_errors_without_side_effects(self):
        async def run():
            server = MCPServer("transparency-errors")
            transparency_tool.register(server)
            async with Client(server) as client:
                for path in ("relative.png", str(Path(tempfile.gettempdir()) / "missing-ct-fixture.png")):
                    result = await client.call_tool("check_transparency", {"image_path": path})
                    self.assertTrue(result.is_error, result)
                    self.assertEqual(result.structured_content["status"], "failed")
                    self.assertEqual(result.structured_content["error"]["code"], "transparency_check_failed")
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
                    self.assertIn("check_transparency", tools)
                    self.assertNotIn("generate_image", tools)
                    result = await client.call_tool("check_transparency", {"image_path": str(source)})
                    self.assertFalse(result.is_error, result)
                    self.assertEqual(result.structured_content["alpha"]["pixels"], 80)
        anyio.run(run)


if __name__ == "__main__":
    unittest.main()

