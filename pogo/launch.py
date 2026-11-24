"""Re-launching pogo itself: as a background process, or with admin rights via sudo."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_PKG_PARENT = str(Path(__file__).resolve().parent.parent)
BOOTSTRAP = f"import sys; sys.path.insert(0, {_PKG_PARENT!r}); from pogo.cli import main; main()"


def self_command(*args: str) -> list[str]:
    """argv that runs `pogo <args>` with this same Python, wherever pogo is installed."""
    return [sys.executable, "-c", BOOTSTRAP, *args]


def run_as_root(*args: str) -> int:
    """Run `pogo <args>` as root. sudo asks for the Mac login password in this terminal."""
    cmd = self_command(*args)
    if os.geteuid() != 0:
        cmd = ["sudo", "-p", "  Mac login password (for admin rights): ", *cmd]
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        return 130
