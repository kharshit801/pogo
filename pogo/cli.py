"""pogo command line interface.

Plain `pogo` prints an overview of every command and the current status;
`pogo help <command>` shows details and examples for one command.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import difflib
import getpass
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time

from . import __version__, config, helper, proxifier_import, sysproxy, system, tun, ui
from .launch import run_as_root, self_command
from .rules import bypass_list, decide, is_ip_rule, normalize_pattern
from .server import UpstreamError, open_via_upstream, serve

# --- the overview screen (plain `pogo`) -----------------------------------------

ENV_EXAMPLE = "pogo env | Invoke-Expression" if system.IS_WINDOWS else 'eval "$(pogo env)"'

SECTIONS = [
    ("Everyday", [
        ("on", "Turn on: send every app's traffic through your proxy"),
        ("off", "Turn off and put your network back to normal"),
        ("status", "Is it on? Which proxy? What is being captured?"),
        ("logs -f", "Watch connections live: where each one goes, and why"),
    ]),
    ("Set up", [
        ("import", "Copy your proxy and rules from Proxifier"),
        ("setup", "Type in proxy address, port, username and password"),
        ("test", "Check the proxy is reachable and your password works"),
    ]),
    ("Rules: what goes through the proxy, goes direct, or is blocked", [
        ("rules", "List the rules (checked top to bottom, first match wins)"),
        ("rules add", 'Add one:  pogo rules add "*.youtube.com" block'),
        ("rules remove", "Remove one by its number"),
        ("rules check", "Which rule does a site hit?  pogo rules check github.com"),
    ]),
    ("Terminal tools in basic mode (git, brew, pip, npm…)", [
        ("run", "Run one command through the proxy:  pogo run -- git pull"),
        ("env", "Use the proxy in this shell:  " + ENV_EXAMPLE),
    ]),
    ("When something's wrong", [
        ("doctor", "Find problems and explain how to fix them"),
        ("reset", "Emergency undo of every network change pogo made"),
    ]),
]
ADVANCED = "start, stop, system, tun, helper"

PARSERS: dict[str, argparse.ArgumentParser] = {}   # "rules add" -> its parser, for `pogo help`
COMMANDS: set[str] = set()


def print_home() -> None:
    cfg = config.load()
    print(f"{ui.bold('pogo')} {ui.dim(__version__)} · route every app on your {system.MACHINE} through your proxy")
    print()
    print(f"  {status_line(cfg)}")
    if not config.is_configured(cfg):
        print()
        print(ui.bold("Get started"))
        print(f"  1. {ui.cyan('pogo import')}   copy your settings from Proxifier   (or {ui.cyan('pogo setup')})")
        print(f"  2. {ui.cyan('pogo test')}     check the proxy and your password")
        print(f"  3. {ui.cyan('pogo on')}       turn it on")
    for title, commands in SECTIONS:
        print()
        print(ui.bold(title))
        for name, text in commands:
            print(f"  {ui.cyan(name.ljust(15))}{text}")
    print()
    print(ui.dim(f"Advanced: {ADVANCED}"))
    print(f"Details and examples: {ui.cyan('pogo help <command>')}   e.g. pogo help on")


# --- state ---------------------------------------------------------------------------

def capture_state(cfg: dict) -> dict:
    sys_on, sys_addr = sysproxy.effective()
    mine = config.listen_addr(cfg)
    return {
        "pid": config.running_pid(),
        "tunnel": config.read_tun_state(),
        "system": sys_on and sys_addr == mine,
        "system_other": sys_addr if sys_on and sys_addr != mine else None,
    }


def status_line(cfg: dict, state: dict | None = None) -> str:
    if not config.is_configured(cfg):
        return f"{ui.yellow('○ not set up yet')}  ·  start with: pogo import  (or: pogo setup)"
    state = state or capture_state(cfg)
    via = f"via {cfg['upstream']['host']}:{cfg['upstream']['port']}"
    if state["tunnel"] and state["pid"]:
        return f"{ui.green('● ON')}  ·  all apps  ·  {via}"
    if state["system"] and state["pid"]:
        return f"{ui.green('● ON')}  ·  basic: apps that use the system proxy  ·  {via}"
    if state["system"]:
        return f"{ui.red('✖ BROKEN')}  ·  system proxy points at pogo, which isn't running  ·  fix: pogo off"
    if state["pid"]:
        return f"{ui.yellow('◐ standby')}  ·  local proxy running, nothing sent to it yet  ·  pogo on"
    return f"{ui.dim('○ OFF')}  ·  proxy {cfg['upstream']['host']}:{cfg['upstream']['port']} is set up  ·  turn on: pogo on"


def proxifier_running() -> bool:
    if system.IS_WINDOWS:
        return "proxifier.exe" in system.process_names()
    return system.run(["pgrep", "-x", "Proxifier"]).returncode == 0


def _quit_proxifier(assume_yes: bool) -> bool:
    """Proxifier grabs connections before pogo sees them. Returns False to abort."""
    ui.warn("Proxifier is running. It grabs traffic before pogo can, so don't run both.")
    if system.IS_WINDOWS:
        # Don't force-kill it: that could leave its network driver redirecting to a dead app.
        ui.hint("quit it from its icon next to the clock: right-click it, then Exit")
        for _ in range(3):
            if not ui.interactive():
                return False
            try:
                input("  press Enter once Proxifier is closed... ")
            except EOFError:
                return False
            if not proxifier_running():
                ui.ok("Proxifier closed")
                return True
            ui.warn("Proxifier is still running")
        return False
    if ui.confirm("Quit Proxifier now?", default=True, assume_yes=assume_yes):
        system.run(["osascript", "-e", 'quit app "Proxifier"'])
        for _ in range(50):
            if not proxifier_running():
                ui.ok("Proxifier quit")
                return True
            time.sleep(0.1)
        ui.fail("Proxifier didn't quit. Quit it from its menu-bar icon, then run pogo on again.")
        return False
    ui.hint("continuing anyway; Proxifier will keep handling the traffic it captures")
    return True


def check_proxy(cfg: dict, host: str = "example.com", port: int = 443,
                timeout: float = 8.0) -> tuple[bool, str, int | None]:
    """Log in to the proxy and open a tunnel to host:port. Returns (ok, message, http_status)."""
    up = cfg["upstream"]
    pw = config.get_password(up)
    if up.get("username") and not pw:
        return False, f"no password saved for {up['username']}", None

    async def probe():
        _, writer = await asyncio.wait_for(open_via_upstream(up, pw, host, port), timeout)
        writer.close()

    try:
        asyncio.run(probe())
    except UpstreamError as e:
        if e.code == 407 and e.schemes and "basic" not in e.schemes:
            wanted = "/".join(s.upper() for s in e.schemes)
            return False, f"the proxy wants {wanted} login, which pogo doesn't support yet (only Basic)", 407
        if e.code == 407:
            return False, "the proxy rejected your username or password (HTTP 407)", 407
        if e.code == 403:
            return False, f"the proxy refused {host}:{port} (HTTP 403, its own rules)", 403
        return False, str(e), e.code
    except asyncio.TimeoutError:
        return False, f"no answer from the proxy at {up['host']}:{up['port']} (on the campus network?)", None
    except OSError as e:
        reason = e.strerror or str(e) or type(e).__name__
        return False, f"can't reach the proxy at {up['host']}:{up['port']}: {reason} (on the campus network?)", None
    return True, f"logged in and reached {host}:{port} through the proxy", 200


# --- local proxy process -----------------------------------------------------------------

def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _rotate(path: Path, limit: int = 5_000_000) -> None:
    try:
        if path.stat().st_size > limit:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def _tail(path: Path, n: int) -> list[str]:
    try:
        with open(path, errors="replace") as f:
            return [line.rstrip("\n") for line in collections.deque(f, maxlen=n)]
    except OSError:
        return []


def _ensure_daemon(cfg: dict) -> bool:
    host, port = cfg["listen"]["host"], cfg["listen"]["port"]
    if config.running_pid():
        ui.ok(f"local proxy already running on {host}:{port}")
        return True
    if _port_open(host, port):
        ui.fail(f"port {port} is already used by another program")
        ui.hint(f'pick another: change "listen" → "port" in {config.CONFIG_FILE}')
        return False
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    _rotate(config.LOG_FILE)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}  # UTF-8 log on Windows too
    with open(config.LOG_FILE, "ab") as log_file:
        child = system.spawn_background(self_command("start"), log_file, env=env)
    for _ in range(50):
        if child.poll() is not None:
            break
        if _port_open(host, port) and config.running_pid():
            ui.ok(f"local proxy started on {host}:{port}")
            return True
        time.sleep(0.1)
    ui.fail("the local proxy didn't start. Last log lines:")
    for line in _tail(config.LOG_FILE, 5):
        ui.hint(line)
    return False


def _stop_daemon(timeout: float = 5.0) -> None:
    pid = config.running_pid()
    if not pid:
        return
    system.terminate(pid)  # SIGTERM on macOS (it cleans up), TerminateProcess on Windows
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and system.pid_alive(pid):
        time.sleep(0.1)
    if system.pid_alive(pid):
        system.kill(pid)
    try:
        system.remove_file(config.PID_FILE)
    except OSError:
        pass


def _stop_capture(timeout: float = 10.0) -> bool:
    """Ask the root capture process to stop (it watches for this file) and wait for it."""
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    config.TUN_STOP.touch()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not config.read_tun_state():
            return True
        time.sleep(0.2)
    return False


def _report(kind: str, msg: str) -> None:
    {"ok": ui.ok, "warn": ui.warn, "fail": ui.fail}.get(kind, ui.info)(msg)


# --- everyday commands ---------------------------------------------------------------

def cmd_on(args) -> int:
    cfg = config.load()
    if not config.is_configured(cfg):
        print("pogo isn't set up yet, so let's do that first.\n")
        if not ui.interactive():
            ui.hint("run: pogo import   (or: pogo setup)")
            return 1
        if cmd_setup(argparse.Namespace(host=None, port=None, type=None, user=None,
                                        password_stdin=False, from_on=True)) != 0:
            return 1
        cfg = config.load()
        print()

    state = capture_state(cfg)
    if state["pid"] and state["tunnel"]:
        print(status_line(cfg, state))
        print(ui.dim("  already on. Turn off with: pogo off"))
        return 0
    if state["pid"] and state["system"] and args.basic:
        print(status_line(cfg, state))
        print(ui.dim("  already on. To capture all apps instead: pogo off, then pogo on"))
        return 0

    print(ui.bold("Turning pogo on") + ui.dim("  (basic)" if args.basic else "  (all apps)"))
    if proxifier_running() and not _quit_proxifier(args.yes):
        return 1

    ok, message, _ = check_proxy(cfg)
    if ok:
        ui.ok(f"proxy login works ({cfg['upstream']['host']}:{cfg['upstream']['port']})")
    else:
        ui.fail(message)
        if not args.force:
            ui.hint("fix it with: pogo setup   ·   details: pogo doctor   ·   ignore: pogo on --force")
            return 1
    if not _ensure_daemon(cfg):
        return 1

    if not args.basic:
        if not tun.missing_helpers() or _install_helper(cfg, ask=not args.yes):
            if system.IS_WINDOWS:
                started = tun.start_elevated(cfg, _report) == 0
            else:
                ui.info("capturing all apps needs admin rights, so macOS asks for your login password")
                started = run_as_root("tun", "--background", home=str(config.HOME_DIR)) == 0
            if started:
                _say_on(cfg, all_apps=True)
                return 0
            if not ui.confirm("All-apps capture didn't start. Use basic mode instead?", default=True,
                              assume_yes=args.yes):
                _stop_daemon()
                return 1
        else:
            ui.warn("continuing in basic mode (browsers and most apps)")

    errors = sysproxy.enable(cfg["listen"]["host"], cfg["listen"]["port"], bypass_list(cfg))
    for err in errors:
        ui.fail(f"system proxy: {err}")
    if errors:
        if not system.IS_WINDOWS:
            ui.hint("if it mentions admin rights, run: sudo pogo system on")
        return 1
    ui.ok("system proxy set (" + ", ".join(sysproxy.services()) + ")")
    _say_on(cfg, all_apps=False)
    return 0


def _say_on(cfg: dict, all_apps: bool) -> None:
    where = f"{cfg['upstream']['host']}:{cfg['upstream']['port']}"
    print()
    if all_apps:
        print(f"{ui.green('● pogo is ON')} · every app now goes through {where}")
    else:
        print(f"{ui.green('● pogo is ON')} (basic) · apps that use the system proxy go through {where}")
        print(ui.dim("  terminal tools: pogo run -- <command>   ·   all apps: pogo off, then pogo on"))
    print(ui.dim("  watch traffic: pogo logs -f   ·   turn off: pogo off"))


def cmd_off(args) -> int:
    cfg = config.load()
    addr = config.listen_addr(cfg)
    state = capture_state(cfg)
    if not (state["tunnel"] or state["pid"] or sysproxy.pointing_to(addr)):
        print(f"{ui.dim('○')} pogo is already off.")
        left = tun.leftovers()
        if left:
            ui.warn("leftovers from an earlier run: " + ", ".join(left))
            ui.hint("clean up with: pogo reset")
        return 0

    print(ui.bold("Turning pogo off"))
    rc = 0
    if state["tunnel"]:
        if _stop_capture():
            ui.ok("all-apps capture removed (routes and virtual interface)")
        else:
            ui.fail("the all-apps capture didn't stop by itself")
            ui.hint("run: pogo reset")
            rc = 1
    services = sysproxy.pointing_to(addr)  # the capture usually restored these already
    if services:
        errors = sysproxy.disable(match=addr)
        for err in errors:
            ui.fail(f"system proxy: {err}")
        if errors:
            ui.hint("try: sudo pogo system off")
            rc = 1
        else:
            ui.ok("system proxy switched off (" + ", ".join(services) + ")")
    if config.running_pid():
        _stop_daemon()
        ui.ok("local proxy stopped")
    if rc == 0:
        print()
        print(f"{ui.dim('○')} pogo is OFF · your {system.MACHINE} is back to normal networking")
    return rc


def cmd_status(args) -> int:
    cfg = config.load()
    state = capture_state(cfg)
    print(status_line(cfg, state))
    if not config.is_configured(cfg):
        return 0
    up = cfg["upstream"]
    addr = config.listen_addr(cfg)
    services = sysproxy.pointing_to(addr)
    tunnel = state["tunnel"]
    rows = [
        ("proxy", f"{up['type']}://{up['host']}:{up['port']}"
                  + (f"  ·  user {up['username']}" if up["username"] else "  ·  no login")),
        ("local proxy", f"running on {addr} (pid {state['pid']})" if state["pid"] else "stopped"),
        ("all apps", f"captured through {tunnel.get('device')} · real network "
                     f"{tunnel.get('label') or tunnel.get('interface')}"
                     if tunnel else "not captured"),
        ("system proxy", ", ".join(services) if services else
         (f"points at another proxy ({state['system_other']})" if state["system_other"] else "off")),
        ("rules", f"{len(cfg['rules'])} rules · everything else: {cfg['default_action']}"),
    ]
    print()
    for label, value in rows:
        print(f"  {ui.dim(label.ljust(14))}{value}")
    print()
    on = bool(state["pid"] and (tunnel or services))
    print(ui.dim("  watch traffic: pogo logs -f   ·   turn off: pogo off" if on else "  turn on: pogo on"))
    left = [] if tunnel else tun.leftovers()
    if left:
        ui.warn("leftovers from an earlier run: " + ", ".join(left) + "  →  fix: pogo reset")
    return 0


_LOG_COLORS = {"PROXY": ui.cyan, "DIRECT": ui.green, "BLOCK": ui.red}
_LOG_ACTION = re.compile(r"\b(PROXY|DIRECT|BLOCK)\b")


def _colorize(line: str) -> str:
    if "failed" in line or "WARNING" in line or "ERROR" in line:
        return ui.yellow(line)
    return _LOG_ACTION.sub(lambda m: _LOG_COLORS[m.group(1)](m.group(1)), line, count=1)


def cmd_logs(args) -> int:
    path = config.LOG_FILE
    if not path.exists():
        print("No connections logged yet. Turn pogo on (pogo on), then look again.")
        return 0
    f = open(path, encoding="utf-8", errors="replace")
    try:
        for line in collections.deque(f, maxlen=args.lines):
            print(_colorize(line.rstrip("\n")))
        if not args.follow:
            print(ui.dim("\n  live view: pogo logs -f"))
            return 0
        print(ui.dim("  watching for new connections… (Ctrl-C to stop)"), flush=True)
        while True:
            line = f.readline()
            if line:
                print(_colorize(line.rstrip("\n")), flush=True)
                continue
            time.sleep(0.3)
            try:
                if os.stat(path).st_ino != os.fstat(f.fileno()).st_ino:  # log was rotated
                    f.close()
                    f = open(path, encoding="utf-8", errors="replace")
            except FileNotFoundError:
                pass
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        f.close()


# --- setup -----------------------------------------------------------------------------

def cmd_setup(args) -> int:
    cfg = config.load()
    up = cfg["upstream"]
    old = dict(up)
    flags = any(v is not None for v in (args.host, args.port, args.type, args.user))
    interactive = ui.interactive() and not flags

    if interactive:
        print(ui.bold("Proxy setup") + ui.dim("  (press Enter to keep the value in [brackets])"))
        if not config.is_configured(cfg):
            try:
                profile = proxifier_import.locate()
            except proxifier_import.ImportProblem:
                profile = None
            if profile:
                print(f"  Found your Proxifier profile {ui.bold(profile.name)}.")
                if ui.confirm("Copy the proxy and rules from it?", default=True):
                    return cmd_import(argparse.Namespace(profile=str(profile.path), list=False, yes=True,
                                                         from_on=getattr(args, "from_on", False)))
        print()

    def value(flag, label, current):
        if flag is not None:
            return str(flag)
        return ui.ask(label, str(current)) if interactive else str(current)

    host = value(args.host, "Proxy address (IP or hostname)", up["host"]).strip()
    port = value(args.port, "Port", up["port"]).strip()
    kind = value(args.type, "Type: http (most campus proxies, e.g. port 3128/8080) or socks5",
                 up["type"]).strip().lower()
    user = value(args.user, "Username (leave empty if the proxy has no login)", up["username"]).strip()

    if kind == "https":
        kind = "http"  # Proxifier's "HTTPS" proxy type is an HTTP proxy used with CONNECT
    if not host:
        ui.error("the proxy address is required")
        return 1
    if not port.isdigit() or not 0 < int(port) < 65536:
        ui.error(f"'{port}' isn't a valid port number")
        return 1
    if kind not in ("http", "socks5"):
        ui.error("type must be 'http' or 'socks5'")
        return 1
    up.update(host=host, port=int(port), type=kind, username=user)

    password = ""
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    elif interactive and user:
        password = getpass.getpass("  Password (hidden; Enter keeps the saved one): ")

    if (old["username"], old["host"], old["port"]) != (up["username"], up["host"], up["port"]):
        keep = password or config.get_password(old)  # carry the old password over to the new account
        config.delete_password(old)
        password = keep
    if user and password:
        config.set_password(up, password)
    config.save(cfg)
    print()
    ui.ok(f"saved: {kind}://{host}:{port}" + (f" as {user} (password in the {system.PASSWORD_STORE})" if user else ""))
    if interactive:
        ok, msg, code = check_proxy(cfg)
        (ui.ok if ok else ui.fail)(msg)
        if code == 407:
            ui.hint("run pogo setup again and re-type the password")
    if not getattr(args, "from_on", False):
        print(f"\nNext: {ui.cyan('pogo on')}")
    return 0


def cmd_import(args) -> int:
    if args.list:
        try:
            profiles = proxifier_import.list_profiles()
        except proxifier_import.ImportProblem as e:
            ui.error(str(e))
            return 1
        if not profiles:
            print("No Proxifier profiles found on this Mac.")
            return 1
        print(ui.bold("Proxifier profiles"))
        for p in profiles:
            print(f"  {p.name}" + (ui.green(f"   ← {p.note}") if p.note else ""))
        print(ui.dim("\n  import one with: pogo import <name>"))
        return 0

    try:
        profile = proxifier_import.locate(args.profile)
        result = proxifier_import.convert(profile.path)
    except proxifier_import.ImportProblem as e:
        ui.error(str(e))
        return 1
    up = result.upstream
    if up is None:
        ui.error(f"profile '{profile.name}' has no proxy pogo can use ({result.proxy_label})")
        for note in result.notes:
            ui.hint(note)
        return 1

    print(ui.bold(f"Proxifier profile '{profile.name}'") + (ui.dim(f"  ({profile.note})") if profile.note else ""))
    print(f"  {ui.dim('proxy'.ljust(10))}{up['host']}:{up['port']}  ·  {result.proxy_label.split()[0]} → {up['type']}"
          + (f"  ·  user {up['username']}" if up["username"] else "  ·  no login"))
    if result.rules:
        shown = ", ".join(f"{r['pattern']} → {r['action']}" for r in result.rules[:6])
        more = f" (+{len(result.rules) - 6} more)" if len(result.rules) > 6 else ""
        print(f"  {ui.dim('rules'.ljust(10))}{shown}{more}")
    print(f"  {ui.dim('default'.ljust(10))}everything else → {result.default_action}")
    if result.skipped_apps:
        apps = ", ".join(f"{app} ({action})" for app, action in result.skipped_apps[:8])
        more = ", …" if len(result.skipped_apps) > 8 else ""
        ui.warn(f"{len(result.skipped_apps)} per-app rules can't be copied: pogo sees connections, "
                "not which app opened them")
        ui.hint(apps + more)
        ui.hint(f"those apps follow the default ({result.default_action}); all-apps mode captures them too")
    for note in result.notes:
        ui.warn(note)
    if not ui.confirm("Import these settings?", default=True, assume_yes=args.yes):
        print("Nothing changed.")
        return 1

    cfg = config.load()
    old = dict(cfg["upstream"])
    cfg["upstream"].update(up)
    cfg["rules"] = proxifier_import.merged_rules(result.rules)
    cfg["default_action"] = result.default_action
    config.save(cfg)
    ui.ok(f"saved: proxy {up['host']}:{up['port']} and {len(cfg['rules'])} rules "
          "(pogo's local-network rules included)")

    new = cfg["upstream"]
    if new["username"]:
        saved = config.get_password(new)
        if ui.interactive():
            extra = "; Enter keeps the saved one" if saved else ""
            pw = getpass.getpass(f"  Password for {new['username']} "
                                 f"(Proxifier stores it encrypted, so type it once{extra}): ")
            if pw:
                config.set_password(new, pw)
                if (old.get("username"), old.get("host"), old.get("port")) != \
                        (new["username"], new["host"], new["port"]):
                    config.delete_password(old)
                ui.ok(f"password saved in the {system.PASSWORD_STORE}")
                saved = pw
        if not saved:
            ui.warn("no password saved yet. Add it with: pogo setup")
        elif ui.interactive():
            ok, msg, _ = check_proxy(cfg)
            (ui.ok if ok else ui.fail)(msg)
    if not getattr(args, "from_on", False):
        print(f"\nNext: {ui.cyan('pogo on')}")
    return 0


def cmd_test(args) -> int:
    cfg = config.load()
    if not config.is_configured(cfg):
        ui.error("no proxy set up yet. Run: pogo import   (or: pogo setup)")
        return 1
    up = cfg["upstream"]
    site = normalize_pattern(args.site) or "example.com"
    print(f"Testing {up['type']}://{up['host']}:{up['port']}"
          + (f" as {up['username']}" if up["username"] else "") + " …")
    ok, msg, code = check_proxy(cfg, host=site, port=args.port)
    (ui.ok if ok else ui.fail)(msg)
    if code == 407 or msg.startswith("no password"):
        ui.hint("re-enter your username and password: pogo setup")
    return 0 if ok else 1


# --- rules -------------------------------------------------------------------------------

def cmd_rules(args) -> int:
    cfg = config.load()
    rules = cfg["rules"]
    sub = getattr(args, "rules_cmd", None) or "list"
    sub = {"rm": "remove", "test": "check"}.get(sub, sub)  # argparse reports the alias that was typed

    if sub == "list":
        print(ui.bold("Rules") + ui.dim("  (checked top to bottom; the first match decides)"))
        for i, r in enumerate(rules, 1):
            print(f"  {ui.dim(str(i).rjust(3))}  {ui.action(r['action'])}{' ' * (8 - len(r['action']))}{r['pattern']}")
        default = cfg["default_action"]
        print(f"  {ui.dim('  *')}  {ui.action(default)}{' ' * (8 - len(default))}{ui.dim('everything else (default)')}")
        print(ui.dim("\n  add: pogo rules add <site> <proxy|direct|block>   ·   remove: pogo rules remove <number>"))
        return 0

    if sub == "check":
        host = normalize_pattern(args.site)
        action, rule = decide(cfg, host)
        if rule == "default":
            print(f"{host} → {ui.action(action)}   (no rule matches, so the default applies)")
        else:
            number = next(i for i, r in enumerate(rules, 1) if r["pattern"] == rule)
            print(f"{host} → {ui.action(action)}   (rule {number}: {rule})")
        return 0

    if sub == "add":
        pattern = normalize_pattern(args.pattern)
        if not pattern:
            ui.error("give a site, IP or range, e.g.  pogo rules add \"*.youtube.com\" block")
            return 1
        replaced = ""
        for i, r in enumerate(rules):
            if r["pattern"] == pattern:
                replaced = f"  (replaces the old rule: {r['action']})"
                rules.pop(i)
                break
        index = len(rules) if args.at is None else min(max(args.at - 1, 0), len(rules))
        rules.insert(index, {"pattern": pattern, "action": args.action})
        config.save(cfg)
        ui.ok(f"rule {index + 1}: {pattern} → {ui.action(args.action)}{replaced}")
        sample = pattern[2:] if pattern.startswith("*.") else pattern
        if not is_ip_rule(pattern) and not any(c in sample for c in "*?["):
            hit_action, hit_rule = decide(cfg, sample)
            if hit_rule not in (pattern, "default") and hit_action != args.action:
                number = next(i for i, r in enumerate(rules, 1) if r["pattern"] == hit_rule)
                ui.warn(f"rule {number} ({hit_rule} → {hit_action}) comes first and wins for {sample}")
                ui.hint(f"move yours up: pogo rules add \"{pattern}\" {args.action} --at {number}")
    elif sub == "remove":
        target = args.target.strip()
        if target.isdigit():
            index = int(target) - 1
            if not 0 <= index < len(rules):
                ui.error(f"there's no rule {target} (there are {len(rules)}; see: pogo rules)")
                return 1
        else:
            pattern = normalize_pattern(target)
            index = next((i for i, r in enumerate(rules) if r["pattern"] == pattern), -1)
            if index < 0:
                ui.error(f"no rule for '{pattern}' (see: pogo rules)")
                return 1
        removed = rules.pop(index)
        config.save(cfg)
        ui.ok(f"removed rule {index + 1}: {removed['pattern']} → {removed['action']}")
    elif sub == "default":
        cfg["default_action"] = args.action
        config.save(cfg)
        ui.ok(f"anything that matches no rule now goes: {ui.action(args.action)}")
    if config.running_pid():
        ui.hint("applies right away, no restart needed")
    return 0


# --- terminal tools ----------------------------------------------------------------------

def proxy_env(cfg: dict) -> dict[str, str]:
    addr = config.listen_addr(cfg)
    no_proxy = []
    for p in bypass_list(cfg):
        if p.startswith("*."):
            no_proxy.append(p[1:])  # *.local -> .local
        elif not any(c in p for c in "*?["):
            no_proxy.append(p)
    env = {}
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env[k] = f"http://{addr}"
    for k in ("all_proxy", "ALL_PROXY"):
        env[k] = f"socks5h://{addr}"
    for k in ("no_proxy", "NO_PROXY"):
        env[k] = ",".join(no_proxy)
    return env


SHELL_SYNTAX = {  # (set, unset) per shell
    "sh": ("export {k}='{v}'", "unset {k}"),
    "powershell": ("$env:{k} = '{v}'", "Remove-Item Env:{k} -ErrorAction SilentlyContinue"),
    "cmd": ("set {k}={v}", "set {k}="),
}


def cmd_env(args) -> int:
    env = proxy_env(config.load())
    shell = args.shell or ("powershell" if system.IS_WINDOWS else "sh")
    template = SHELL_SYNTAX[shell][1 if args.unset else 0]
    for k, v in env.items():
        print(template.format(k=k, v=v))
    return 0


def cmd_run(args) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ui.error("say which command to run, e.g.  pogo run -- git pull")
        return 1
    cfg = config.load()
    if not config.running_pid():
        ui.warn("the local proxy isn't running, so this will fail. Start it: pogo on")
    env = {**os.environ, **proxy_env(cfg)}
    if system.IS_WINDOWS:
        executable = shutil.which(command[0])  # finds npm.cmd, git.exe ... through PATHEXT
        if not executable:
            ui.error(f"command not found: {command[0]}")
            return 127
        try:
            return subprocess.run([executable, *command[1:]], env=env).returncode
        except KeyboardInterrupt:
            return 130
    try:
        os.execvpe(command[0], command, env)
    except FileNotFoundError:
        ui.error(f"command not found: {command[0]}")
        return 127


# --- troubleshooting ---------------------------------------------------------------------

def cmd_doctor(args) -> int:
    cfg = config.load()
    up = cfg["upstream"]
    addr = config.listen_addr(cfg)
    counts = {"problems": 0, "warnings": 0}

    def problem(msg: str, fix: str) -> None:
        counts["problems"] += 1
        ui.fail(msg)
        ui.hint(fix)

    def caution(msg: str, note: str = "") -> None:
        counts["warnings"] += 1
        ui.warn(msg)
        if note:
            ui.hint(note)

    print(ui.bold("Settings"))
    configured = config.is_configured(cfg)
    if not configured:
        problem("no proxy set up", "run: pogo import   (or: pogo setup)")
    else:
        ui.ok(f"proxy: {up['type']}://{up['host']}:{up['port']}" + (f" as {up['username']}" if up["username"] else ""))
        if up["username"]:
            if config.get_password(up):
                ui.ok(f"password saved in the {system.PASSWORD_STORE}")
            else:
                problem(f"no password saved for {up['username']}", "run: pogo setup")

    print("\n" + ui.bold("Network"))
    tunnel = config.read_tun_state()
    if tunnel:
        ui.ok(f"connected: {tunnel.get('label') or tunnel.get('interface')} via {tunnel.get('gateway') or 'direct link'}")
    else:
        net = tun.default_net()
        iface, gateway = net.primary_route()
        if iface:
            ui.ok(f"connected: {net.label(iface)} via {gateway or 'direct link'}")
        else:
            problem("no network connection", "connect to the campus Wi-Fi or Ethernet")
    try:
        socket.getaddrinfo("example.com", 443)
        ui.ok("DNS works (internet names resolve on this network)")
    except socket.gaierror:
        caution("this network can't look up internet names by itself",
                "all-apps mode needs that; basic mode still works: pogo on --basic")
    if _ipv6_internet():
        caution("this network has IPv6, which all-apps mode doesn't capture",
                "apps reaching a site over IPv6 skip pogo; campus networks rarely have it")
    if configured and up["username"] and not config.get_password(up):
        ui.info("proxy login not tested yet (no password saved)")
    elif configured:
        ok, msg, code = check_proxy(cfg, timeout=6)
        if ok:
            ui.ok(msg)
            ok80, _, _ = check_proxy(cfg, port=80, timeout=6)
            if ok80:
                ui.ok("non-HTTPS connections (port 80) are allowed through the proxy too")
            else:
                caution("the proxy only tunnels HTTPS (it refused port 80)",
                        "browsers are fine; in all-apps mode, plain-HTTP traffic from other apps may fail")
        else:
            problem(msg, "re-enter your password: pogo setup" if code == 407 else
                    "check the address and port with your IT team, then: pogo setup")

    print("\n" + ui.bold("pogo"))
    state = capture_state(cfg)
    services = sysproxy.pointing_to(addr)
    if state["pid"]:
        ui.ok(f"local proxy running on {addr} (pid {state['pid']})")
    else:
        ui.info("local proxy not running (fine while pogo is off)")
    if services and not state["pid"]:
        problem(f"the system proxy points at pogo, which isn't running ({', '.join(services)})",
                "apps can't connect like this. Fix: pogo off")
    elif services:
        ui.ok("system proxy → pogo on " + ", ".join(services))
    elif state["system_other"]:
        caution(f"the system proxy points at another proxy ({state['system_other']})",
                "pogo on replaces it; pogo off only removes pogo's own settings")
    if state["tunnel"]:
        ui.ok(f"all-apps capture running ({state['tunnel'].get('device')})")
    else:
        left = tun.leftovers()
        if left:
            problem("leftovers from a capture that didn't shut down cleanly: " + ", ".join(left),
                    "fix: pogo reset")
    missing = tun.missing_helpers()
    if missing:
        caution("all-apps helper isn't installed (" + ", ".join(missing) + ")",
                "pogo on offers to install it, or run: pogo helper")
    else:
        ui.ok(f"all-apps helper installed ({tun.find_tun2socks()})")
    if system.IS_WINDOWS:
        from . import winproxy
        if winproxy.policy_machine_wide():
            caution("Group Policy makes proxy settings machine-wide on this PC",
                    "basic mode can't change them; all-apps mode (pogo on) works without them")
        if not system.is_admin():
            ui.info("all-apps mode shows a Windows permission prompt (UAC) when it starts")
    if proxifier_running():
        caution("Proxifier is running; it grabs traffic before pogo can", "quit Proxifier before pogo on")
    elif not system.IS_WINDOWS and system.run(["pgrep", "-i", "proxifier"]).returncode == 0:
        ui.info("Proxifier's network extension is loaded (fine while the Proxifier app is closed)")

    print()
    p, w = counts["problems"], counts["warnings"]
    if p:
        print(ui.red(f"{p} problem{'s' if p > 1 else ''}") + (f", {w} warning{'s' if w > 1 else ''}" if w else "")
              + ". Fix the ✖ items above.")
    elif w:
        print(ui.green("No problems") + f", {w} warning{'s' if w > 1 else ''}.")
    else:
        print(ui.green("Everything looks good."))
    return 1 if p else 0


def _ipv6_internet() -> bool:
    """Does this computer have a route to the IPv6 internet? (UDP connect sends nothing.)"""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as probe:
            probe.connect(("2001:4860:4860::8888", 53))
            address = probe.getsockname()[0].split("%")[0]
        return ipaddress.ip_address(address).is_global
    except (OSError, ValueError):
        return False


def cmd_reset(args) -> int:
    cfg = config.load()
    addr = config.listen_addr(cfg)
    print(ui.bold("Undoing every network change pogo made"))
    if config.read_tun_state() and _stop_capture(timeout=6):
        ui.ok("all-apps capture stopped")
    errors = sysproxy.disable(match=addr)
    for err in errors:
        ui.fail(f"system proxy: {err}")
    if not errors:
        ui.ok("system proxy: pogo's settings removed (other proxy settings untouched)")
    if config.running_pid():
        _stop_daemon()
        ui.ok("local proxy stopped")
    left = tun.leftovers()
    if left or config.read_tun_state():
        ui.info(f"found {', '.join(left) or 'a running capture'}; removing it needs admin rights")
        try:
            code = run_as_root("tun", "cleanup", home=str(config.HOME_DIR))
        except PermissionError:
            ui.fail("admin permission was declined, so the leftovers are still there")
            return 1
        if code != 0:
            ui.fail("cleanup failed")
            return 1
    left = tun.leftovers()
    if left:
        ui.fail("still present: " + ", ".join(left))
        return 1
    print(f"\n{ui.green('✔')} all clean · your {system.MACHINE} is back to normal networking")
    return 0


# --- advanced ----------------------------------------------------------------------------

def cmd_start(args) -> int:
    cfg = config.load()
    if not config.is_configured(cfg):
        ui.error("no proxy set up yet. Run: pogo import   (or: pogo setup)")
        return 1
    if args.daemon:
        return 0 if _ensure_daemon(cfg) else 1
    if config.running_pid():
        ui.error(f"already running (pid {config.running_pid()}). Stop it with: pogo stop")
        return 1
    host, port = cfg["listen"]["host"], cfg["listen"]["port"]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    config.PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    if args.system:
        for err in sysproxy.enable(host, port, bypass_list(cfg)):
            logging.warning("system proxy: %s", err)
    try:
        asyncio.run(serve(config.ConfigStore(), host, port))
    except OSError as e:
        logging.error("can't listen on %s:%d: %s", host, port, e)
        return 1
    finally:
        if args.system:
            sysproxy.disable(match=config.listen_addr(cfg))
        try:
            if config.PID_FILE.read_text().strip() == str(os.getpid()):
                config.PID_FILE.unlink()
        except OSError:
            pass
    return 0


def cmd_stop(args) -> int:
    cfg = config.load()
    if not config.running_pid():
        print("The local proxy isn't running.")
        return 0
    if config.read_tun_state():
        ui.warn("all-apps capture is on; it switches itself off when the local proxy stops")
    _stop_daemon()
    ui.ok("local proxy stopped")
    if sysproxy.pointing_to(config.listen_addr(cfg)):
        sysproxy.disable(match=config.listen_addr(cfg))
        ui.ok("system proxy switched off (it pointed at the stopped proxy)")
    return 0


def cmd_system(args) -> int:
    cfg = config.load()
    if args.state == "on":
        if not config.running_pid():
            ui.warn("the local proxy isn't running, so apps can't connect until: pogo start -d")
        errors = sysproxy.enable(cfg["listen"]["host"], cfg["listen"]["port"], bypass_list(cfg))
    else:
        errors = sysproxy.disable(match=config.listen_addr(cfg))
    for err in errors:
        ui.fail(err)
    if not errors:
        ui.ok(f"system proxy {args.state}")
    return 1 if errors else 0


def cmd_tun(args) -> int:
    cfg = config.load()
    if args.worker:
        return tun.run_worker(cfg, report_fd=args.report_fd, report_file=args.report_file,
                              log_file=args.log_file)
    if args.background:
        return tun.start_background(cfg, _report)
    if args.action == "cleanup":
        return tun.cleanup(_report)
    return tun.run_foreground(cfg, _report, dry_run=args.dry_run)


def _install_helper(cfg: dict, ask: bool) -> bool:
    missing = tun.missing_helpers() or ["tun2socks"]
    ui.warn("all-apps mode needs a small open-source helper (" + " and ".join(missing) + "), not installed yet")
    ui.hint(helper.describe())
    if ask and not ui.confirm("Download it now?", default=True):
        return False
    proxy = f"http://{config.listen_addr(cfg)}" if config.running_pid() else None  # through your proxy
    try:
        path = helper.install(proxy=proxy, report=_report)
    except helper.HelperError as e:
        ui.fail(str(e))
        return False
    ui.ok(f"installed into {path.parent}")
    return True


def cmd_helper(args) -> int:
    if not tun.missing_helpers() and not args.reinstall:
        ui.ok(f"already installed: {tun.find_tun2socks()}")
        return 0
    return 0 if _install_helper(config.load(), ask=False) else 1


# --- argument parsing --------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message: str):
        ui.error(message)
        print(file=sys.stderr)
        self.print_help(sys.stderr)
        sys.exit(2)


def _examples(*lines: str) -> str:
    return "examples:\n" + "\n".join(f"  {line}" for line in lines)


def _command(sub, name: str, fn, summary: str, description: str, examples: tuple = (),
             aliases: tuple = (), key: str | None = None) -> argparse.ArgumentParser:
    p = sub.add_parser(name, aliases=list(aliases), help=summary, description=description,
                       epilog=_examples(*examples) if examples else None,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.set_defaults(fn=fn)
    PARSERS[key or name] = p
    for alias in aliases:
        PARSERS[alias if key is None else key.replace(name, alias)] = p
    return p


def build_parser() -> Parser:
    PARSERS.clear()
    parser = Parser(prog="pogo", add_help=False)
    sub = parser.add_subparsers(dest="cmd", parser_class=Parser)

    admin_prompt = "Windows shows a permission prompt (UAC)" if system.IS_WINDOWS else \
        "macOS asks for your login password"
    proxy_settings = "Windows proxy settings" if system.IS_WINDOWS else "macOS system proxy"
    p = _command(sub, "on", cmd_on, "turn pogo on",
                 "Turn pogo on.\n\n"
                 "By default it captures ALL apps, like Proxifier: a virtual network interface hands\n"
                 "every app's connections to pogo, which applies your rules and logs in to your proxy.\n"
                 f"That needs admin rights, so {admin_prompt}.\n\n"
                 f"With --basic it only sets the {proxy_settings}. No admin rights needed, and it covers\n"
                 "browsers and most apps, but not apps that ignore proxy settings (Docker, IDE helpers,\n"
                 "command-line tools).",
                 ("pogo on             capture every app", "pogo on --basic     browsers and most apps, no admin password"))
    p.add_argument("--basic", action="store_true", help="only set the system proxy (no admin password)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask questions, assume yes")
    p.add_argument("--force", action="store_true", help="turn on even if the proxy login check fails")

    _command(sub, "off", cmd_off, "turn pogo off",
             "Turn pogo off: removes the all-apps capture, switches the system proxy off and stops\n"
             f"the local proxy. Your {system.MACHINE} goes back to normal networking. No password needed.",
             ("pogo off",))

    _command(sub, "status", cmd_status, "show what's running",
             "Show whether pogo is on, which proxy it uses and what it captures.", ("pogo status",))

    p = _command(sub, "logs", cmd_logs, "show connections",
                 "Show the connections pogo handled: where each one went (PROXY, DIRECT or BLOCK),\n"
                 "whether it came from an app's proxy settings or from the all-apps capture, and which\n"
                 "rule decided.",
                 ("pogo logs           the last 50 connections", "pogo logs -f        watch live (Ctrl-C to stop)",
                  "pogo logs -n 200    the last 200"))
    p.add_argument("-f", "--follow", action="store_true", help="keep watching for new connections")
    p.add_argument("-n", "--lines", type=int, default=50, metavar="N", help="how many recent lines to show (default 50)")

    p = _command(sub, "setup", cmd_setup, "enter proxy details",
                 "Enter your proxy's address, port, username and password, step by step.\n"
                 f"Press Enter to keep a value. The password goes into the {system.PASSWORD_STORE},\n"
                 "never into a file.\n"
                 "If Proxifier is installed, setup offers to copy its settings instead.",
                 ("pogo setup", "pogo setup --host proxy.example.edu --port 3128 --user alice"))
    p.add_argument("--host", help="proxy IP or hostname")
    p.add_argument("--port", help="proxy port, e.g. 3128 or 8080")
    p.add_argument("--type", help="http (most campus proxies) or socks5")
    p.add_argument("--user", help="username (empty string for none)")
    p.add_argument("--password-stdin", action="store_true", help="read the password from standard input")

    p = _command(sub, "import", cmd_import, "copy settings from Proxifier",
                 "Copy the proxy and rules from a Proxifier profile (Proxifier's active one unless you\n"
                 "name another). Site and IP rules are copied; per-app rules can't be, because pogo\n"
                 "sees connections, not which app opened them. Proxifier keeps your password encrypted,\n"
                 "so you type it once.",
                 ("pogo import                    the profile active in Proxifier", "pogo import --list             see all profiles",
                  "pogo import campus             a profile by name", "pogo import ~/Desktop/x.ppx    an exported profile file"))
    p.add_argument("profile", nargs="?", help="profile name or path to a .ppx file")
    p.add_argument("--list", action="store_true", help="list Proxifier profiles")
    p.add_argument("-y", "--yes", action="store_true", help="import without asking")

    p = _command(sub, "test", cmd_test, "check proxy and password",
                 "Check that the proxy answers and accepts your username and password, by opening a\n"
                 "connection to a site through it.",
                 ("pogo test", "pogo test github.com"))
    p.add_argument("site", nargs="?", default="example.com", help="site to reach (default example.com)")
    p.add_argument("--port", type=int, default=443, help="port on that site (default 443)")

    p = _command(sub, "rules", cmd_rules, "list and change rules",
                 "Rules decide where each connection goes:\n"
                 "  proxy    through your campus proxy\n"
                 "  direct   straight out, without the proxy (campus sites, local network)\n"
                 "  block    refused\n"
                 "They're checked top to bottom and the first match wins; anything that matches no rule\n"
                 "uses the default. Changes apply right away, no restart needed.\n\n"
                 "Patterns:\n"
                 "  example.com      that site and all its subdomains\n"
                 "  *.example.com    the same, written Proxifier-style\n"
                 "  10.0.0.0/8       an IP range (a single IP works too)\n"
                 "  192.168.*        wildcards\n"
                 "  *                everything",
                 ("pogo rules", 'pogo rules add "*.youtube.com" block', "pogo rules add intranet.college.edu direct --at 1",
                  "pogo rules remove 3", "pogo rules check github.com", "pogo rules default proxy"),
                 aliases=("rule",))
    rs = p.add_subparsers(dest="rules_cmd", parser_class=Parser, metavar="{list,add,remove,check,default}")
    _command(rs, "list", cmd_rules, "show all rules", "Show all rules in order.", key="rules list")
    a = _command(rs, "add", cmd_rules, "add a rule",
                 "Add a rule. A rule for the same site is replaced. New rules go to the end unless you\n"
                 "give --at (1 = checked first).",
                 ('pogo rules add "*.youtube.com" block', "pogo rules add 10.20.0.0/16 direct --at 1"), key="rules add")
    a.add_argument("pattern", help="site, IP, range or wildcard")
    a.add_argument("action", choices=config.ACTIONS, help="where matching connections go")
    a.add_argument("--at", type=int, metavar="N", help="position (1 = checked first)")
    r = _command(rs, "remove", cmd_rules, "remove a rule", "Remove a rule by its number (see: pogo rules) or its pattern.",
                 ("pogo rules remove 3", 'pogo rules remove "*.youtube.com"'), aliases=("rm",), key="rules remove")
    r.add_argument("target", help="rule number or pattern")
    c = _command(rs, "check", cmd_rules, "which rule a site hits", "Show which rule decides a site, and what happens to it.",
                 ("pogo rules check github.com",), aliases=("test",), key="rules check")
    c.add_argument("site")
    d = _command(rs, "default", cmd_rules, "what happens when no rule matches",
                 "Set what happens to connections that match no rule.", ("pogo rules default proxy",), key="rules default")
    d.add_argument("action", choices=config.ACTIONS)

    p = _command(sub, "run", cmd_run, "run a command through the proxy",
                 "Run one command with proxy settings in its environment. Handy in basic mode for terminal\n"
                 "tools that ignore the system proxy (git, brew, pip, npm, curl). In all-apps mode you\n"
                 "don't need it.",
                 ("pogo run -- git pull", "pogo run -- brew update"))
    p.add_argument("command", nargs=argparse.REMAINDER, help="the command to run")

    env_examples = (("pogo env | Invoke-Expression            PowerShell: use the proxy in this window",
                     "pogo env --unset | Invoke-Expression    stop using it",
                     "for /f \"delims=\" %i in ('pogo env --shell cmd') do %i    cmd.exe")
                    if system.IS_WINDOWS else
                    ('eval "$(pogo env)"          use the proxy in this shell', 'eval "$(pogo env --unset)"  stop using it'))
    p = _command(sub, "env", cmd_env, "proxy variables for your shell",
                 "Print proxy environment variables so terminal tools in this shell use pogo.",
                 env_examples)
    p.add_argument("--unset", action="store_true", help="print commands that remove the variables")
    p.add_argument("--shell", choices=sorted(SHELL_SYNTAX),
                   help="syntax to print (default: powershell on Windows, sh elsewhere)")

    _command(sub, "doctor", cmd_doctor, "diagnose problems",
             "Check everything that commonly goes wrong (settings, password, proxy reachability, DNS,\n"
             "the all-apps helper, leftovers, Proxifier running at the same time) and say how to fix it.",
             ("pogo doctor",))
    _command(sub, "reset", cmd_reset, "emergency undo",
             "Emergency undo: stop pogo and remove every network change it made (routes, virtual\n"
             "interface, system proxy), even after a crash. May ask for admin rights.",
             ("pogo reset",))

    # advanced
    p = _command(sub, "start", cmd_start, "start only the local proxy",
                 "Start only the local proxy, without capturing anything. It runs in this terminal unless\n"
                 "you give -d. Most people want 'pogo on' instead.",
                 ("pogo start -d", "pogo start --system"))
    p.add_argument("-d", "--daemon", action="store_true", help="run in the background")
    p.add_argument("--system", action="store_true", help="also set the system proxy while running")
    _command(sub, "stop", cmd_stop, "stop the local proxy",
             "Stop the local proxy (and the all-apps capture with it). 'pogo off' does this and more.",
             ("pogo stop",))
    p = _command(sub, "system", cmd_system, "system proxy on/off",
                 f"Point the {proxy_settings} at pogo, or switch that off. 'off' only removes\n"
                 "settings that point at pogo.",
                 ("pogo system on", "pogo system off"))
    p.add_argument("state", choices=["on", "off"])
    tun_examples = (("pogo tun              (in an Administrator terminal)", "pogo tun --dry-run    show what it would do",
                     "pogo tun cleanup      remove leftovers (Administrator)")
                    if system.IS_WINDOWS else
                    ("sudo pogo tun", "sudo pogo tun --dry-run    show what it would do",
                     "sudo pogo tun cleanup     remove leftovers"))
    p = _command(sub, "tun", cmd_tun, "all-apps capture in the foreground",
                 "Run the all-apps capture in this terminal until Ctrl-C (needs admin rights). 'pogo on'\n"
                 "runs it in the background for you; this is for debugging.",
                 tun_examples)
    p.add_argument("action", nargs="?", choices=["start", "cleanup"], default="start")
    p.add_argument("--dry-run", action="store_true", help="print the commands without running them")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--report-fd", type=int, default=-1, help=argparse.SUPPRESS)
    p.add_argument("--report-file", help=argparse.SUPPRESS)
    p.add_argument("--log-file", help=argparse.SUPPRESS)
    p = _command(sub, "helper", cmd_helper, "install the all-apps helper",
                 "Install the open-source helper all-apps mode uses: tun2socks, plus the Wintun driver DLL\n"
                 "on Windows. Official builds, checked against known checksums, downloaded through your\n"
                 "proxy when pogo is running.",
                 ("pogo helper",))
    p.add_argument("--reinstall", action="store_true", help="install again even if it's present")

    COMMANDS.clear()
    COMMANDS.update(k for k in PARSERS if " " not in k)
    return parser


def show_help(topic: list[str]) -> int:
    if not topic:
        print_home()
        return 0
    parser = PARSERS.get(" ".join(topic[:2])) or PARSERS.get(topic[0])
    if parser is None:
        return _unknown(topic[0])
    parser.print_help()
    return 0


def _unknown(word: str) -> int:
    ui.error(f"'{word}' isn't a pogo command")
    guess = difflib.get_close_matches(word, sorted(COMMANDS), n=1, cutoff=0.6)
    if guess:
        print(f"  did you mean:  pogo {guess[0]}", file=sys.stderr)
    print(ui.dim("  see every command:  pogo"), file=sys.stderr)
    return 2


def _dispatch(argv: list[str]) -> int:
    parser = build_parser()
    if not argv or argv[0] in ("-h", "--help"):
        print_home()
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print(f"pogo {__version__}")
        return 0
    if argv[0] == "help":
        return show_help(argv[1:])
    if argv[0] not in COMMANDS:
        return _unknown(argv[0])
    args = parser.parse_args(argv)
    return args.fn(args) or 0


def main(argv: list[str] | None = None) -> None:
    ui.setup_console()
    try:
        code = _dispatch(sys.argv[1:] if argv is None else argv)
    except KeyboardInterrupt:
        print()
        code = 130
    sys.exit(code)
