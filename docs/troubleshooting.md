# Troubleshooting

Start with:

```sh
pogo doctor
```

It checks your settings, password, network, DNS, the proxy login, the all-apps helper,
leftovers from earlier runs and a running Proxifier, and prints a fix next to every problem.

## Common problems

| Symptom | Cause | Fix |
|---|---|---|
| Web pages don't load after a crash or hard reboot | the system proxy still points at pogo, which isn't running | `pogo off` (or `pogo reset`) |
| "the proxy rejected your username or password (HTTP 407)" | wrong or expired password | `pogo setup` and type it again |
| "the proxy wants NTLM login" | your proxy needs NTLM or Kerberos | not supported yet; use `pogo on --basic` with a browser that does NTLM, and follow the roadmap |
| "no answer from the proxy" | not on the network that has the proxy, or wrong address | connect to the campus or company network; check with `pogo setup` |
| "port 8899 is already used" | another program listens there | change `"listen": {"port": …}` in `~/.config/pogo/config.json` |
| all-apps mode turned itself off | your network changed (another Wi-Fi, Ethernet unplugged) | run `pogo on` again |
| "Proxifier is running" | both tools capture traffic | let `pogo on` quit Proxifier, or quit it from its menu-bar icon |
| installing tun2socks fails | Go missing or too old (needs 1.22+), or no internet yet | `brew install go`; turn on basic mode first (`pogo on --basic`) so the download goes through your proxy, then `pogo helper` |
| a terminal tool fails in basic mode | it ignores the system proxy | `pogo run -- <command>`, or `eval "$(pogo env)"`, or switch to all-apps mode |
| "the network can't look up internet names" | your network only resolves names through the proxy | all-apps mode needs local DNS; use `pogo on --basic` |

## Windows

| Symptom | Fix |
|---|---|
| "pogo is not recognized" in a new window | close Windows Terminal completely and open it again (it caches PATH) |
| the permission prompt was declined | run `pogo on` again and choose Yes, or use `pogo on --basic` |
| tun2socks "disappeared right after it was saved" | your antivirus removed it: allow `%USERPROFILE%\.pogo\bin\tun2socks.exe`, then `pogo helper` |
| "Group Policy makes proxy settings machine-wide" | basic mode can't work on this PC; use all-apps mode (`pogo on`) |
| Ctrl-C asks "Terminate batch job (Y/N)?" | that's cmd.exe; answer Y |

More in [windows.md](windows.md).

## Logs

```sh
pogo logs -f                    # every connection: PROXY / DIRECT / BLOCK, and the rule that decided
cat ~/.config/pogo/tun.log      # all-apps capture: start-up steps, why it stopped
```

A connection log line reads:

```text
14:03:11 PROXY  github.com:443                               app · default
14:03:12 DIRECT 10.20.5.9:443                                captured · rule 10.0.0.0/8
14:03:15 BLOCK  www.youtube.com:443 [142.250.1.1]            captured · rule *.youtube.com
```

`app` means the app used the system proxy; `captured` means the virtual interface caught it.

## Undoing everything by hand

`pogo reset` does all of this for you. If pogo itself is broken:

```sh
# capture routes (they also disappear on reboot)
sudo route -n delete -net 0.0.0.0/1
sudo route -n delete -net 128.0.0.0/1
sudo pkill -f "tun2socks.*utun233"

# system proxy: turn it off on every network service that points at 127.0.0.1:8899
networksetup -listallnetworkservices
networksetup -setwebproxystate "Wi-Fi" off
networksetup -setsecurewebproxystate "Wi-Fi" off
networksetup -setsocksfirewallproxystate "Wi-Fi" off

rm -f ~/.config/pogo/tun.json ~/.config/pogo/tun.stop
```

Or in System Settings → Network → your connection → Details → Proxies, switch the proxies off.

## Reporting a bug

[Open an issue](https://github.com/kharshit801/pogo/issues/new/choose) with your macOS version, what you ran, what happened,
and the output of `pogo doctor`. Remove your proxy address from logs if you'd rather keep it private.
