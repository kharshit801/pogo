"""Terminal presentation: colors, status symbols and prompts.
Plain text when output isn't a terminal or NO_COLOR is set."""
from __future__ import annotations

import os
import sys

COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def _style(code: str):
    def apply(text) -> str:
        return f"\033[{code}m{text}\033[0m" if COLOR else str(text)
    return apply


bold, dim, red, green, yellow, cyan = (_style(c) for c in ("1", "2", "31", "32", "33", "36"))

_ACTION_STYLE = {"proxy": cyan, "direct": green, "block": red}


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
    return sys.stdin.isatty()


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
