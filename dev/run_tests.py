"""Run regressions; --offline skips the MCP e2e suite that needs live Ollama."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

# Keep Unicode diagnostics readable on Windows consoles and in captured output.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--offline", action="store_true", help="Skip live Ollama e2e tests")
args = parser.parse_args()
failed = False
for name in ("dev/smoke_test.py", "dev/chunker_test.py", "dev/csharp_test.py", "dev/search_test.py", "dev/graph_test.py", "dev/memory_test.py", "dev/e2e_test.py", "dev/image_orchestration_test.py", "dev/image_orchestration_regression_test.py", "dev/image_multimodel_test.py", "dev/crop_zoom_test.py", "dev/check_transparency_test.py", "dev/remove_bg_test.py"):
    if args.offline and name == "dev/e2e_test.py":
        print(name, "SKIP (requires live Ollama)")
        continue
    p = subprocess.run(
        [sys.executable, str(root / name)], capture_output=True,
        cwd=root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
    )
    out = (p.stdout + p.stderr).decode("utf-8", errors="replace").splitlines()
    print(name, "EXIT", p.returncode)
    for line in (out if p.returncode else out[-4:]):
        print("   |", line)
    failed = failed or p.returncode != 0
sys.exit(1 if failed else 0)
