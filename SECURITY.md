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

`pogo on` (all-apps mode) asks for your Mac password once, through `sudo`, and starts a root
process that only:

- starts [tun2socks](https://github.com/xjasonlyu/tun2socks), which creates the virtual interface `utun233`
- runs `ifconfig` to configure that interface
- runs `route add` / `route delete` for the capture routes and the exceptions it needs
- runs `networksetup` to point the system proxy at pogo, and to switch that off again
- writes its state into `~/.config/pogo/` and removes it on exit

`pogo on --basic`, `pogo off` and every other command run without admin rights.

## Hardening already in place

- **Password storage.** Your proxy password is kept in the macOS Keychain, never in a file or log.
- **Local only.** The local proxy listens on `127.0.0.1` by default, so other machines can't use it.
- **Root writes are symlink-safe.** Files the root process writes in your config folder are
  created with a fresh temporary file plus an atomic rename, or `O_NOFOLLOW`.
- **Untrusted input is validated.** The state file is writable by your user, so cleanup only
  accepts well-formed `route delete` commands from it.
- **No shell.** Every external command is run with an argument list, never through a shell.
- **Minimal changes, fully undone.** pogo only removes proxy settings that point at itself, and a
  watchdog removes the capture if pogo stops, crashes or your network changes.

## Known limitations

- Like any tool you install as your user and then run with `sudo`, anything that can modify
  pogo's files as your user could change what runs as root. Install pogo somewhere only you can
  write (the default), and keep your account secure.
- tun2socks is a third-party binary, built from source with `go install`. Review it if that
  matters in your environment.
- If you change `listen.host` to something other than `127.0.0.1`, anyone who can reach that
  address can use your proxy login. Don't.
