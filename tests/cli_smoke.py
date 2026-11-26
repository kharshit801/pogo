"""CI smoke test: drive the real `pogo` command the way a person would, on any OS.
Needs no admin rights and changes nothing on the machine.

    python tests/cli_smoke.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):  # CI consoles on Windows use an old code page
    _stream.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "POGO_HOME": tempfile.mkdtemp(prefix="pogo-smoke-"), "NO_COLOR": "1",
       "PYTHONIOENCODING": "utf-8",
       "POGO_NO_TELEMETRY": "1"}


def pogo(*args: str, expect: int = 0) -> str:
    print(f"$ pogo {' '.join(args)}", flush=True)
    result = subprocess.run([sys.executable, str(REPO / "bin" / "pogo"), *args], env=ENV, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    print(result.stdout + result.stderr, flush=True)
    if result.returncode != expect:
        raise SystemExit(f"FAILED: exit code {result.returncode}, expected {expect}")
    return result.stdout


assert "Get started" in pogo()
assert "examples:" in pogo("help", "on")
pogo("setup", "--host", "10.0.0.1", "--port", "3128", "--type", "http", "--user", "ci")
pogo("rules", "add", "*.example.com", "block", "--at", "1")
assert "block" in pogo("rules", "check", "www.example.com")
pogo("status")
pogo("env")
pogo("doctor", expect=1)  # no password saved for "ci": that's a problem doctor must report
pogo("tun", "--dry-run")
pogo("strat", expect=2)
print("SMOKE TEST PASSED")
