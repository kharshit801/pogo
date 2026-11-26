r"""`pogo uninstall`: remove pogo and everything it set up.

What an installation is made of:

    macOS                                         Windows
    ~/.config/pogo       settings, logs, helper   %USERPROFILE%\.pogo       all of it: settings, logs,
    ~/.local/share/pogo  the program (install.sh)                           helpers, the program (app)
    ~/.local/bin/pogo    the pogo command (link)                            and the pogo command (bin)
    Keychain item        the proxy password       Credential Manager entry  the proxy password
                                                  user PATH entry           %USERPROFILE%\.pogo\bin

cli.cmd_uninstall turns pogo off (undoing its network changes) before anything here runs, and
removes the password. A copy of pogo that the installers didn't put there (a git clone, a pip
install) is never deleted: only the command that points to it.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config, system
from .launch import PKG_PARENT

PATH_ENTRY = r"%USERPROFILE%\.pogo\bin"  # what install.ps1 adds to the user's PATH

# Everything pogo writes into its settings folder. If that folder isn't pogo's alone (POGO_HOME
# pointing at a shared folder), only these go.
OWN_FILES = ("config.json", "config.tmp", "pogo.pid", "pogo.log", "pogo.log.1", "tun.json", "tun.stop",
             "tun.log", "tun.log.1", "tun.progress", "winproxy-backup.json", "restore-proxy.py",
             "telemetry.json")
OWN_HELPERS = ("tun2socks", "tun2socks.exe", "wintun.dll")


@dataclass
class Locations:
    home: Path       # settings, logs and downloaded helpers (config.HOME_DIR)
    running: Path    # the copy of pogo that is running now
    installed: Path  # where the installer puts the program
    command: Path    # the `pogo` command the installer creates: a link (macOS) or pogo.cmd (Windows)


def locations() -> Locations:
    user = config.user_home()
    if system.IS_WINDOWS:
        root = user / ".pogo"  # install.ps1 always installs here
        return Locations(config.HOME_DIR, Path(PKG_PARENT), root / "app", root / "bin" / "pogo.cmd")
    installed = Path(os.environ.get("POGO_INSTALL_DIR") or user / ".local" / "share" / "pogo")
    command = Path(os.environ.get("POGO_BIN_DIR") or user / ".local" / "bin") / "pogo"
    return Locations(config.HOME_DIR, Path(PKG_PARENT), installed, command)


@dataclass
class Plan:
    delete: list[tuple[Path, str]] = field(default_factory=list)  # (path, what it is), with all inside
    tidy: list[Path] = field(default_factory=list)  # shared folders: only pogo's own files go
    keep: list[str] = field(default_factory=list)   # what's left alone, and why
    later: Path | None = None  # Windows: holds pogo.cmd, which is running, so it goes after pogo exits


def is_pogo(folder: Path) -> bool:
    """Does this folder hold a copy of pogo's program?"""
    return (folder / "pogo" / "cli.py").is_file() and (folder / "bin" / "pogo").is_file()


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (OSError, ValueError):
        return False


def show(path: Path) -> str:
    """A path as people write it: ~/... on macOS."""
    if not system.IS_WINDOWS:
        try:
            return "~/" + path.relative_to(config.user_home()).as_posix()
        except ValueError:
            pass
    return str(path)


def plan(where: Locations, windows: bool | None = None) -> Plan:
    """What to remove. `windows` picks the layout (for tests); it defaults to this computer's."""
    p = Plan()
    user = config.user_home()
    if system.IS_WINDOWS if windows is None else windows:
        root = where.command.parent.parent
        if root.is_dir() and (is_pogo(where.installed) or where.command.exists()):
            p.delete.append((root, "the program, its settings, logs and helpers"))
            if where.command.exists():
                p.later = root
    else:
        if where.command.is_symlink():
            target = where.command.resolve()
            if target.name == "pogo" and is_pogo(target.parent.parent):
                p.delete.append((where.command, "the pogo command"))
            else:
                p.keep.append(f"{show(where.command)} isn't a link to pogo, so it stays")
        if is_pogo(where.installed):
            p.delete.append((where.installed, "the program"))
    home = where.home
    if home.is_dir() and not any(_inside(home, path) for path, _ in p.delete):
        if "pogo" in home.name.lower() and not _inside(user, home):
            p.delete.append((home, "settings, logs and downloaded helpers"))
        else:
            p.tidy.append(home)
    running = where.running
    if not any(_inside(running, path) for path, _ in p.delete):
        if "site-packages" in running.parts:
            p.keep.append("pogo is also installed as a Python package; remove that with: "
                          "pipx uninstall pogo-cli   (or: pip uninstall pogo-cli)")
        else:
            p.keep.append(f"the source folder {show(running)} stays: pogo runs from it, "
                          "but the installer didn't put it there")
    return p


# --- removing ------------------------------------------------------------------------------

def _writable_and_retry(func, target, _error) -> None:
    os.chmod(target, stat.S_IWRITE)  # Windows refuses to delete read-only files
    func(target)


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=_writable_and_retry)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def _empty_except(folder: Path, spare: Path) -> None:
    """Delete everything in folder except `spare` (and the folders that lead to it)."""
    for child in folder.iterdir():
        if child == spare:
            continue
        if child.is_dir() and not child.is_symlink():
            if _inside(spare, child):
                _empty_except(child, spare)
            else:
                _rmtree(child)
        else:
            _unlink(child)


def delete_later(folder: Path) -> None:
    """Windows: delete `folder` two seconds from now. The pogo command is a batch file, and
    cmd.exe reads it again when pogo returns, so it can't be deleted while pogo runs."""
    subprocess.Popen(f'cmd.exe /d /s /c "ping -n 3 127.0.0.1 >nul & rd /s /q "{folder}""',
                     cwd=os.environ.get("SystemRoot") or None, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=system.CREATE_NO_WINDOW | system.CREATE_NEW_PROCESS_GROUP)


def remove(p: Plan, report: Callable[[str, str], None]) -> int:
    """Carry out the plan. Returns how many things couldn't be removed."""
    failed = 0
    for path, _ in p.delete:
        try:
            if p.later is not None and path == p.later:
                _empty_except(path, Path(path, "bin", "pogo.cmd"))
                delete_later(path)
                report("ok", f"removed {show(path)} (the pogo command itself goes once this finishes)")
                continue
            if path.is_symlink() or path.is_file():
                _unlink(path)
            elif path.exists():
                _rmtree(path)
            report("ok", f"removed {show(path)}")
        except OSError as e:
            failed += 1
            report("fail", f"couldn't remove {show(path)}: {e.strerror or e}")
    for folder in p.tidy:
        names = [folder / name for name in OWN_FILES] + [folder / "bin" / name for name in OWN_HELPERS]
        for path in names:
            try:
                if path.exists():
                    _unlink(path)
            except OSError as e:
                failed += 1
                report("fail", f"couldn't remove {show(path)}: {e.strerror or e}")
        for leftover in (folder / "bin", folder):
            try:
                leftover.rmdir()  # only if pogo's files were all it held
            except OSError:
                pass
        report("ok", f"removed pogo's files from {show(folder)}")
    return failed


# --- Windows: the user PATH entry that install.ps1 added -----------------------------------------

def _normalize(entry: str) -> str:
    return entry.strip().strip('"').rstrip("\\/").casefold()


def without_pogo(path_value: str, user_home: str) -> str:
    """A PATH value without the folder install.ps1 added (written either way)."""
    ours = {_normalize(PATH_ENTRY), _normalize(user_home + r"\.pogo\bin")}
    return ";".join(entry for entry in path_value.split(";") if _normalize(entry) not in ours)


def remove_path_entry(subkey: str = "Environment") -> bool:
    """Take pogo's folder out of the user's PATH. True if it was there."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            value, kind = winreg.QueryValueEx(key, "Path")  # unexpanded: %USERPROFILE% stays as is
            if not isinstance(value, str):
                return False
            new = without_pogo(value, str(config.user_home()))
            if new == value:
                return False
            winreg.SetValueEx(key, "Path", 0, kind, new)  # same type: REG_EXPAND_SZ stays expandable
    except OSError:
        return False
    if subkey == "Environment":
        _announce_environment_change()
    return True


def _announce_environment_change() -> None:
    """Tell Explorer and new terminals that the user's environment changed."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                                           wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = ctypes.c_size_t
    result = ctypes.c_size_t()
    # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG, at most 5 seconds
    user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
