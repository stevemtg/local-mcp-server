"""Apply the prepared local image settings to Cline, preserving other servers.

Creates a timestamped backup beside the original and atomically replaces JSON.
Run only after reviewing workflows/cline-server-settings.json.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = Path.home() / ".cline/data/settings/cline_mcp_settings.json"
    prepared = json.loads((ROOT / "workflows/cline-server-settings.json").read_text(encoding="utf-8"))
    config = json.loads(target.read_text(encoding="utf-8-sig"))
    server = config["mcpServers"]["codebase-semantic-index"]
    server["command"] = prepared["command"]
    server["args"] = prepared["args"]
    server["timeout"] = prepared["timeout"]
    server.setdefault("env", {}).update(prepared["env"])
    approvals = server.setdefault("autoApprove", [])
    if "generate_image" not in approvals:
        approvals.append("generate_image")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = target.with_name(target.name + ".backup-image-" + stamp)
    shutil.copy2(target, backup)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=target.name + ".", suffix=".tmp", delete=False) as f:
            temp_path = Path(f.name)
            json.dump(config, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    print("Updated:", target)
    print("Backup:", backup)
    print("Continue configuration was not modified.")


if __name__ == "__main__":
    main()
