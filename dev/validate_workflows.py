"""Validate bundled workflow mappings; optionally check live ComfyUI schemas.

Uses read-only object_info calls. Never queues work or loads models.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_orchestration.settings import Settings


def validate_schema(graph, schemas):
    for node_id, node in graph.items():
        schema = schemas[node["class_type"]]
        inputs = node["inputs"]
        required = schema["input"].get("required", {})
        specs = {**required, **schema["input"].get("optional", {})}
        for name, spec in required.items():
            # An autogrow with min=0 represents no reference images in T2I.
            if spec[0] == "COMFY_AUTOGROW_V3" and spec[1]["template"]["min"] == 0:
                continue
            if name not in inputs:
                raise ValueError(f"Node {node_id}: missing {name}")
        for name, value in inputs.items():
            expected = specs[name][0]
            options = specs[name][1] if len(specs[name]) > 1 else {}
            if isinstance(value, list):
                source, slot = value
                if type(slot) is not int or slot < 0:
                    raise ValueError(f"Node {node_id}: invalid output slot")
                actual = schemas[graph[source]["class_type"]]["output"][slot]
                if actual != expected:
                    raise ValueError(f"Node {node_id}: {name} expects {expected}, got {actual}")
            elif isinstance(expected, list):
                if value not in expected:
                    raise ValueError(f"Node {node_id}: unavailable {name}={value!r}")
            else:
                types = {"STRING": (str,), "INT": (int,), "FLOAT": (int, float), "BOOLEAN": (bool,)}
                if type(value) not in types.get(expected, ()):
                    raise ValueError(f"Node {node_id}: invalid type for {name}")
                for bound, compare in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
                    if bound in options and compare(value, options[bound]):
                        raise ValueError(f"Node {node_id}: {name} outside {bound}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfy-url", help="Check installed nodes/models using GET /object_info")
    parser.add_argument("names", nargs="*", default=["image.json", "image-simple.json", "image-krea2-turbo.json"])
    args = parser.parse_args()
    schemas = {}
    with tempfile.TemporaryDirectory() as td:
        for name in args.names:
            graph = json.loads((ROOT / "workflows" / name).read_text(encoding="utf-8"))
            negative = "6" if graph["6"]["class_type"] == "TextEncodeQwenImage21" else "7"
            settings = Settings(model="validation-only", load_options={"num_ctx": 4096},
                                workflow=graph, negative_node=negative, output_root=Path(td),
                                state_root=Path(td), image_vram_bytes=1)
            if args.comfy_url:
                for node in graph.values():
                    kind = node["class_type"]
                    if kind not in schemas:
                        with urllib.request.urlopen(args.comfy_url.rstrip("/") + "/object_info/" + kind, timeout=10) as response:
                            schemas.update(json.load(response))
                validate_schema(graph, schemas)
            print(f"{name}: mappings OK ({settings.workflow_bindings()})" +
                  ("; installed node schemas, model names and links OK" if args.comfy_url else ""))


if __name__ == "__main__":
    main()
