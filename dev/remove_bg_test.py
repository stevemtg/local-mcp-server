"""Offline remove_bg checks through MCP, plus the PowerShell regressions."""
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
from image_orchestration import removebg_tool, transparency_tool
from image_orchestration.tool import register as register_generation


def write_fixture(path: Path):
    """A tiny RGBA PNG: opaque-white 10x8 field, an opaque black 6x4 block at
    (2,2) and one A=128 pixel at (5,3); no imaging library dependency. RGBA."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    def pixel(x, y):
        if not (2 <= x < 8 and 2 <= y < 6):
            return bytes((255, 255, 255, 255))
        if (x, y) == (5, 3):
            return bytes((200, 100, 50, 128))
        return bytes((0, 0, 0, 255))
    rows = b"".join(b"\0" + b"".join(pixel(x, y) for x in range(10)) for y in range(8))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 10, 8, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class Discovery(unittest.TestCase):
    def test_generation_points_to_removal_and_schema_is_documented(self):
        async def run():
            server = MCPServer("removebg-discovery")
            removebg_tool.register(server)
            register_generation(server)
            async with Client(server) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                self.assertIn("remove_bg", tools["generate_image"].description)
                tool = tools["remove_bg"]
                self.assertEqual(tool.input_schema["required"], ["image_path"])
                self.assertTrue(all(p.get("description") for p in tool.input_schema["properties"].values()))
                self.assertFalse(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.destructive_hint)
                self.assertTrue(tool.annotations.idempotent_hint)
                for args in ({"image_path": "relative.png"},
                             {"image_path": str(Path(tempfile.gettempdir()) / "missing-rb-fixture.png")}):
                    result = await client.call_tool("remove_bg", args)
                    self.assertTrue(result.is_error)
        anyio.run(run)


@unittest.skipUnless(shutil.which("powershell.exe"), "Requires Windows PowerShell/System.Drawing")
class RemoveBackground(unittest.TestCase):
    def test_powershell_regressions(self):
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(ROOT / "dev/remove_bg_test.ps1")], capture_output=True, timeout=180)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors="replace"))

    def test_removes_white_background_into_new_png_without_touching_source(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                source = Path(td) / "sprite.png"
                write_fixture(source)
                original = source.read_bytes()
                server = MCPServer("removebg-test")
                removebg_tool.register(server)
                async with Client(server) as client:
                    result = await client.call_tool("remove_bg", {"image_path": str(source)})
                    self.assertFalse(result.is_error, result)
                    payload = result.structured_content
                    self.assertEqual(payload["status"], "succeeded")
                    self.assertEqual(payload["source"]["path"], str(source.resolve()))
                    self.assertTrue(payload["source"]["alpha_channel"])
                    self.assertEqual((payload["source"]["width"], payload["source"]["height"]), (10, 8))
                    self.assertEqual(payload["settings"], {"threshold": 225, "edge_width": 2})
                    self.assertEqual(payload["output"]["path"].lower(),
                                     str(source.with_name("sprite_transparent.png").resolve()).lower())
                    self.assertEqual(payload["output"]["alpha"]["pixels"], 80)
                    self.assertEqual(payload["output"]["alpha"]["transparent"], 56)
                    self.assertEqual(payload["output"]["alpha"]["partial"], 1)
                    self.assertEqual(payload["output"]["alpha"]["opaque"], 23)
                    self.assertEqual(payload["output"]["corners"]["top_left"],
                                     {"a": 0, "r": 0, "g": 0, "b": 0})
                    self.assertEqual(payload["output"]["content_bounds"],
                                     {"x": 2, "y": 2, "width": 6, "height": 4})
                    self.assertEqual(json.loads(result.content[0].text), payload)
                    self.assertIn("note", payload)
                    self.assertEqual(source.read_bytes(), original)
                    for key in ("source", "output"):
                        inspection = await transparency_tool.inspect_transparency(payload[key]["path"])
                        expected = {**inspection["source"], **{name: inspection[name] for name in (
                            "alpha", "corners", "content_bounds")}}
                        self.assertEqual(payload[key], expected)
        anyio.run(run)

    def test_out_override_and_source_collision(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                source = Path(td) / "sprite.png"
                write_fixture(source)
                server = MCPServer("removebg-out")
                removebg_tool.register(server)
                async with Client(server) as client:
                    other = Path(td) / "cut out [v2].png"
                    result = await client.call_tool("remove_bg", {"image_path": str(source), "out": str(other)})
                    self.assertFalse(result.is_error, result)
                    self.assertEqual(result.structured_content["output"]["path"].lower(), str(other.resolve()).lower())
                    self.assertTrue(other.is_file())
                    for args in ({"image_path": str(source), "out": str(source)},
                                 {"image_path": str(source), "out": "relative-out.png"}):
                        result = await client.call_tool("remove_bg", args)
                        self.assertTrue(result.is_error, result)
                        self.assertEqual(result.structured_content["status"], "failed")
                        self.assertEqual(result.structured_content["error"]["code"], "background_removal_failed")
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
                    self.assertIn("remove_bg", tools)
                    self.assertNotIn("generate_image", tools)
                    result = await client.call_tool("remove_bg", {"image_path": str(source)})
                    self.assertFalse(result.is_error, result)
                    self.assertEqual(result.structured_content["output"]["alpha"]["opaque"], 23)
        anyio.run(run)

    def test_existing_destinations_and_source_alias_are_never_overwritten(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                source = Path(td) / "sprite.png"
                write_fixture(source)
                original = source.read_bytes()
                destination = source.with_name("sprite_transparent.png")
                destination.write_bytes(b"keep this existing file")
                result = await removebg_tool.remove_background(str(source), None, 225, 2)
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(destination.read_bytes(), b"keep this existing file")
                alias = source.with_name("source-alias.png")
                os.link(source, alias)
                result = await removebg_tool.remove_background(str(source), str(alias), 225, 2)
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(alias.read_bytes(), original)
                invalid = source.with_name("not-a-png.jpg")
                result = await removebg_tool.remove_background(str(source), str(invalid), 225, 2)
                self.assertEqual(result["status"], "failed", result)
                self.assertFalse(invalid.exists())
        anyio.run(run)

    def test_concurrent_calls_publish_only_one_complete_output(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                source = Path(td) / "sprite.png"
                write_fixture(source)
                results = []

                async def remove():
                    results.append(await removebg_tool.remove_background(str(source), None, 225, 2))

                async with anyio.create_task_group() as group:
                    group.start_soon(remove)
                    group.start_soon(remove)
                self.assertEqual(sorted(r["status"] for r in results), ["failed", "succeeded"])
                output = source.with_name("sprite_transparent.png")
                report = await transparency_tool.inspect_transparency(str(output))
                self.assertEqual(report["alpha"]["transparent"], 56)
                self.assertEqual(set(Path(td).iterdir()), {source, output})
        anyio.run(run)


if __name__ == "__main__":
    unittest.main()
