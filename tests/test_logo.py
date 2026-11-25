"""The logo in the terminal: the art on pogo's home screen and in both installers must match
docs/assets/logo.png, and the PNG reader that draws it must handle any PNG row filter."""
import importlib.util
import os
import re
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pogo import logo  # noqa: E402

_spec = importlib.util.spec_from_file_location("logo_blocks", REPO / "scripts" / "logo_blocks.py")
logo_blocks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(logo_blocks)

POWERSHELL_BLOCKS = {"#": "█", "^": "▀", "_": "▄"}  # install.ps1 stays ASCII


class LogoArtTests(unittest.TestCase):
    def setUp(self):
        self.art = logo_blocks.render(logo_blocks.COLUMNS)

    def test_home_screen_art_matches_the_png(self):
        self.assertEqual(logo.LOGO, self.art)

    def test_macos_installer_shows_the_same_logo(self):
        script = (REPO / "install.sh").read_text(encoding="utf-8")
        self.assertEqual(re.search(r"<<'LOGO'\n(.*?)\nLOGO\n", script, re.S).group(1).split("\n"), self.art)

    def test_windows_installer_shows_the_same_logo_and_stays_ascii(self):
        script = (REPO / "install.ps1").read_bytes().decode("ascii")
        block = re.search(r"\$logo = @\((.*?)\n\s*\)", script, re.S).group(1)
        rows = re.findall(r"^\s*'([ #^_]*)',?\s*$", block, re.M)
        self.assertEqual(["".join(POWERSHELL_BLOCKS.get(c, c) for c in row) for row in rows], self.art)


def _filtered(kind: int, row: bytes, previous: bytes) -> bytes:
    """Apply PNG row filter `kind` (the encoder's side of what read_alpha undoes)."""
    out = bytearray([kind])
    for i, value in enumerate(row):
        left, up = row[i - 4] if i >= 4 else 0, previous[i]
        up_left = previous[i - 4] if i >= 4 else 0
        guess = left + up - up_left
        predictor = [0, left, up, (left + up) // 2,
                     min((abs(guess - left), 0, left), (abs(guess - up), 1, up),
                         (abs(guess - up_left), 2, up_left))[2]][kind]
        out.append((value - predictor) & 0xFF)
    return bytes(out)


class PngReaderTests(unittest.TestCase):
    def test_every_row_filter_is_undone(self):
        width, height = 3, 5  # one row per filter type: none, sub, up, average, paeth
        rows = [bytes((x * 37 + y * 91 + c * 53) % 256 for x in range(width) for c in range(4)) for y in range(height)]
        previous, data = bytes(width * 4), b""
        for kind, row in enumerate(rows):
            data += _filtered(kind, row, previous)
            previous = row

        def chunk(kind: bytes, body: bytes) -> bytes:
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b""))
        fd, name = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(png)
        self.addCleanup(os.unlink, name)
        w, h, alpha = logo_blocks.read_alpha(Path(name))
        self.assertEqual((w, h), (width, height))
        self.assertEqual(alpha, [row[x * 4 + 3] / 255 for row in rows for x in range(width)])


if __name__ == "__main__":
    unittest.main()
