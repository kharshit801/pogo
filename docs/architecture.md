# Architecture

pogo is about 2,700 lines of dependency-free Python plus one optional open-source helper,
[tun2socks](https://github.com/xjasonlyu/tun2socks). This page explains how the pieces fit
together and why they're built that way.

- [Components](#components)
- [What `pogo on` does](#what-pogo-on-does)
- [Life of a connection](#life-of-a-connection)
- [How pogo avoids routing loops](#how-pogo-avoids-routing-loops)
- [The watchdog: how pogo cleans up](#the-watchdog-how-pogo-cleans-up)
- [Processes and files](#processes-and-files)
- [Source layout](#source-layout)

## Components

```mermaid
flowchart LR
    subgraph apps["Your apps"]
        A1["Browsers, Slack, most Mac apps<br/>honor proxy settings"]
        A2["Docker, git, IDE helpers, CLIs<br/>ignore proxy settings"]
    end
    subgraph mac["macOS"]
        SP["System proxy settings"]
        RT["Routes 0/1 and 128/1"]
        TUN["utun233<br/>virtual interface"]
    end
    T2S["tun2socks<br/>runs as root"]
    subgraph pogo["pogo local proxy, 127.0.0.1:8899"]
        IN["HTTP + SOCKS5 listener"]
        SN["Hostname sniffing<br/>TLS SNI or HTTP Host"]
        RU["Rules engine<br/>first match wins"]
        UP["Upstream client<br/>CONNECT or SOCKS5 + login"]
    end
    KC[("macOS Keychain")]
    PX["Your proxy"]
    NET(("Internet"))
    LAN["Campus network or LAN"]
    BLK["Refused"]

    A1 --> SP --> IN
    A2 --> RT --> TUN --> T2S -->|SOCKS5| IN
    IN --> SN --> RU
    RU -->|proxy| UP --> PX --> NET
    RU -->|direct| LAN
    RU -->|block| BLK
    KC -. password .-> UP
```

| Piece | Runs as | Job |
|---|---|---|
| **Local proxy** (`pogo/server.py`) | you | One port that speaks both HTTP-proxy and SOCKS5. Applies rules, logs in to your proxy, pipes bytes. |
| **Capture worker** (`pogo/tun.py`) | root | Starts tun2socks, installs and removes routes, sets the system proxy, watches for reasons to stop. |
| **tun2socks** | root | Turns raw packets arriving on `utun233` into SOCKS5 connections to the local proxy. |
| **CLI** (`pogo/cli.py`) | you | Everything you type. Starts and stops the other two and explains what's going on. |

## What `pogo on` does

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant CLI as pogo CLI
    participant LP as Local proxy
    participant W as Capture worker (root)
    participant OS as macOS
    You->>CLI: pogo on
    CLI->>CLI: log in to your proxy once (CONNECT example.com:443)
    CLI->>LP: start in the background on 127.0.0.1:8899
    CLI->>W: sudo pogo tun --background (asks your Mac password)
    W->>OS: write state file, so the local proxy pins its sockets to the real NIC
    W->>OS: start tun2socks, creating utun233
    W->>OS: host routes so the proxy server and DNS stay direct
    W->>OS: routes 0/1 and 128/1 into utun233
    W->>OS: system proxy set to 127.0.0.1:8899
    W-->>CLI: progress lines, then ready
    CLI-->>You: pogo is ON
```

The root part is split in two. The sudo'd process launches the long-running worker, relays its
progress to your terminal through a pipe, and exits as soon as the worker reports `ready`. So
your terminal is free again, and `pogo off` later doesn't need your password: it just creates a
stop file that the worker watches.

## Life of a connection

An app that ignores proxy settings, in all-apps mode:

```mermaid
sequenceDiagram
    participant App as App
    participant Tun as utun233 + tun2socks
    participant P as pogo
    participant X as Your proxy
    participant S as Server
    App->>Tun: TCP connect to 54.x.x.x:443
    Tun->>P: SOCKS5 CONNECT 54.x.x.x:443
    P-->>Tun: accepted
    Tun->>P: first bytes, a TLS ClientHello
    P->>P: SNI says registry-1.docker.io
    P->>P: rules say proxy
    P->>X: CONNECT registry-1.docker.io:443 with Proxy-Authorization
    X-->>P: 200 Connection established
    P->>X: replay the ClientHello
    X->>S: connect to the real server
    Note over App,S: pogo never decrypts anything, TLS stays end to end
```

Why accept first and decide later? In all-apps mode pogo only sees an IP address. To apply site
rules (and to send your proxy a hostname, which many proxies require for filtering) pogo
accepts the connection, waits up to 0.3 s for the first bytes, and reads the hostname from the
TLS Server Name Indication or the HTTP `Host` header. Protocols where the server speaks first,
like SSH, simply fall back to the IP address after that short wait.

Apps that use the system proxy skip all of this: they send `CONNECT hostname:443` straight to
pogo.

## How pogo avoids routing loops

Capturing everything has a classic trap: the proxy's *own* connections would be captured too,
and loop forever. tun2socks' own documentation warns about exactly this when its proxy runs locally.

```mermaid
flowchart TB
    S["pogo opens an outgoing socket"] --> Q{"Is all-apps capture running?"}
    Q -- no --> N["normal routing"]
    Q -- yes --> L{"Destination is localhost?"}
    L -- yes --> N
    L -- no --> PIN["set IP_BOUND_IF to the real network card"]
    PIN --> OUT["leaves through Wi-Fi or Ethernet,<br/>never through utun233"]
```

On top of that, host routes keep the proxy server and your DNS servers on the real network, and
your own subnet already has a more specific route than `0/1` and `128/1`, so local traffic is
never captured.

On macOS a pinned socket only uses routes *scoped* to its interface, so pogo also adds a default
route scoped to the real interface (`route add -ifscope en0 default <gateway>`) while the capture
runs; without it, pinned connections would fail with "Network is unreachable". On Windows the
equivalent option, `IP_UNICAST_IF`, uses the adapter's own default route, so nothing extra is
needed. How the Windows side works in detail: [windows.md](windows.md).

## The watchdog: how pogo cleans up

The capture worker checks once a second whether it should stop, and always removes what it
added: routes first, so traffic stops entering the tunnel, then the system proxy, then
tun2socks, and the state file last.

```mermaid
stateDiagram-v2
    [*] --> Starting: pogo on
    Starting --> Active: ready
    Starting --> Removing: setup failed
    Active --> Removing: pogo off (stop file)
    Active --> Removing: local proxy stopped or crashed
    Active --> Removing: network changed
    Active --> Removing: tun2socks exited
    Active --> Removing: SIGTERM or pogo reset
    Removing --> [*]: routes, system proxy, tun2socks, state file removed
```

If even the worker is killed with `kill -9`, `pogo reset` reads the list of routes the worker
recorded and removes them. Only well-formed `route delete` commands are accepted from that file.

## Processes and files

Paths are shown for macOS; on Windows the folder is `%USERPROFILE%\.pogo`.

| Path | Written by | Contents |
|---|---|---|
| `~/.config/pogo/config.json` | you, via the CLI | proxy address, port, username, rules |
| macOS Keychain item `pogo` / Credential Manager `pogo:…` | you, via the CLI | your proxy password |
| `~/.config/pogo/pogo.pid` | local proxy | its process ID |
| `~/.config/pogo/pogo.log` | local proxy | one line per connection (`pogo logs`) |
| `~/.config/pogo/tun.json` | capture worker | interface, gateway, routes it added |
| `~/.config/pogo/tun.stop` | `pogo off` | asks the worker to stop |
| `~/.config/pogo/tun.log` | capture worker | capture start/stop log |

Files the root worker writes in your (user-writable) config folder use a fresh temporary file
plus an atomic rename, or `O_NOFOLLOW`, so a planted symlink can't redirect a root write.

## Source layout

| File | Purpose |
|---|---|
| `pogo/cli.py` | commands, help texts, the overview screen, `doctor`, `reset` |
| `pogo/server.py` | the local proxy: HTTP + SOCKS5 in, CONNECT / SOCKS5 out, sniffing, piping |
| `pogo/tun.py` | all-apps capture: routes, tun2socks, watchdog, cleanup |
| `pogo/rules.py` | pattern matching and rule decisions |
| `pogo/sysproxy.py` | macOS system proxy via `networksetup` and `scutil` |
| `pogo/config.py` | config file, runtime state, Keychain |
| `pogo/proxifier_import.py` | reads Proxifier `.ppx` profiles |
| `pogo/launch.py` | relaunching pogo in the background, or with admin rights (sudo / UAC) |
| `pogo/system.py` | processes, admin rights and files: what differs between macOS and Windows |
| `pogo/helper.py` | downloads and verifies tun2socks (and Wintun on Windows) |
| `pogo/winnet.py` | Windows routing table, adapters and DNS servers (IP Helper API) |
| `pogo/winproxy.py` | Windows proxy settings (WinINet), backup and restart safety net |
| `pogo/wincred.py` | Windows Credential Manager |
| `pogo/ui.py` | colors, symbols, prompts |
