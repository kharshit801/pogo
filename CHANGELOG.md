# Changelog

All notable changes to pogo are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

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

[0.2.0]: https://github.com/kharshit801/pogo/releases/tag/v0.2.0
[0.1.0]: https://github.com/kharshit801/pogo/commits/main
