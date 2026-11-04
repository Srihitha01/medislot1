"""Run portable, offline-friendly checks from the repository root."""
import compileall
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
env = os.environ.copy()
env.setdefault("AWS_REGION", "ap-south-1")
env.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
env.setdefault("AWS_EC2_METADATA_DISABLED", "true")
env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")

if not compileall.compile_dir(str(ROOT / "src"), quiet=1):
    raise SystemExit("Python compilation failed")

for suite in ("src/auth/tests", "src/slots/materializer/tests"):
    print(f"Running {suite}", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", suite, "-p", "test_*.py", "-v"],
        cwd=ROOT, env=env, check=False,
    )
    if result.returncode:
        raise SystemExit(result.returncode)
print("All checks passed.")
