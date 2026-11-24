<div align="center">

<img src="docs/assets/logo.svg" width="104" alt="pogo logo" />

# pogo

**Route every app on your Mac or PC through your proxy, with one command.**

The open-source, command-line alternative to Proxifier, for campus and company networks<br/>
where the only way to the internet is an authenticated proxy.

<p>
  <a href="https://github.com/kharshit801/pogo/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/kharshit801/pogo/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/kharshit801/pogo/actions/workflows/e2e.yml"><img alt="End-to-end" src="https://github.com/kharshit801/pogo/actions/workflows/e2e.yml/badge.svg"></a>
  <img alt="macOS and Windows" src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows-000000">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white">
  <img alt="Zero dependencies" src="https://img.shields.io/badge/dependencies-0-22C55E">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-6366F1"></a>
</p>

<p>
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="docs/commands.md">Commands</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
  <a href="docs/windows.md">Windows</a> ·
  <a href="docs/comparison.md">vs. Proxifier</a> ·
  <a href="docs/troubleshooting.md">Troubleshooting</a>
</p>

<img src="docs/assets/demo.svg" width="760" alt="Typing pogo on its own shows its status and every command" />

</div>

## Why pogo

Some networks only let you out through an HTTP proxy that wants a username and password.
Browsers cope. Everything else struggles: Docker pulls hang, `git` can't reach GitHub, IDE
extensions sit offline, and every command-line tool wants its own proxy flags.

[Proxifier](https://www.proxifier.com) fixes this with a paid GUI app. **pogo** does the same job
from your terminal, on macOS and Windows. It's free, open source, has no dependencies, and is
small enough to read in an afternoon.

## Features

- **Every app, one command.** `pogo on` captures all TCP traffic through a virtual network interface. `pogo off` puts everything back.
- **Rules, like Proxifier.** Send sites and IP ranges through the proxy, straight out, or nowhere. First match wins, and changes apply instantly.
- **Site rules even for raw connections.** pogo reads the site name from the TLS handshake (SNI) or HTTP `Host` header, so `*.youtube.com` matches even when an app only connects to an IP address.
- **Imports your Proxifier setup.** `pogo import` copies the proxy and rules from a Proxifier profile on macOS or Windows.
- **Explains itself.** Type `pogo` for your status and every command, `pogo help <command>` for examples, and mistyped commands get a suggestion.
- **Safe by design.** Your password lives in the macOS Keychain or Windows Credential Manager. The capture removes itself if pogo stops or your network changes, a restart can never leave your network broken, and `pogo reset` undoes everything in one step.
- **A doctor built in.** `pogo doctor` spots wrong passwords, unreachable proxies, unsupported login methods (NTLM/Kerberos), Group Policy locks, leftovers and conflicts with a running Proxifier.

## Quick start

**macOS** (Terminal):

```sh
curl -fsSL https://raw.githubusercontent.com/kharshit801/pogo/main/install.sh | bash
```

**Windows** (PowerShell, no admin rights needed):

```powershell
irm https://raw.githubusercontent.com/kharshit801/pogo/main/install.ps1 | iex
```

<details>
<summary>Other ways to install, and what you need</summary>

```sh
pipx install git+https://github.com/kharshit801/pogo        # with pipx, on either system
git clone https://github.com/kharshit801/pogo && make -C pogo install   # from source (macOS)
```

pogo needs Python 3.9+. On macOS it comes with Apple's command line tools
(`xcode-select --install`). On Windows the installer finds your Python, or offers to install
it with winget. The first `pogo on` offers to download [tun2socks](https://github.com/xjasonlyu/tun2socks)
(and on Windows the [Wintun](https://www.wintun.net) driver DLL): official builds, checked
against known checksums.

No internet until the proxy is set up? Install while connected some other way (a phone
hotspot, or with Proxifier still running), or on macOS point the installer at your proxy:
`export https_proxy=http://USER:PASSWORD@PROXY:PORT`.
</details>

Then, in a new terminal:

```sh
pogo setup      # proxy address, port, username, password   (or: pogo import, from Proxifier)
pogo test       # ✔ logged in and reached example.com:443 through the proxy
pogo on         # every app now goes through your proxy
pogo off        # back to normal
```

`pogo on` needs admin rights to create a network interface: macOS asks for your password,
Windows shows a permission prompt. Want to skip that? `pogo on --basic` only sets the system
proxy (see [two modes](#two-modes)).

## How it works

<p align="center"><img src="docs/assets/how-it-works.svg" alt="Apps reach pogo through the system proxy or a virtual interface; pogo's rules send each connection through your proxy, direct, or block it" /></p>

1. **Apps that honor proxy settings** (browsers, most apps) reach pogo through the system proxy and pass the site name along.
2. **Everything else** (Docker, git, IDE helpers…) is caught by two routes that send all IPv4 traffic into a virtual interface: `utun233` on macOS, a Wintun adapter called `pogo` on Windows. [tun2socks](https://github.com/xjasonlyu/tun2socks) hands each connection to pogo.
3. **pogo decides** with your rules, then forwards through your proxy (`CONNECT` or SOCKS5, with your login), straight out, or not at all. Its own outgoing sockets are pinned to your real network card, so nothing loops back into the tunnel.

This is what happens when an app that ignores proxy settings opens a connection:

```mermaid
sequenceDiagram
    autonumber
    participant App as Docker
    participant Tun as Virtual interface + tun2socks
    participant Pogo as pogo
    participant Proxy as Your proxy
    App->>Tun: connect to 54.x.x.x:443
    Tun->>Pogo: SOCKS5 CONNECT 54.x.x.x:443
    Pogo->>Pogo: read the TLS hello → registry-1.docker.io
    Pogo->>Pogo: check rules → proxy
    Pogo->>Proxy: CONNECT registry-1.docker.io:443 with your login
    Proxy-->>Pogo: 200 Connection established
    Note over App,Proxy: bytes flow both ways, still end-to-end encrypted
```

More diagrams (startup, loop prevention, the watchdog, Windows specifics) are in
**[docs/architecture.md](docs/architecture.md)** and **[docs/windows.md](docs/windows.md)**.

## Two modes

|  | **All apps**: `pogo on` | **Basic**: `pogo on --basic` |
|---|---|---|
| Covers | every app's TCP connections | apps that honor the system proxy |
| Docker, git, IDE helpers | ✅ | use `pogo run -- <cmd>` |
| Needs | admin rights (password / UAC prompt), tun2socks | nothing |
| How | virtual interface (+ system proxy on macOS) | system proxy |

## Rules

<p align="center"><img src="docs/assets/rules.svg" width="720" alt="Adding a block rule for YouTube, checking it, and listing all rules" /></p>

Rules are checked top to bottom and the first match wins. Anything unmatched uses the default
(`pogo rules default proxy`). Out of the box, localhost and private network ranges go direct.

| Pattern | Matches |
|---|---|
| `example.com` | the site and all its subdomains |
| `*.example.com` | the same, written Proxifier-style |
| `10.0.0.0/8` | an IP range (a single IP works too) |
| `192.168.*` | wildcards |
| `*` | everything |

## Commands

| Everyday | | Set up | |
|---|---|---|---|
| `pogo` | status + every command | `pogo import` | copy settings from Proxifier |
| `pogo on` | capture every app | `pogo setup` | enter proxy details |
| `pogo off` | back to normal | `pogo test` | check proxy + password |
| `pogo status` | what's running | `pogo rules …` | list / add / remove / check |
| `pogo logs -f` | watch connections live | `pogo doctor` | diagnose problems |
| `pogo run -- git pull` | one command via the proxy | `pogo reset` | emergency undo |

Full reference with every option: **[docs/commands.md](docs/commands.md)**.

## pogo vs. Proxifier

|  | Proxifier | pogo |
|---|---|---|
| Captures every app's TCP traffic | ✅ | ✅ virtual interface + routes |
| macOS and Windows | ✅ | ✅ |
| Rules by site / IP | ✅ | ✅ |
| Rules by app | ✅ | ❌ (on the [roadmap](#roadmap)) |
| Proxy login | Basic, NTLM, Kerberos | Basic, SOCKS5 user/pass |
| UDP / QUIC | TCP only | TCP only (QUIC falls back to TCP) |
| IPv6 | ✅ | ❌ |
| Interface | GUI | CLI |
| Price | paid | free, MIT |

How Proxifier works under the hood, and why pogo takes a different route:
**[docs/comparison.md](docs/comparison.md)**.

## Safety and privacy

- **Your password stays in the macOS Keychain or Windows Credential Manager.** Never in a file, never in logs.
- **pogo only undoes its own changes.** Proxy settings that point anywhere else are left alone; on Windows your previous proxy settings are saved and put back.
- **It can't strand you offline.** The capture removes itself if pogo stops, crashes or your network changes. Routes live in memory only, so a restart clears them, and on Windows a one-time sign-in task restores the proxy settings if the PC restarted while pogo was on. `pogo reset` cleans up even after a crash.
- **Admin rights are only used to** create the interface and change routes (and the system proxy on macOS). See [SECURITY.md](SECURITY.md).
- **No telemetry.** pogo only makes the connections you route through it, plus a test connection to `example.com` through your proxy when you set up, test, turn on, or run the doctor.

## FAQ

<details>
<summary><b>Which proxies does it work with?</b></summary>

HTTP proxies that support `CONNECT` (Squid and most campus and company proxies), and SOCKS5,
with or without a username and password (Basic auth). NTLM and Kerberos logins aren't supported
yet; `pogo test` tells you if your proxy asks for one of them.
</details>

<details>
<summary><b>Why does <code>pogo on</code> ask for my password, or show a Windows permission prompt?</b></summary>

Creating a virtual network interface and changing routes requires admin rights. pogo asks once
per `pogo on`; turning it off doesn't need them. `pogo on --basic` never does.
</details>

<details>
<summary><b>Does it work on Windows the same way?</b></summary>

Yes: same commands, same rules. Windows uses the Wintun driver (from the WireGuard project)
for the virtual interface. Details, including what to expect from antivirus and Group Policy, in
[docs/windows.md](docs/windows.md).
</details>

<details>
<summary><b>What about UDP, QUIC and video calls?</b></summary>

pogo carries TCP, like Proxifier. Browsers fall back from QUIC to TCP automatically, and video
apps fall back to TCP when UDP isn't available. DNS stays on your network.
</details>

<details>
<summary><b>Can I run it alongside Proxifier?</b></summary>

No. Proxifier grabs connections before pogo sees them. `pogo on` offers to quit Proxifier on
macOS and asks you to close it on Windows.
</details>

<details>
<summary><b>What if something goes wrong?</b></summary>

Run `pogo doctor`. If your connection seems stuck, `pogo reset` removes every change pogo made.
More in [docs/troubleshooting.md](docs/troubleshooting.md).
</details>

## Uninstall

```sh
pogo off
rm -rf ~/.local/share/pogo ~/.config/pogo ~/.local/bin/pogo                   # macOS
```

```powershell
pogo off
Remove-Item -Recurse -Force "$env:USERPROFILE\.pogo"                          # Windows
```

On Windows, also remove `%USERPROFILE%\.pogo\bin` from your user PATH, and the `pogo:` entry in
Credential Manager if you like.

## Roadmap

- [ ] Per-app rules (match the app that opened a connection)
- [ ] NTLM and Kerberos proxy logins
- [ ] IPv6 capture
- [ ] Homebrew formula and winget package
- [ ] Start at login
- [ ] Linux support

Ideas and votes welcome in [issues](https://github.com/kharshit801/pogo/issues).

## Status

pogo is **beta**. On every push, CI runs the tests on macOS and Windows with three Python
versions, and the [end-to-end workflow](https://github.com/kharshit801/pogo/actions/workflows/e2e.yml)
turns all-apps mode on for real on both systems, sends traffic through a test proxy, and checks
that `pogo off` cleans up. Networks differ, so if it misbehaves on yours, please
[open an issue](https://github.com/kharshit801/pogo/issues/new/choose) with the output of
`pogo doctor`.

## Contributing

Pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md): setup takes one
command and the tests run in seconds.

## License

[MIT](LICENSE) © pogo contributors

<sub>pogo is an independent project and is not affiliated with or endorsed by the makers of
Proxifier. Proxifier is a trademark of its respective owner.</sub>
