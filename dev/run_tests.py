"""Run smoke + e2e tests as subprocesses and report exit codes (dev helper)."""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
for name in ("dev/smoke_test.py", "dev/e2e_test.py"):
    p = subprocess.run([sys.executable, str(root / name)], capture_output=True)
    out = p.stdout.decode(errors="replace").splitlines()
    print(name, "EXIT", p.returncode)
    for line in out[-2:]:
        print("   |", line)
