"""Run offline regressions and MCP e2e tests, failing if any suite fails."""
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
failed = False
for name in ("dev/smoke_test.py", "dev/chunker_test.py", "dev/csharp_test.py", "dev/search_test.py", "dev/e2e_test.py", "dev/image_orchestration_test.py", "dev/image_orchestration_regression_test.py", "dev/crop_zoom_test.py"):
    p = subprocess.run(
        [sys.executable, str(root / name)], capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    out = (p.stdout + p.stderr).decode(errors="replace").splitlines()
    print(name, "EXIT", p.returncode)
    for line in (out if p.returncode else out[-4:]):
        print("   |", line)
    failed = failed or p.returncode != 0
sys.exit(1 if failed else 0)
