# Changelog

All notable changes to pogo are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **`pogo setup-helper`**: an optional one-time setup, for computers you manage, so that
  afterwards `pogo on` routes every app with no password (macOS) and no UAC prompt (Windows).
  macOS installs a `visudo`-checked sudoers rule scoped to a root-owned wrapper; Windows creates
  a highest-privileges scheduled task triggered on demand. `pogo on` falls back to prompting when
  it isn't set up. Undo with `pogo setup-helper --remove`. It's a scoped privilege grant — see
  SECURITY.md.
- **Anonymous usage counting**, on by default, so the project can see how many people use pogo.
  It sends a random install id, the version, and your OS when you turn pogo on, and a heartbeat
  while it runs. Never your proxy, password, the sites you visit, or your IP. Turn it off with
  `pogo telemetry off`, `DO_NOT_TRACK=1`, or `POGO_NO_TELEMETRY=1`. See `docs/telemetry.md`.

## [0.3.0] - 2026-11-25

### Added
- **Windows support**, with the same commands and rules as on macOS:
  - all-apps mode with a Wintun adapter and `netsh` routes in the non-persistent store
    (a restart always clears them), started through a UAC prompt; `pogo off` needs none
  - basic mode through the Windows proxy settings (WinINet); previous settings are saved and
    restored, with a one-time sign-in task in case the PC restarts while pogo is on
  - passwords in Windows Credential Manager
  - `install.ps1`: one-line installer that needs no admin rights and no git
  - `pogo import` reads Proxifier for Windows profiles
  - `pogo env --shell powershell|cmd|sh`
- `pogo helper` downloads official tun2socks builds (and Wintun on Windows), verified against
  pinned checksums. Go is no longer needed on macOS.
- End-to-end test workflow that turns all-apps mode on for real on macOS and Windows runners.
- `pogo doctor` checks for Group Policy proxy locks and IPv6.
- `pogo uninstall` removes pogo completely. It turns pogo off first, then deletes its settings,
  logs, helpers, saved password and the program (on Windows, also its PATH entry), after
  listing them and asking. A folder you run pogo from, like a git clone, is never deleted.

### Changed
- New logo. Both installers and pogo's home screen show it too, drawn with block characters
  that every terminal font has.

### Fixed
- macOS: in all-apps mode, pogo's own connections (direct rules, and the hop to the proxy) could
  fail with "Network is unreachable". pogo now adds a default route scoped to the real interface.
- After a crash or a restart, the process IDs pogo saved can belong to other programs. pogo could
  then mistake one for its own, and `pogo reset` (running as admin) could stop it. A saved process
  ID now only counts if that process started before pogo saved it.
- Running the macOS installer again to update could fail with "Not possible to fast-forward" if
  pogo's history had been rewritten since the install. It now fetches the latest version and
  resets its own copy to it, and stops instead if you changed files in that copy.

## [0.2.0] - 2026-11-25

First public release.

### Added
- **All-apps mode.** `pogo on` captures every app's TCP traffic through a virtual network
  interface (`utun233` + tun2socks). It runs in the background, removes itself if pogo stops or
  the network changes, and `pogo off` needs no password.
- **Hostname sniffing.** Site rules work for apps that only connect by IP, using TLS SNI and the
  HTTP `Host` header.
- **`pogo import`** copies the proxy and rules from a Proxifier profile, and lists what it can't copy.
- **`pogo doctor`** diagnoses settings, password, proxy reachability, DNS, unsupported login
  methods (NTLM/Kerberos), leftovers and a running Proxifier.
- **`pogo reset`** removes every change pogo made, even after a crash.
- **`pogo logs -f`**: live, colored connection log showing the rule behind each decision.
- **A self-explaining CLI.** Plain `pogo` shows status and every command, `pogo help <command>`
  shows examples, and typos get a suggestion.
- **`pogo helper`** installs tun2socks, downloaded through your proxy.

## [0.1.0] - 2026-11-25

### Added
- Local proxy that speaks HTTP and SOCKS5 on one port and forwards through an upstream HTTP
  (`CONNECT`) or SOCKS5 proxy with username/password.
- Rules (proxy / direct / block) with domain, wildcard and CIDR patterns; first match wins;
  live reload.
- macOS system proxy on/off, `pogo env` and `pogo run` for terminal tools.
- Password storage in the macOS Keychain.

[0.3.0]: https://github.com/kharshit801/pogo/releases/tag/v0.3.0
[0.2.0]: https://github.com/kharshit801/pogo/releases/tag/v0.2.0
[0.1.0]: https://github.com/kharshit801/pogo/commits/main
