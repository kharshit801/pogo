<div align="center">

<img src="docs/assets/logo.svg" width="104" alt="pogo logo" />

# pogo

**Route every app on your Mac through your proxy, with one command.**

The open-source, command-line alternative to Proxifier, for campus and company networks<br/>
where the only way to the internet is an authenticated proxy.

<p>
  <a href="https://github.com/kharshit801/pogo/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/kharshit801/pogo/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Platform: macOS" src="https://img.shields.io/badge/platform-macOS-000000?logo=apple&logoColor=white">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white">
  <img alt="Zero dependencies" src="https://img.shields.io/badge/dependencies-0-22C55E">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-6366F1"></a>
</p>

<p>
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="docs/commands.md">Commands</a> ·
  <a href="docs/architecture.md">Architecture</a> ·
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
from your terminal. It's free, open source, has no dependencies, and is small enough to read in an
afternoon.

## Features

- **Every app, one command.** `pogo on` captures all TCP traffic through a virtual network interface. `pogo off` puts everything back.
- **Rules, like Proxifier.** Send sites and IP ranges through the proxy, straight out, or nowhere. First match wins, and changes apply instantly.
- **Site rules even for raw connections.** pogo reads the site name from the TLS handshake (SNI) or HTTP `Host` header, so `*.youtube.com` matches even when an app only connects to an IP address.
- **Imports your Proxifier setup.** `pogo import` copies the proxy and rules from your Proxifier profile.
- **Explains itself.** Type `pogo` for your status and every command, `pogo help <command>` for examples, and mistyped commands get a suggestion.
- **Safe by design.** Your password lives in the macOS Keychain. The capture removes itself if pogo stops or your network changes, and `pogo reset` undoes everything in one step.
- **A doctor built in.** `pogo doctor` spots wrong passwords, unreachable proxies, unsupported login methods (NTLM/Kerberos), leftovers and conflicts with a running Proxifier.

## Quick start

```sh
curl -fsSL https://raw.githubusercontent.com/kharshit801/pogo/main/install.sh | bash
```

<details>
<summary>Other ways to install</summary>

```sh
pipx install git+https://github.com/kharshit801/pogo        # with pipx
git clone https://github.com/kharshit801/pogo && make -C pogo install   # from source
```

pogo needs Python 3.9+, which comes with Apple's command line tools (`xcode-select --install`).
For all-apps mode, `pogo on` offers to build [tun2socks](https://github.com/xjasonlyu/tun2socks)
once with Go.

No internet until the proxy is set up? Point `curl` and `git` at it for the install:
`export https_proxy=http://USER:PASSWORD@PROXY:PORT`, then run the installer.
</details>

Then:

```sh
pogo setup      # proxy address, port, username, password   (or: pogo import, from Proxifier)
pogo test       # ✔ logged in and reached example.com:443 through the proxy
pogo on         # every app now goes through your proxy
pogo off        # back to normal
```

`pogo on` asks for your Mac password, because creating a network interface needs admin rights.
Want to skip that? `pogo on --basic` only sets the macOS system proxy (see [two modes](#two-modes)).

## How it works

<p align="center"><img src="docs/assets/how-it-works.svg" alt="Apps reach pogo through the system proxy or a virtual interface; pogo's rules send each connection through your proxy, direct, or block it" /></p>

1. **Apps that honor proxy settings** (browsers, most Mac apps) reach pogo through the macOS system proxy and pass the site name along.
2. **Everything else** (Docker, git, IDE helpers…) is caught by two routes that send all IPv4 traffic into a virtual interface, `utun233`. [tun2socks](https://github.com/xjasonlyu/tun2socks) hands each connection to pogo.
3. **pogo decides** with your rules, then forwards through your proxy (`CONNECT` or SOCKS5, with your login), straight out, or not at all. Its own outgoing sockets are pinned to your real network card, so nothing loops back into the tunnel.

This is what happens when an app that ignores proxy settings opens a connection:

```mermaid
sequenceDiagram
    autonumber
    participant App as Docker
    participant Tun as utun233 + tun2socks
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

More diagrams (startup sequence, loop prevention, the watchdog) are in **[docs/architecture.md](docs/architecture.md)**.

## Two modes

|  | **All apps**: `pogo on` | **Basic**: `pogo on --basic` |
|---|---|---|
| Covers | every app's TCP connections | apps that honor the system proxy |
| Docker, git, IDE helpers | ✅ | use `pogo run -- <cmd>` |
| Needs | your Mac password, tun2socks | nothing |
| How | virtual interface + system proxy | system proxy |

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
| Captures every app's TCP traffic | ✅ Network Extension | ✅ virtual interface + routes |
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

- **Your password stays in the macOS Keychain.** Never in a file, never in logs.
- **pogo only undoes its own changes.** Proxy settings that point anywhere else are left alone.
- **It can't strand you offline.** The capture removes itself if pogo stops, crashes or your network changes, and `pogo reset` cleans up even after `kill -9`.
- **Admin rights are only used to** create the interface, add and remove routes, and set the system proxy. See [SECURITY.md](SECURITY.md).
- **No telemetry.** pogo only makes the connections you route through it, plus a test connection to `example.com` through your proxy when you set up, test, turn on, or run the doctor.

## FAQ

<details>
<summary><b>Which proxies does it work with?</b></summary>

HTTP proxies that support `CONNECT` (Squid and most campus and company proxies), and SOCKS5,
with or without a username and password (Basic auth). NTLM and Kerberos logins aren't supported
yet; `pogo test` tells you if your proxy asks for one of them.
</details>

<details>
<summary><b>Why does <code>pogo on</code> need my Mac password?</b></summary>

Creating a virtual network interface and changing routes requires root. pogo uses sudo once per
`pogo on`; turning it off doesn't need the password. `pogo on --basic` never needs it.
</details>

<details>
<summary><b>What about UDP, QUIC and video calls?</b></summary>

pogo carries TCP, like Proxifier. Browsers fall back from QUIC to TCP automatically, and video
apps fall back to TCP when UDP isn't available. DNS stays on your network.
</details>

<details>
<summary><b>Can I run it alongside Proxifier?</b></summary>

No. Proxifier grabs connections before pogo sees them. `pogo on` offers to quit Proxifier for you.
</details>

<details>
<summary><b>What if something goes wrong?</b></summary>

Run `pogo doctor`. If your connection seems stuck, `pogo reset` removes every change pogo made.
Routes never survive a reboot either. More in [docs/troubleshooting.md](docs/troubleshooting.md).
</details>

## Roadmap

- [ ] Per-app rules (match the app that opened a connection)
- [ ] NTLM and Kerberos proxy logins
- [ ] IPv6 capture
- [ ] Homebrew formula
- [ ] Start at login (launchd)
- [ ] Linux support

Ideas and votes welcome in [issues](https://github.com/kharshit801/pogo/issues).

## Status

pogo is **beta**. CI tests the proxy engine, rules, import, the CLI and the capture plumbing on
every push. All-apps mode changes real network routes, which CI can't do, so if it misbehaves on
your network, please [open an issue](https://github.com/kharshit801/pogo/issues/new/choose) with the
output of `pogo doctor`.

## Contributing

Pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md): setup takes one
command and the tests run in seconds.

## License

[MIT](LICENSE) © pogo contributors

<sub>pogo is an independent project and is not affiliated with or endorsed by the makers of
Proxifier. Proxifier is a trademark of its respective owner.</sub>
