#!/usr/bin/env bash
# pogo installer: https://github.com/kharshit801/pogo
#
#   curl -fsSL https://raw.githubusercontent.com/kharshit801/pogo/main/install.sh | bash
#
# Installs into ~/.local/share/pogo and links ~/.local/bin/pogo. Run it again to update.
set -euo pipefail

REPO="https://github.com/kharshit801/pogo.git"
DEST="${POGO_INSTALL_DIR:-$HOME/.local/share/pogo}"
BIN_DIR="${POGO_BIN_DIR:-$HOME/.local/bin}"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# pogo's logo, the same art as on pogo's home screen (made by scripts/logo_blocks.py)
printf '\n\033[1m'
cat <<'LOGO'
 ▀█████████████████████▄
  ▀█████████████████████
    ▀▀██████████████████
              ▄████████
             ▄████████
            ███████▀▀
  ▄████████
 ▄████████
████████▀
LOGO
printf '\033[0m\n'

[ "$(uname -s)" = "Darwin" ] ||
  fail "this installer is for macOS. On Windows, run this in PowerShell: irm https://raw.githubusercontent.com/kharshit801/pogo/main/install.ps1 | iex"
command -v git >/dev/null 2>&1 || fail "git is missing. Install Apple's command line tools: xcode-select --install"
command -v python3 >/dev/null 2>&1 || fail "python3 is missing. Install Apple's command line tools: xcode-select --install"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail "pogo needs Python 3.9 or newer."

if [ -d "$DEST/.git" ]; then
  say "Updating pogo in $DEST"
  [ -z "$(git -C "$DEST" status --porcelain)" ] ||
    fail "$DEST has files you changed. Move them somewhere else, then run this again."
  # Fetch and reset instead of pull: this folder is the installer's own copy, and updating must
  # work even if the project's history was rewritten since it was installed.
  git -C "$DEST" fetch --quiet --depth 1 origin main
  git -C "$DEST" reset --quiet --hard FETCH_HEAD
else
  say "Installing pogo into $DEST"
  mkdir -p "$(dirname "$DEST")"
  git clone --depth 1 --quiet "$REPO" "$DEST"
fi

mkdir -p "$BIN_DIR"
ln -sf "$DEST/bin/pogo" "$BIN_DIR/pogo"
say "✔ installed $BIN_DIR/pogo"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo ""
    echo "Add $BIN_DIR to your PATH, then open a new terminal:"
    echo "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc"
    ;;
esac
echo ""
echo "Next: run 'pogo' to see what it can do."
