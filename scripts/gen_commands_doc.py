"""Regenerate docs/commands.md from pogo's own help text, so the reference never drifts.

    python3 scripts/gen_commands_doc.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["COLUMNS"] = "100"
os.environ["NO_COLOR"] = "1"

from pogo import cli  # noqa: E402

ORDER = [
    ("Everyday", ["on", "off", "status", "logs"]),
    ("Set up", ["import", "setup", "test"]),
    ("Rules", ["rules", "rules add", "rules remove", "rules check", "rules default"]),
    ("Terminal tools", ["run", "env"]),
    ("When something's wrong", ["doctor", "reset"]),
    ("Advanced", ["start", "stop", "system", "tun", "helper", "setup-helper", "telemetry", "uninstall"]),
]


def help_text(name: str) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.PARSERS[name].print_help()
    return buf.getvalue().rstrip()


def main() -> None:
    cli.build_parser()
    out = ["# Command reference", "",
           "> Generated from pogo's own help by `scripts/gen_commands_doc.py` (`make docs`).",
           "> Run `pogo` on its own for an overview, or `pogo help <command>` in your terminal.", ""]
    for section, names in ORDER:
        links = " · ".join(f"[`{n}`](#pogo-{n.replace(' ', '-')})" for n in names)
        out.append(f"- **{section}:** {links}")
    out.append("")
    for section, names in ORDER:
        out += [f"## {section}", ""]
        for name in names:
            out += [f"### `pogo {name}`", "", "```text", help_text(name), "```", ""]
    (REPO / "docs" / "commands.md").write_text("\n".join(out))
    print("wrote docs/commands.md")


if __name__ == "__main__":
    main()
