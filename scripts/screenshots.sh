#!/usr/bin/env bash
# Regenerate the terminal images in docs/assets from real pogo output.
set -euo pipefail
cd "$(dirname "$0")/.."

POGO_HOME="$(mktemp -d)" python3 scripts/termshot.py docs/assets/demo.svg "pogo"

export POGO_HOME="$(mktemp -d)"
bin/pogo setup --host proxy.example.edu --port 3128 --type http --user alice >/dev/null
python3 scripts/termshot.py docs/assets/rules.svg \
  'pogo rules add "*.youtube.com" block --at 1' "pogo rules check m.youtube.com" "pogo rules"
