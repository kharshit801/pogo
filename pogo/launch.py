"""Re-launching pogo itself: in the background, or with admin rights
(sudo on macOS, a UAC prompt on Windows)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import system

PKG_PARENT = str(Path(__file__).resolve().parent.parent)
BOOTSTRAP = f"import sys; sys.path.insert(0, {PKG_PARENT!r}); from pogo.cli import main; main()"


def self_command(*args: str, home: str | None = None, windowless: bool = False) -> list[str]:
    """argv that runs `pogo <args>` with this same Python, wherever pogo is installed.
    home: the config folder, passed explicitly because sudo and UAC start with a fresh
    environment (and UAC may even run as another account).
    windowless: on Windows, use pythonw.exe so no console window appears."""
    code = BOOTSTRAP
    if home is not None:
        code = f"import os; os.environ['POGO_HOME'] = {str(home)!r}; " + code
    executable = sys.executable
    if windowless and system.IS_WINDOWS:
        pythonw = Path(executable).with_name("pythonw.exe")
        if pythonw.exists():
            executable = str(pythonw)
    return [executable, "-c", code, *args]


def run_as_root(*args: str, home: str) -> int:
    """Run `pogo <args>` with admin rights and wait for it. macOS asks for the login password
    in this terminal (sudo); Windows shows a UAC prompt. Raises PermissionError if declined."""
    if system.IS_WINDOWS:
        cmd = self_command(*args, home=home, windowless=True)
        if system.is_admin():
            return subprocess.run(cmd, **system.hidden()).returncode
        handle = system.elevate(cmd)
        try:
            code = system.wait_process(handle)
            return 1 if code is None else code
        finally:
            system.close_handle(handle)
    cmd = self_command(*args, home=home)
    if os.geteuid() != 0:
        cmd = ["sudo", "-p", "  Mac login password (for admin rights): ", *cmd]
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        return 130
