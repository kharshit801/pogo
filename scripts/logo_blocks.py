"""Draw pogo's logo with terminal block characters, from docs/assets/logo.png.

Only ▀ ▄ █ are used, because every console font has them (the classic Windows console's
too). Each character shows two square pixels, one above the other, so the art keeps the
logo's proportions.

    python3 scripts/logo_blocks.py        # the size pogo uses
    python3 scripts/logo_blocks.py 40     # another size

The same art is in pogo/logo.py (the home screen), install.sh and install.ps1 (the
installers). tests/test_logo.py checks that all three still match the PNG.
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

LOGO_PNG = Path(__file__).resolve().parent.parent / "docs" / "assets" / "logo.png"
COLUMNS = 28  # wide enough to keep the shape, small enough to sit beside text in 80 columns


def read_alpha(path: Path = LOGO_PNG) -> tuple[int, int, list[float]]:
    """Width, height and opacity (0 to 1) of every pixel of an 8-bit RGBA PNG."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} isn't a PNG")
    pos, header, idat = 8, None, []
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", data[pos + 8:pos + 21])
        elif kind == b"IDAT":
            idat.append(data[pos + 8:pos + 8 + length])
        pos += 12 + length
    width, height, depth, color, _, _, interlace = header
    if (depth, color, interlace) != (8, 6, 0):
        raise ValueError(f"{path} must be an 8-bit RGBA PNG without interlacing")
    raw, stride = zlib.decompress(b"".join(idat)), width * 4
    alpha, previous = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, row = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):  # undo the PNG row filter
            left = row[i - 4] if i >= 4 else 0
            up, up_left = previous[i], previous[i - 4] if i >= 4 else 0
            if kind == 1:
                row[i] = (row[i] + left) & 0xFF
            elif kind == 2:
                row[i] = (row[i] + up) & 0xFF
            elif kind == 3:
                row[i] = (row[i] + (left + up) // 2) & 0xFF
            elif kind == 4:
                guess = left + up - up_left
                nearest = min((abs(guess - left), 0, left), (abs(guess - up), 1, up),
                              (abs(guess - up_left), 2, up_left))[2]
                row[i] = (row[i] + nearest) & 0xFF
        alpha += [row[x * 4 + 3] / 255 for x in range(width)]
        previous = row
    return width, height, alpha


def _coverage(alpha: list[float], width: int, height: int, x0: float, y0: float, x1: float, y1: float) -> float:
    """Average opacity of the box [x0, x1) x [y0, y1), counting partly covered pixels in part."""
    total = area = 0.0
    for y in range(int(y0), min(height, math.ceil(y1))):
        fy = min(y + 1, y1) - max(y, y0)
        for x in range(int(x0), min(width, math.ceil(x1))):
            fx = min(x + 1, x1) - max(x, x0)
            total += alpha[y * width + x] * fx * fy
            area += fx * fy
    return total / area if area else 0.0


def render(columns: int, path: Path = LOGO_PNG) -> list[str]:
    """The logo, `columns` characters wide: one string per line, without trailing spaces."""
    width, height, alpha = read_alpha(path)
    size = width / columns  # a character is one pixel wide and two tall: each pixel is size x size
    pixel_rows = round(height / size)
    pixel_rows += pixel_rows % 2
    on = [[_coverage(alpha, width, height, i * size, j * size, (i + 1) * size, (j + 1) * size) >= 0.5
           for i in range(columns)] for j in range(pixel_rows)]
    lines = ["".join(" ▄▀█"[top << 1 | bottom] for top, bottom in zip(on[r], on[r + 1])).rstrip()
             for r in range(0, pixel_rows, 2)]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    indent = min(len(line) - len(line.lstrip()) for line in lines if line)
    return [line[indent:] for line in lines]


def main() -> None:
    for columns in [int(arg) for arg in sys.argv[1:]] or [COLUMNS]:
        print(f"{columns} columns:")
        print("\n".join(render(columns)), end="\n\n")


if __name__ == "__main__":
    main()
