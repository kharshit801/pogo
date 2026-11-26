# Security policy

pogo handles your proxy password and, in all-apps mode, runs a small part of itself as root.
We take both seriously.

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Use GitHub's private reporting
instead: **Security → Report a vulnerability** on this repository. You'll get a reply within a
week, and credit in the release notes if you'd like it.

## Supported versions

Security fixes go into the latest release on `main`.

## What pogo does with admin rights

`pogo on` (all-apps mode) asks for admin rights once: your password through `sudo` on macOS,
a UAC prompt on Windows. The admin process only:

- starts [tun2socks](https://github.com/xjasonlyu/tun2socks), which creates the virtual interface
  (`utun233` on macOS, a Wintun adapter called `pogo` on Windows)
- configures that interface (`ifconfig` on macOS, `netsh` on Windows)
- adds and removes the capture routes and the exceptions it needs (`route` on macOS; `netsh` in the
  non-persistent store on Windows)
- on macOS, points the system proxy at pogo and switches that off again
- writes its state into pogo's folder and removes it on exit

`pogo on --basic`, `pogo off` and every other command run without admin rights. On Windows,
basic mode writes your user's proxy settings and a one-time RunOnce entry (to restore them after
a restart); neither needs admin rights.

## Hardening already in place

- **Password storage.** Your proxy password is kept in the macOS Keychain or Windows Credential
  Manager, never in a file or log.
- **Verified downloads.** tun2socks and Wintun are checked against pinned SHA-256 checksums before
  anything is written.
- **Local only.** The local proxy listens on `127.0.0.1` by default, so other machines can't use it.
- **Root writes are symlink-safe.** Files the root process writes in your config folder are
  created with a fresh temporary file plus an atomic rename, or `O_NOFOLLOW`.
- **Untrusted input is validated.** The state file is writable by your user, so cleanup only
  accepts well-formed `route delete` commands from it.
- **No shell.** Every external command is run with an argument list, never through a shell.
- **Minimal changes, fully undone.** pogo only removes proxy settings that point at itself, and a
  watchdog removes the capture if pogo stops, crashes or your network changes.
- **Telemetry is a count, never your data.** Anonymous usage counting (on by default) sends a
  random install id, the version and your OS. It never sends your proxy, password, the sites you
  visit, or your IP; geo lookup is off and the IP is overwritten before storage. Turn it off with
  `pogo telemetry off` or `DO_NOT_TRACK=1`. See [docs/telemetry.md](docs/telemetry.md).

## `pogo setup-helper` (no-prompt capture)

`pogo setup-helper` is an **optional** one-time setup, for computers you manage (a lab or shared
PC), so that afterwards `pogo on` captures every app without a password or UAC prompt. It asks for
admin rights once to install:

- **macOS:** a root-owned wrapper at `/usr/local/libexec/pogo-tun` and a sudoers rule in
  `/etc/sudoers.d/pogo` that lets your account run **only that wrapper** with `sudo`, no password.
  The rule is checked with `visudo` before it's written.
- **Windows:** a Scheduled Task (`pogo-capture`) that runs the capture with the highest
  privileges, using your interactive token (no stored password). `pogo on` triggers it.

This is a deliberate, scoped privilege grant. The trade-off: **anyone who can modify pogo's files
on that machine could then run code as root/administrator without a password.** So:

- Only run it on machines you trust and control.
- Install pogo somewhere ordinary users can't write (for a multi-user PC, a root-owned location
  rather than a per-user folder).
- Undo it any time with `pogo setup-helper --remove`.

Without `setup-helper`, `pogo on` elevates per run (password/UAC each time), which is the default.

## Known limitations

- Like any tool you install as your user and then run with `sudo`, anything that can modify
  pogo's files as your user could change what runs as root. Install pogo somewhere only you can
  write (the default), and keep your account secure.
- tun2socks and Wintun are third-party binaries (official builds, verified by checksum). Wintun
  is signed by WireGuard LLC; tun2socks isn't code-signed, so antivirus may flag it.
- If you change `listen.host` to something other than `127.0.0.1`, anyone who can reach that
  address can use your proxy login. Don't.
