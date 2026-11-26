# Command reference

> Generated from pogo's own help by `scripts/gen_commands_doc.py` (`make docs`).
> Run `pogo` on its own for an overview, or `pogo help <command>` in your terminal.

- **Everyday:** [`on`](#pogo-on) · [`off`](#pogo-off) · [`status`](#pogo-status) · [`logs`](#pogo-logs)
- **Set up:** [`import`](#pogo-import) · [`setup`](#pogo-setup) · [`test`](#pogo-test)
- **Rules:** [`rules`](#pogo-rules) · [`rules add`](#pogo-rules-add) · [`rules remove`](#pogo-rules-remove) · [`rules check`](#pogo-rules-check) · [`rules default`](#pogo-rules-default)
- **Terminal tools:** [`run`](#pogo-run) · [`env`](#pogo-env)
- **When something's wrong:** [`doctor`](#pogo-doctor) · [`reset`](#pogo-reset)
- **Advanced:** [`start`](#pogo-start) · [`stop`](#pogo-stop) · [`system`](#pogo-system) · [`tun`](#pogo-tun) · [`helper`](#pogo-helper) · [`setup-helper`](#pogo-setup-helper) · [`telemetry`](#pogo-telemetry) · [`uninstall`](#pogo-uninstall)

## Everyday

### `pogo on`

```text
usage: pogo on [-h] [--basic] [-y] [--force]

Turn pogo on.

By default it captures ALL apps, like Proxifier: a virtual network interface hands
every app's connections to pogo, which applies your rules and logs in to your proxy.
That needs admin rights, so macOS asks for your login password.

With --basic it only sets the macOS system proxy. No admin rights needed, and it covers
browsers and most apps, but not apps that ignore proxy settings (Docker, IDE helpers,
command-line tools).

options:
  -h, --help  show this help message and exit
  --basic     only set the system proxy (no admin password)
  -y, --yes   don't ask questions, assume yes
  --force     turn on even if the proxy login check fails

examples:
  pogo on             capture every app
  pogo on --basic     browsers and most apps, no admin password
```

### `pogo off`

```text
usage: pogo off [-h]

Turn pogo off: removes the all-apps capture, switches the system proxy off and stops
the local proxy. Your Mac goes back to normal networking. No password needed.

options:
  -h, --help  show this help message and exit

examples:
  pogo off
```

### `pogo status`

```text
usage: pogo status [-h]

Show whether pogo is on, which proxy it uses and what it captures.

options:
  -h, --help  show this help message and exit

examples:
  pogo status
```

### `pogo logs`

```text
usage: pogo logs [-h] [-f] [-n N]

Show the connections pogo handled: where each one went (PROXY, DIRECT or BLOCK),
whether it came from an app's proxy settings or from the all-apps capture, and which
rule decided.

options:
  -h, --help     show this help message and exit
  -f, --follow   keep watching for new connections
  -n, --lines N  how many recent lines to show (default 50)

examples:
  pogo logs           the last 50 connections
  pogo logs -f        watch live (Ctrl-C to stop)
  pogo logs -n 200    the last 200
```

## Set up

### `pogo import`

```text
usage: pogo import [-h] [--list] [-y] [profile]

Copy the proxy and rules from a Proxifier profile (Proxifier's active one unless you
name another). Site and IP rules are copied; per-app rules can't be, because pogo
sees connections, not which app opened them. Proxifier keeps your password encrypted,
so you type it once.

positional arguments:
  profile     profile name or path to a .ppx file

options:
  -h, --help  show this help message and exit
  --list      list Proxifier profiles
  -y, --yes   import without asking

examples:
  pogo import                    the profile active in Proxifier
  pogo import --list             see all profiles
  pogo import campus             a profile by name
  pogo import ~/Desktop/x.ppx    an exported profile file
```

### `pogo setup`

```text
usage: pogo setup [-h] [--host HOST] [--port PORT] [--type TYPE] [--user USER] [--password-stdin]

Enter your proxy's address, port, username and password, step by step.
Press Enter to keep a value. The password goes into the macOS Keychain,
never into a file.
If Proxifier is installed, setup offers to copy its settings instead.

options:
  -h, --help        show this help message and exit
  --host HOST       proxy IP or hostname
  --port PORT       proxy port, e.g. 3128 or 8080
  --type TYPE       http (most campus proxies) or socks5
  --user USER       username (empty string for none)
  --password-stdin  read the password from standard input

examples:
  pogo setup
  pogo setup --host proxy.example.edu --port 3128 --user alice
```

### `pogo test`

```text
usage: pogo test [-h] [--port PORT] [site]

Check that the proxy answers and accepts your username and password, by opening a
connection to a site through it.

positional arguments:
  site         site to reach (default example.com)

options:
  -h, --help   show this help message and exit
  --port PORT  port on that site (default 443)

examples:
  pogo test
  pogo test github.com
```

## Rules

### `pogo rules`

```text
usage: pogo rules [-h] {list,add,remove,check,default} ...

Rules decide where each connection goes:
  proxy    through your campus proxy
  direct   straight out, without the proxy (campus sites, local network)
  block    refused
They're checked top to bottom and the first match wins; anything that matches no rule
uses the default. Changes apply right away, no restart needed.

Patterns:
  example.com      that site and all its subdomains
  *.example.com    the same, written Proxifier-style
  10.0.0.0/8       an IP range (a single IP works too)
  192.168.*        wildcards
  *                everything

positional arguments:
  {list,add,remove,check,default}
    list                show all rules
    add                 add a rule
    remove (rm)         remove a rule
    check (test)        which rule a site hits
    default             what happens when no rule matches

options:
  -h, --help            show this help message and exit

examples:
  pogo rules
  pogo rules add "*.youtube.com" block
  pogo rules add intranet.college.edu direct --at 1
  pogo rules remove 3
  pogo rules check github.com
  pogo rules default proxy
```

### `pogo rules add`

```text
usage: pogo rules add [-h] [--at N] pattern {proxy,direct,block}

Add a rule. A rule for the same site is replaced. New rules go to the end unless you
give --at (1 = checked first).

positional arguments:
  pattern               site, IP, range or wildcard
  {proxy,direct,block}  where matching connections go

options:
  -h, --help            show this help message and exit
  --at N                position (1 = checked first)

examples:
  pogo rules add "*.youtube.com" block
  pogo rules add 10.20.0.0/16 direct --at 1
```

### `pogo rules remove`

```text
usage: pogo rules remove [-h] target

Remove a rule by its number (see: pogo rules) or its pattern.

positional arguments:
  target      rule number or pattern

options:
  -h, --help  show this help message and exit

examples:
  pogo rules remove 3
  pogo rules remove "*.youtube.com"
```

### `pogo rules check`

```text
usage: pogo rules check [-h] site

Show which rule decides a site, and what happens to it.

positional arguments:
  site

options:
  -h, --help  show this help message and exit

examples:
  pogo rules check github.com
```

### `pogo rules default`

```text
usage: pogo rules default [-h] {proxy,direct,block}

Set what happens to connections that match no rule.

positional arguments:
  {proxy,direct,block}

options:
  -h, --help            show this help message and exit

examples:
  pogo rules default proxy
```

## Terminal tools

### `pogo run`

```text
usage: pogo run [-h] ...

Run one command with proxy settings in its environment. Handy in basic mode for terminal
tools that ignore the system proxy (git, brew, pip, npm, curl). In all-apps mode you
don't need it.

positional arguments:
  command     the command to run

options:
  -h, --help  show this help message and exit

examples:
  pogo run -- git pull
  pogo run -- brew update
```

### `pogo env`

```text
usage: pogo env [-h] [--unset] [--shell {cmd,powershell,sh}]

Print proxy environment variables so terminal tools in this shell use pogo.

options:
  -h, --help            show this help message and exit
  --unset               print commands that remove the variables
  --shell {cmd,powershell,sh}
                        syntax to print (default: powershell on Windows, sh elsewhere)

examples:
  eval "$(pogo env)"          use the proxy in this shell
  eval "$(pogo env --unset)"  stop using it
```

## When something's wrong

### `pogo doctor`

```text
usage: pogo doctor [-h]

Check everything that commonly goes wrong (settings, password, proxy reachability, DNS,
the all-apps helper, leftovers, Proxifier running at the same time) and say how to fix it.

options:
  -h, --help  show this help message and exit

examples:
  pogo doctor
```

### `pogo reset`

```text
usage: pogo reset [-h]

Emergency undo: stop pogo and remove every network change it made (routes, virtual
interface, system proxy), even after a crash. May ask for admin rights.

options:
  -h, --help  show this help message and exit

examples:
  pogo reset
```

## Advanced

### `pogo start`

```text
usage: pogo start [-h] [-d] [--system]

Start only the local proxy, without capturing anything. It runs in this terminal unless
you give -d. Most people want 'pogo on' instead.

options:
  -h, --help    show this help message and exit
  -d, --daemon  run in the background
  --system      also set the system proxy while running

examples:
  pogo start -d
  pogo start --system
```

### `pogo stop`

```text
usage: pogo stop [-h]

Stop the local proxy (and the all-apps capture with it). 'pogo off' does this and more.

options:
  -h, --help  show this help message and exit

examples:
  pogo stop
```

### `pogo system`

```text
usage: pogo system [-h] {on,off}

Point the macOS system proxy at pogo, or switch that off. 'off' only removes
settings that point at pogo.

positional arguments:
  {on,off}

options:
  -h, --help  show this help message and exit

examples:
  pogo system on
  pogo system off
```

### `pogo tun`

```text
usage: pogo tun [-h] [--dry-run] [{start,cleanup}]

Run the all-apps capture in this terminal until Ctrl-C (needs admin rights). 'pogo on'
runs it in the background for you; this is for debugging.

positional arguments:
  {start,cleanup}

options:
  -h, --help       show this help message and exit
  --dry-run        print the commands without running them

examples:
  sudo pogo tun
  sudo pogo tun --dry-run    show what it would do
  sudo pogo tun cleanup     remove leftovers
```

### `pogo helper`

```text
usage: pogo helper [-h] [--reinstall]

Install the open-source helper all-apps mode uses: tun2socks, plus the Wintun driver DLL
on Windows. Official builds, checked against known checksums, downloaded through your
proxy when pogo is running.

options:
  -h, --help   show this help message and exit
  --reinstall  install again even if it's present

examples:
  pogo helper
```

### `pogo setup-helper`

```text
usage: pogo setup-helper [-h] [--remove]

One-time setup so 'pogo on' captures every app without a password. It asks for admin
rights once, now, and installs a small helper (a sudoers rule and a root-owned wrapper).
Undo it with: pogo setup-helper --remove

Only run this on computers you trust. Afterwards, anyone who can change pogo's files
on this machine could run code with admin rights without a password. See SECURITY.md.

options:
  -h, --help  show this help message and exit
  --remove    undo the setup

examples:
  pogo setup-helper
  pogo setup-helper --remove
```

### `pogo telemetry`

```text
usage: pogo telemetry [-h] [{on,off,status}]

Anonymous usage counting: it's on by default and sends a random id, the pogo version and
your OS so the author can see how many people use pogo. Your proxy, password, the sites
you visit and your IP are never sent. 'pogo telemetry' alone shows the current state.
You can also turn it off with DO_NOT_TRACK=1 or POGO_NO_TELEMETRY=1.

positional arguments:
  {on,off,status}

options:
  -h, --help       show this help message and exit

examples:
  pogo telemetry          show what's sent and whether it's on
  pogo telemetry off      stop counting this install
  pogo telemetry on       start again
```

### `pogo uninstall`

```text
usage: pogo uninstall [-h] [-y]

Remove pogo from this computer. It turns pogo off and undoes its network changes first,
then deletes its settings, logs, downloaded helpers, the password saved in Keychain
and the program with its pogo command.

It shows what it will remove and asks first. Nothing is removed if pogo can't turn off
cleanly. A copy of pogo you run from a folder of your own (a git clone) is never deleted.

options:
  -h, --help  show this help message and exit
  -y, --yes   don't ask for confirmation

examples:
  pogo uninstall
  pogo uninstall --yes     don't ask first
```
