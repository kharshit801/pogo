"""Render a real terminal session as an SVG "screenshot" for the docs.

Runs each command in a pseudo-terminal (so pogo prints its normal colors), then
converts the ANSI colors to SVG. The images in docs/assets are made with this, so
they always show genuine output. Block characters (the logo) become rectangles, so
they touch the way they do in a terminal instead of leaving gaps between lines.

    python3 scripts/termshot.py docs/assets/demo.svg "pogo" ["pogo rules" ...]
"""
from __future__ import annotations

import fcntl
import html
import os
import pty
import re
import shlex
import struct
import subprocess
import sys
import termios
from pathlib import Path

FONT = "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
SIZE, LINE, PAD, BAR = 14, 20, 22, 38
CHAR_W = 8.45  # advance width of a 14px monospace glyph
ASCENT = 15    # from a line's top to its text baseline
COLUMNS, ROWS = 100, 50  # the pseudo-terminal's window size
COLORS = {"31": "#FF7B72", "32": "#3FB950", "33": "#E3B341", "36": "#39C5CF"}
BLOCKS = {"█": (0, LINE), "▀": (0, LINE / 2), "▄": (LINE / 2, LINE / 2)}  # (top, height) in a line
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def capture(command: str, env: dict) -> str:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
    proc = subprocess.Popen(shlex.split(command), stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)
    chunks = []
    while True:
        try:
            data = os.read(master, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    proc.wait()
    os.close(master)
    return b"".join(chunks).decode().replace("\r\n", "\n").rstrip("\n")


def _fill(style: dict) -> str:
    if "color" in style:
        return style["color"]
    if style.get("bold"):
        return "#FFFFFF"
    return "#7D8590" if style.get("dim") else "#C9D1D9"


def spans(line: str) -> tuple[str, list[tuple[int, str, str]]]:
    """SVG markup for one line of output, and its block characters as (column, character,
    color). Those are left out of the text (as spaces) and drawn as rectangles."""
    out, blocks, style, pos, column = [], [], {}, 0, 0
    for m in list(SGR.finditer(line)) + [None]:
        text = line[pos:m.start()] if m else line[pos:]
        if text:
            blocks += [(column + i, char, _fill(style)) for i, char in enumerate(text) if char in BLOCKS]
            column += len(text)
            text = "".join(" " if char in BLOCKS else char for char in text)
            attrs = []
            if "color" in style:
                attrs.append(f'fill="{style["color"]}"')
            if style.get("bold"):
                attrs.append('font-weight="700" fill="#FFFFFF"' if "color" not in style else 'font-weight="700"')
            if style.get("dim"):
                attrs.append('fill="#7D8590"')
            out.append(f"<tspan {' '.join(attrs)}>{html.escape(text)}</tspan>" if attrs else html.escape(text))
        if m is None:
            break
        for code in (m.group(1) or "0").split(";"):
            if code in ("0", ""):
                style = {}
            elif code == "1":
                style["bold"] = True
            elif code == "2":
                style["dim"] = True
            elif code in COLORS:
                style["color"] = COLORS[code]
        pos = m.end()
    return "".join(out), blocks


def block_rects(blocks: list[tuple[int, int, str, str]]) -> str:
    """Rectangles for (row, column, character, color) cells, joining runs of the same kind."""
    runs: list[list] = []
    for row, column, char, fill in sorted(blocks):
        last = runs[-1] if runs else None
        if last and (last[0], last[2], last[3]) == (row, char, fill) and last[1] + last[4] == column:
            last[4] += 1
        else:
            runs.append([row, column, char, fill, 1])
    rects = []
    for row, column, char, fill, count in runs:
        top, height = BLOCKS[char]
        y = BAR + PAD + 10 + row * LINE - ASCENT + top
        rects.append(f'    <rect x="{PAD + column * CHAR_W:.2f}" y="{y:g}" width="{count * CHAR_W:.2f}" '
                     f'height="{height:g}" fill="{fill}"/>')
    return "\n".join(rects)


def render(sessions: list[tuple[str, str]], title: str) -> str:
    lines: list[str] = []
    blocks: list[tuple[int, int, str, str]] = []
    for i, (command, output) in enumerate(sessions):
        if i:
            lines.append("")
        lines.append(f'<tspan fill="#3FB950">$</tspan> <tspan fill="#FFFFFF">{html.escape(command)}</tspan>')
        for line in output.split("\n"):
            markup, cells = spans(line)
            blocks += [(len(lines), *cell) for cell in cells]
            lines.append(markup)
    plain = max((len(SGR.sub("", re.sub(r"<[^>]+>", "", line)).rstrip()) for line in lines), default=40)
    width = int(max(640, plain * CHAR_W + 2 * PAD))
    height = BAR + PAD + len(lines) * LINE + PAD - 6
    rows = "\n".join(f'    <text x="{PAD}" y="{BAR + PAD + 10 + i * LINE}" xml:space="preserve">{line}</text>'
                     for i, line in enumerate(lines))
    if blocks:
        rows = f'    <g shape-rendering="crispEdges">\n{block_rects(blocks)}\n    </g>\n{rows}'
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
  <title>{html.escape(title)}</title>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" fill="#0D1117" stroke="#30363D"/>
  <path d="M1 {BAR} H{width - 1}" stroke="#30363D"/>
  <circle cx="22" cy="19" r="6" fill="#FF5F57"/><circle cx="42" cy="19" r="6" fill="#FEBC2E"/><circle cx="62" cy="19" r="6" fill="#28C840"/>
  <text x="{width / 2}" y="24" text-anchor="middle" font-family="-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif" font-size="13" fill="#7D8590">{html.escape(title)}</text>
  <g font-family="{FONT}" font-size="{SIZE}" fill="#C9D1D9" style="white-space:pre">
{rows}
  </g>
</svg>
"""


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    target, commands = Path(sys.argv[1]), sys.argv[2:]
    repo = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
    env.update(TERM="xterm-256color", PATH=f"{repo / 'bin'}:{env.get('PATH', '')}")
    sessions = [(command, capture(command, env)) for command in commands]
    target.write_text(render(sessions, "pogo — zsh"))
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
