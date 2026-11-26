"""End-to-end test of all-apps mode on a real machine. Needs admin rights (GitHub's macOS
runners have passwordless sudo; its Windows runners run as an administrator).

1. start a fake authenticating proxy (tests/e2e/upstream.py)
2. pogo setup, pogo helper, pogo on
3. fetch https://example.com with every proxy setting disabled: only the capture can make
   that work
4. check pogo's log shows the connection was captured and sent through the proxy
5. pogo off, and check nothing is left behind

    python tests/e2e/run_e2e.py
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):  # CI consoles on Windows use an old code page
    _stream.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent.parent
HOME = Path(tempfile.mkdtemp(prefix="pogo-e2e-"))
PORT = 3129
ENV = {**os.environ, "POGO_HOME": str(HOME), "POGO_PASSWORD": "secret", "NO_COLOR": "1",
       "PYTHONIOENCODING": "utf-8",
       "POGO_NO_TELEMETRY": "1"}


def pogo(*args: str, check: bool = True) -> str:
    print(f"\n$ pogo {' '.join(args)}", flush=True)
    result = subprocess.run([sys.executable, str(REPO / "bin" / "pogo"), *args], env=ENV, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=300)
    print(result.stdout + result.stderr, flush=True)
    if check and result.returncode != 0:
        raise SystemExit(f"FAILED: pogo {' '.join(args)} exited with {result.returncode}")
    return result.stdout


def fetch(url: str) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no proxy: capture or nothing
    last = None
    for _ in range(5):
        try:
            with opener.open(url, timeout=30) as response:
                return response.status
        except OSError as e:
            last = e
            time.sleep(2)
    raise SystemExit(f"FAILED: couldn't fetch {url}: {last}")


def wait_for_port(port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise SystemExit("FAILED: the test proxy didn't start")


def show(name: str, path: Path, tail: int = 60) -> None:
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
        print(f"\n----- {name} ({path}) -----\n" + "\n".join(lines), flush=True)


def main() -> int:
    upstream_log = open(HOME / "upstream.log", "w", encoding="utf-8")
    upstream = subprocess.Popen([sys.executable, str(REPO / "tests" / "e2e" / "upstream.py"), str(PORT)],
                                env=ENV, stdout=upstream_log, stderr=subprocess.STDOUT)
    try:
        wait_for_port(PORT)
        pogo("setup", "--host", "127.0.0.1", "--port", str(PORT), "--type", "http", "--user", "ci")
        pogo("test")
        pogo("helper")
        pogo("on", "--yes")
        status = pogo("status")
        assert "all apps" in status, "pogo isn't on in all-apps mode"

        code = fetch("https://example.com/")
        print(f"\nfetched https://example.com/ without any proxy settings: HTTP {code}", flush=True)
        assert code == 200
        log = (HOME / "pogo.log").read_text(encoding="utf-8", errors="replace")
        captured = [line for line in log.splitlines() if "example.com" in line and "captured" in line]
        assert captured, "pogo's log doesn't show a captured connection to example.com"
        assert "PROXY" in captured[-1], captured[-1]
        upstream_log.flush()
        assert "CONNECT example.com:443" in (HOME / "upstream.log").read_text(encoding="utf-8"), \
            "the test proxy never saw the connection"

        pogo("off")
        time.sleep(2)
        status = pogo("status")
        assert "OFF" in status and "leftovers" not in status, "pogo didn't clean up"
        assert fetch("https://example.com/") == 200, "the network didn't come back after pogo off"
        print("\nEND-TO-END TEST PASSED", flush=True)
        return 0
    finally:
        pogo("off", check=False)
        if "leftovers" in pogo("status", check=False):
            pogo("reset", check=False)
        upstream.terminate()
        upstream_log.close()
        show("pogo.log", HOME / "pogo.log")
        show("tun.log", HOME / "tun.log")
        show("test proxy log", HOME / "upstream.log")


if __name__ == "__main__":
    sys.exit(main())
