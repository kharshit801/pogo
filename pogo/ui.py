"""Terminal presentation: colors, status symbols and prompts.
Plain text when output isn't a terminal or NO_COLOR is set."""
from __future__ import annotations

import os
import sys


def _windows_vt() -> bool:
    """Windows 10+ consoles draw ANSI colors once asked to."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = ctypes.c_void_p(kernel32.GetStdHandle(-11))  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def _color_enabled() -> bool:
    out = sys.stdout
    if out is None or not hasattr(out, "isatty") or not out.isatty():
        return False
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    return sys.platform != "win32" or _windows_vt()


COLOR = _color_enabled()


def _style(code: str):
    def apply(text) -> str:
        return f"\033[{code}m{text}\033[0m" if COLOR else str(text)
    return apply


bold, dim, red, green, yellow, cyan = (_style(c) for c in ("1", "2", "31", "32", "33", "36"))

_ACTION_STYLE = {"proxy": cyan, "direct": green, "block": red}

# The classic Windows console font can't draw these; Windows Terminal and VS Code can.
_LEGACY_SYMBOLS = str.maketrans({"✔": "√", "✖": "×", "◐": "o", "●": "*", "○": "o", "↳": "->", "←": "<-"})


class _LegacyConsole:
    def __init__(self, stream):
        self._stream = stream

    def write(self, text: str) -> int:
        return self._stream.write(text.translate(_LEGACY_SYMBOLS))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def setup_console() -> None:
    """Make output safe to print everywhere: no crash on symbols a console can't encode, and
    look-alike symbols on the classic Windows console."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:  # pythonw.exe (the Windows admin helper) has no console at all
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
        if (sys.platform == "win32" and stream.isatty()
                and not (os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"))):
            setattr(sys, name, _LegacyConsole(stream))


def action(name: str) -> str:
    return _ACTION_STYLE.get(name, str)(name)


def ok(msg: str) -> None:
    print(f"  {green('✔')} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  {yellow('!')} {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  {red('✖')} {msg}", flush=True)


def info(msg: str) -> None:
    print(f"  {dim('•')} {msg}", flush=True)


def hint(msg: str) -> None:
    print(f"    {dim(msg)}", flush=True)


def error(msg: str) -> None:
    print(f"{red('error:')} {msg}", file=sys.stderr, flush=True)


def interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def is_terminal() -> bool:
    """Is the output going to a terminal (rather than a pipe or a file)?"""
    out = sys.stdout
    return out is not None and hasattr(out, "isatty") and out.isatty()


def ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        return input(f"  {label}{suffix}: ").strip() or default
    except EOFError:
        return default


def confirm(question: str, default: bool = True, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not interactive():
        return default
    choices = "Y/n" if default else "y/N"
    try:
        answer = input(f"  {question} [{choices}] ").strip().lower()
    except EOFError:
        return default
    return default if not answer else answer in ("y", "yes")
