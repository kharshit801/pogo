# Contributing to pogo

Thanks for helping! pogo is small on purpose, and the whole codebase can be read in an afternoon.

## Setup

```sh
git clone https://github.com/kharshit801/pogo && cd pogo
make test        # ~40 tests, under 10 seconds, no network or admin rights needed
make install     # links ./bin/pogo into ~/.local/bin, so `pogo` runs your working copy
```

There's nothing to `pip install`: pogo uses only the Python standard library (3.9+).

## Ground rules

- **No dependencies.** Standard library only. tun2socks is the single optional external helper.
- **Python 3.9 compatible.** macOS may run pogo with an older `python3` under `sudo`.
- **Explain yourself to the user.** Every message should say what happened and what to do next.
  New commands need a description and examples in `build_parser()` (that's what `pogo help` shows).
- **Never leave the network broken.** Anything that changes system state must be undone by
  `pogo off`, by the watchdog, and by `pogo reset`.
- **Tests must not touch the real system.** No real routes, system proxy or Keychain writes in
  tests. Use fakes, a temporary `POGO_HOME` and `POGO_PASSWORD`, as the existing tests do.

## Project layout

See [docs/architecture.md](docs/architecture.md#source-layout) for what each file does.

## Testing all-apps mode

All-apps mode changes real routes. CI tests it for real in the
[E2E workflow](.github/workflows/e2e.yml) on macOS and Windows runners (`tests/e2e/run_e2e.py`:
a fake authenticating proxy, `pogo on`, a request with every proxy setting disabled, `pogo off`).
To try your changes by hand:

```sh
sudo pogo tun --dry-run   # print every route and command without running them (Windows: pogo tun --dry-run)
pogo on                   # the real thing
pogo logs -f              # watch decisions
pogo off                  # or, if anything is stuck: pogo reset
```

Routes never survive a restart, so a restart is always a clean slate.

## Windows

Windows-specific code lives in `pogo/system.py` (processes, UAC), `pogo/winnet.py` (IP Helper
API), `pogo/winproxy.py` (WinINet proxy settings), `pogo/wincred.py` (Credential Manager) and the
`WinNet` backend in `pogo/tun.py`. Pure logic (bypass lists, netsh commands, checksums, the
progress relay) is unit-tested on every platform; `tests/test_windows.py` also has tests that run
only on Windows, which CI runs on GitHub's Windows machines.

## Docs and screenshots

- `docs/commands.md` is generated from the CLI's own help: `make docs`.
- The terminal images in `docs/assets` are rendered from real output: `make screenshots`.
- The logo is `docs/assets/logo.png`, plus `logo-white.png` for dark backgrounds. If it changes,
  redraw its terminal version with `python3 scripts/logo_blocks.py` and paste the result into
  `pogo/logo.py`, `install.sh` and `install.ps1` (`make test` checks all three). Then re-render
  `social-preview.png` with `rsvg-convert docs/assets/social-preview.svg -o docs/assets/social-preview.png`.
- The README header (`logo-lockup.svg`, `logo-lockup-dark.svg`), the social preview and the How
  it works diagram share one vector copy of the mark, traced from `logo.png`. The wordmark is
  built the same way as the mark: 30° lean, x-height and descender on the mark's bands, a sweep
  on the leading corner. Keep the look minimal: the logo's ink (#333) and grays, thin lines, no
  gradients, with a light and a dark version wherever GitHub's theme can change.

## Pull requests

1. Open an issue first for anything bigger than a bug fix, so we can agree on the approach.
2. Keep PRs focused, with tests for new behavior.
3. Run `make test` and `make docs` before pushing.
4. Describe what you changed and how you tested it. The PR template will prompt you.

By contributing you agree that your work is released under the [MIT license](LICENSE), and to
follow the [code of conduct](CODE_OF_CONDUCT.md).
