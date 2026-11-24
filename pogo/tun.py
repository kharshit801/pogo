"""All-apps capture: makes pogo work for every app, like Proxifier.

    every app ─► virtual interface ─► tun2socks ─► pogo local proxy (rules, login) ─► proxy / direct
                                                       └ its own sockets are pinned to the real
                                                         network card, so nothing loops back.

macOS: the utun233 interface, set up with `ifconfig` and `route`, started through sudo.
Windows: a Wintun adapter called "pogo", set up with `netsh` in the non-persistent route store
(a restart always clears it), started through a UAC prompt.

An admin worker sets everything up and reports progress, then checks once a second and removes
everything it added when `pogo off` asks (a stop file), the local proxy stops, or the network
changes.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import config, sysproxy, system
from .launch import self_command
from .rules import bypass_list

TUN_ADDR = "198.18.0.1"     # benchmark range: never real internet space
CAPTURE_NETS = ("0.0.0.0/1", "128.0.0.0/1")   # together: everything; more specific than "default"

Report = Callable[[str, str], None]   # (kind, message); kind: ok | warn | info | fail


def _out(cmd: list[str]) -> str:
    r = system.run(cmd)
    return (r.stdout or "") + (r.stderr or "")


def _is_v4(s: str) -> bool:
    try:
        return ipaddress.ip_address(s).version == 4
    except ValueError:
        return False


def resolve_ipv4(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)})
    except socket.gaierror:
        return []


# --- the tun2socks helper ------------------------------------------------------------------

def find_tun2socks() -> str | None:
    own = config.HOME_DIR / "bin" / ("tun2socks.exe" if system.IS_WINDOWS else "tun2socks")
    if own.exists() and (system.IS_WINDOWS or os.access(own, os.X_OK)):
        return str(own)
    if system.IS_WINDOWS:
        return None  # only pogo's own copy, which sits next to its own wintun.dll
    found = shutil.which("tun2socks")
    if found:
        return found
    homes = [os.path.expanduser("~")]
    if os.environ.get("SUDO_USER"):
        import pwd
        try:
            homes.append(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
        except KeyError:
            pass
    candidates = ["/opt/homebrew/bin/tun2socks", "/usr/local/bin/tun2socks"]
    for home in homes:
        candidates += [f"{home}/.local/bin/tun2socks", f"{home}/go/bin/tun2socks"]
    return next((c for c in candidates if os.access(c, os.X_OK)), None)


def missing_helpers() -> list[str]:
    missing = [] if find_tun2socks() else ["tun2socks"]
    if system.IS_WINDOWS and not (config.HOME_DIR / "bin" / "wintun.dll").exists():
        missing.append("wintun.dll")
    return missing


def _loglevel_args(binary: str) -> list[str]:
    """Quiet tun2socks down (it logs every dropped UDP flow otherwise), using a level name that
    this build lists in its own help text, e.g. "Log level [debug|info|warn|error|silent]"."""
    m = re.search(r"log ?level.*?\[([a-z|]+)\]", _out([binary, "--help"]), re.I | re.S)
    levels = m.group(1).lower().split("|") if m else []
    for level in ("error", "warning", "warn"):
        if level in levels:
            return ["--loglevel", level]
    return []


# --- platform backends ---------------------------------------------------------------------

class MacNet:
    """macOS: the utun233 interface, `ifconfig`, `route`, `netstat` and `scutil`."""
    device = "utun233"

    def __init__(self):
        self.tun_index = None

    def primary_route(self) -> tuple[str | None, str | None]:
        """(interface, gateway) of the primary IPv4 default route. Read from the routing table,
        so pogo's own 0/1 + 128/1 capture routes don't hide it."""
        for line in _out(["netstat", "-rn", "-f", "inet"]).splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "default" and "I" not in parts[2]:  # I = interface-scoped
                return parts[3], parts[1] if _is_v4(parts[1]) else None
        return None, None

    def network_ok(self, iface: str, gateway: str | None) -> bool:
        return self.primary_route() == (iface, gateway)

    def label(self, iface: str) -> str:
        return iface

    def ifindex(self, iface: str) -> int:
        try:
            return socket.if_nametoindex(iface)
        except OSError:
            return 0

    def via_gateway(self, ip: str) -> bool:
        """True if ip is reached through a router (so the capture routes would grab it).
        Addresses on your own subnet keep their more specific local route."""
        return "gateway:" in _out(["route", "-n", "get", ip])

    def dns_servers(self) -> list[str]:
        found = []
        for m in re.finditer(r"nameserver\[\d+\]\s*:\s*(\S+)", _out(["scutil", "--dns"])):
            ip = m.group(1)
            if _is_v4(ip) and not ipaddress.ip_address(ip).is_loopback and ip not in found:
                found.append(ip)
        return found

    def tun2socks_cmd(self, binary: str, listen: str) -> list[str]:
        # Two dashes: tun2socks 2.7+ parses POSIX-style flags; older builds accept them too.
        return [binary, "--device", self.device, "--proxy", f"socks5://{listen}"]

    def wait_device(self, proc: subprocess.Popen) -> str | None:
        for _ in range(50):
            if proc.poll() is not None:
                return "tun2socks exited right away"
            if system.run(["ifconfig", self.device]).returncode == 0:
                return None
            time.sleep(0.1)
        return f"the virtual interface {self.device} never appeared"

    def configure_commands(self) -> list[list[str]]:
        return [["ifconfig", self.device, "inet", TUN_ADDR, TUN_ADDR, "up"]]

    def exception_route(self, ip: str, iface: str, gateway: str | None) -> tuple[list[str], list[str]]:
        via = [gateway] if gateway else ["-interface", iface]
        return ["route", "-n", "add", "-host", ip, *via], ["route", "-n", "delete", "-host", ip]

    def extra_routes(self, iface: str, gateway: str | None = None) -> list[tuple[list[str], list[str]]]:
        routes = [(["route", "-n", "add", "-net", net, "-interface", iface], ["route", "-n", "delete", "-net", net])
                  for net in ("224.0.0.0/4", "169.254.0.0/16")]
        routes.append((["route", "-n", "add", "-host", "255.255.255.255", "-interface", iface],
                       ["route", "-n", "delete", "-host", "255.255.255.255"]))
        # pogo pins its own sockets to the real interface (IP_BOUND_IF). Those lookups only use
        # routes scoped to that interface, and the capture routes would leave them nowhere to
        # go ("Network is unreachable"). A default route scoped to the real interface fixes that
        # without affecting anything else.
        via = [gateway] if gateway else ["-interface", iface]
        routes.append((["route", "-n", "add", "-ifscope", iface, "default", *via],
                       ["route", "-n", "delete", "-ifscope", iface, "default"]))
        return routes

    def capture_routes(self) -> list[tuple[list[str], list[str]]]:
        return [(["route", "-n", "add", "-net", net, TUN_ADDR], ["route", "-n", "delete", "-net", net])
                for net in CAPTURE_NETS]

    def route_present(self, add: list[str]) -> bool:
        return False  # `route add` itself refuses an existing route ("File exists")

    def route_added(self, add: list[str], result) -> bool:
        return result is not None and result.returncode == 0  # includes "already exists": not ours

    def leftovers(self) -> list[str]:
        found = []
        if system.run(["ifconfig", self.device]).returncode == 0:
            found.append(f"{self.device} interface")
        if re.search(rf"^(0/1|128(\.0)?/1)\s+{re.escape(TUN_ADDR)}", _out(["netstat", "-rn", "-f", "inet"]), re.M):
            found.append("capture routes")
        return found

    _DELETE_ARG = re.compile(r"^(-host|-net|-interface|-ifscope|default|[0-9A-Fa-f.:/]+|[a-z]+[0-9]+)$")

    def valid_delete(self, cmd) -> bool:
        return (isinstance(cmd, list) and cmd[:3] == ["route", "-n", "delete"] and len(cmd) <= 6
                and all(isinstance(a, str) and self._DELETE_ARG.match(a) for a in cmd[3:]))

    def cleanup(self, state: dict) -> None:
        for cmd in state.get("routes", []):  # the file is user-writable: only well-formed deletes
            if self.valid_delete(cmd):
                system.run(cmd)
        for net in CAPTURE_NETS:
            system.run(["route", "-n", "delete", "-net", net])
        system.run(["pkill", "-f", f"tun2socks.*{self.device}"])


class WinNet:
    """Windows: a Wintun adapter called "pogo"; routes changed with netsh in the "active" store,
    which is never written to disk, so a restart always clears them."""
    device = "pogo"

    def __init__(self):
        self.tun_index = None

    @staticmethod
    def _api():
        from . import winnet
        return winnet

    def primary_route(self) -> tuple[str | None, str | None]:
        index, gateway = self._api().primary_route()
        return (str(index) if index else None), gateway

    def network_ok(self, iface: str, gateway: str | None) -> bool:
        try:
            return self._api().has_default_route(int(iface), gateway)
        except OSError:
            return True  # a failed read isn't a network change

    def label(self, iface: str) -> str:
        return self._api().interface_name(int(iface))

    def ifindex(self, iface: str) -> int:
        return int(iface)

    def via_gateway(self, ip: str) -> bool:
        return self._api().via_gateway(ip)

    def dns_servers(self) -> list[str]:
        return [ip for ip in self._api().dns_servers() if _is_v4(ip)]

    def tun2socks_cmd(self, binary: str, listen: str) -> list[str]:
        return [binary, "--device", f"tun://{self.device}", "--proxy", f"socks5://{listen}"]

    def wait_device(self, proc: subprocess.Popen) -> str | None:
        for _ in range(150):  # the first run installs the Wintun driver, which takes a few seconds
            if proc.poll() is not None:
                return "tun2socks exited right away"
            adapter = self._api().find_adapter(self.device)
            if adapter:
                self.tun_index = adapter.index
                return None
            time.sleep(0.1)
        return f"the virtual network adapter '{self.device}' never appeared"

    def _tun(self) -> str:
        return str(self.tun_index) if self.tun_index is not None else f"<{self.device} adapter>"

    def configure_commands(self) -> list[list[str]]:
        base = ["netsh", "interface", "ipv4", "set", "address"]
        tail = ["source=static", f"address={TUN_ADDR}", "mask=255.255.255.0"]
        # Tried in order: some Windows builds reject a spelling the next one accepts.
        return [base + [f"name={self._tun()}", *tail, "store=active"],
                base + [f"name={self._tun()}", *tail],
                base + [f"name={self.device}", *tail]]

    @staticmethod
    def route_cmd(verb: str, prefix: str, ifindex, nexthop: str) -> list[str]:
        cmd = ["netsh", "interface", "ipv4", verb, "route", f"prefix={prefix}",
               f"interface={ifindex}", f"nexthop={nexthop}"]
        if verb == "add":
            cmd.append("metric=1")
        return cmd + ["store=active"]

    def exception_route(self, ip: str, iface: str, gateway: str | None) -> tuple[list[str], list[str]]:
        hop = gateway or "0.0.0.0"
        return self.route_cmd("add", f"{ip}/32", iface, hop), self.route_cmd("delete", f"{ip}/32", iface, hop)

    def extra_routes(self, iface: str, gateway: str | None = None) -> list:
        return []  # Windows keeps its own on-link routes, and IP_UNICAST_IF uses the adapter's default route

    def capture_routes(self) -> list[tuple[list[str], list[str]]]:
        return [(self.route_cmd("add", net, self._tun(), TUN_ADDR), self.route_cmd("delete", net, self._tun(), TUN_ADDR))
                for net in CAPTURE_NETS]

    def route_present(self, add: list[str]) -> bool:
        """Checked before adding: a route that's already there isn't pogo's to remove later."""
        fields = dict(arg.split("=", 1) for arg in add if "=" in arg)
        try:
            return self._api().route_exists(fields["prefix"], int(fields["interface"]))
        except (KeyError, ValueError, OSError):
            return False

    def route_added(self, add: list[str], result) -> bool:
        # netsh's exit codes aren't documented, so check the routing table instead.
        return self.route_present(add)

    def leftovers(self) -> list[str]:
        found = []
        try:
            api = self._api()
            if any(r.nexthop == TUN_ADDR and r.mask == "128.0.0.0" for r in api.routes()):
                found.append("capture routes")
            if api.find_adapter(self.device):
                found.append(f"'{self.device}' network adapter")
        except OSError:
            pass
        return found

    _DELETE_ARG = re.compile(r"^(prefix=[0-9./]+|interface=\d+|nexthop=[0-9.]+|store=active)$")

    def valid_delete(self, cmd) -> bool:
        return (isinstance(cmd, list) and cmd[:5] == ["netsh", "interface", "ipv4", "delete", "route"]
                and len(cmd) <= 9 and all(isinstance(a, str) and self._DELETE_ARG.match(a) for a in cmd[5:]))

    def cleanup(self, state: dict) -> None:
        for cmd in state.get("routes", []):  # the file is user-writable: only well-formed deletes
            if self.valid_delete(cmd):
                system.run(cmd)
        try:
            for r in self._api().routes():
                if r.nexthop == TUN_ADDR:
                    prefix = str(ipaddress.ip_network(f"{r.dest}/{r.mask}", strict=False))
                    system.run(self.route_cmd("delete", prefix, r.ifindex, TUN_ADDR))
        except OSError:
            pass
        helper_pid = state.get("helper_pid")
        if isinstance(helper_pid, int) and "tun2socks" in Path(system.process_command(helper_pid)).name.lower():
            system.kill(helper_pid)


def default_net():
    return WinNet() if system.IS_WINDOWS else MacNet()


def leftovers() -> list[str]:
    """Traces of a capture that is no longer running (e.g. after a crash). No admin needed."""
    found = []
    if config.TUN_FILE.exists() and not config.read_tun_state():
        found.append("stale state file")
    return found + default_net().leftovers()


def preflight(cfg: dict, need_admin: bool = True) -> str | None:
    """Why all-apps capture can't start right now, or None if it can."""
    if need_admin and not system.is_admin():
        return "needs admin rights (use 'pogo on', which asks for them)"
    if not config.is_configured(cfg):
        return "no proxy configured yet: run 'pogo setup'"
    missing = missing_helpers()
    if missing:
        return " and ".join(missing) + " not installed: run 'pogo helper'"
    if config.read_tun_state():
        return "all-apps capture is already running"
    if not config.running_pid():
        return "the local proxy isn't running: start it with 'pogo start -d'"
    if not default_net().primary_route()[0]:
        return "no network connection (no default route)"
    return None


# --- files written by the admin process into the user's (writable) config folder ----------

def _owner() -> tuple[int, int] | None:
    name = os.environ.get("SUDO_USER")
    if system.IS_WINDOWS or not name or os.geteuid() != 0:
        return None
    import pwd
    try:
        pw = pwd.getpwnam(name)
        return pw.pw_uid, pw.pw_gid
    except KeyError:
        return None


def _write_state(data: dict) -> None:
    """Atomic write via a fresh temp file + rename, so a planted symlink is never followed."""
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.HOME_DIR, prefix=".tun.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o644)  # the local proxy (not root) reads it
        system.replace_file(tmp, config.TUN_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _open_log() -> int:
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if config.TUN_LOG.stat().st_size > 5_000_000:
            os.replace(config.TUN_LOG, config.TUN_LOG.with_suffix(".log.1"))
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(config.TUN_LOG, flags, 0o644)
    owner = _owner()
    if owner:
        os.fchown(fd, *owner)  # so you can read and delete it without sudo
    return fd


def _output_stream():
    """Where tun2socks' own output goes: our log when we have one."""
    try:
        sys.stdout.fileno()
        return sys.stdout
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL


# --- the capture itself -------------------------------------------------------------------

class Capture:
    def __init__(self, cfg: dict, report: Report, *, system_proxy: bool, dry_run: bool = False, net=None):
        self.cfg, self.report, self.system_proxy, self.dry = cfg, report, system_proxy, dry_run
        self.net = net or default_net()
        self.listen = config.listen_addr(cfg)
        self.proc: subprocess.Popen | None = None
        self.undo: list[list[str]] = []   # commands that delete what we added
        self.sysproxy_set = False
        self.iface: str | None = None
        self.gateway: str | None = None
        self.ifindex = 0
        self.label = ""
        self.stopping = False

    def _run(self, cmd: list[str]):
        if self.dry:
            print("    $", " ".join(cmd))
            return None
        r = system.run(cmd)
        if r.returncode != 0:
            print(f"$ {' '.join(cmd)}  ->  {((r.stdout or '') + (r.stderr or '')).strip()}", flush=True)
        return r

    def _route(self, add: list[str], delete: list[str], adopt: bool = False) -> bool:
        """Add a route and remember how to remove it. A route that already exists is left alone
        and never removed later, except capture routes (adopt=True): those can only be pogo's own
        leftovers from a crash."""
        if not self.dry and self.net.route_present(add):
            if adopt:
                self.undo.append(delete)
                self._save_state()
            return True
        result = self._run(add)
        if not self.dry and not self.net.route_added(add, result):
            return False
        self.undo.append(delete)
        self._save_state()
        return True

    def _save_state(self) -> None:
        if not self.dry:
            _write_state({"pid": os.getpid(), "interface": self.iface, "ifindex": self.ifindex,
                          "label": self.label, "gateway": self.gateway, "device": self.net.device,
                          "tun_index": self.net.tun_index, "helper_pid": self.proc.pid if self.proc else None,
                          "listen": self.listen, "routes": self.undo, "system_proxy": self.sysproxy_set,
                          "started": int(time.time())})

    def setup(self) -> bool:
        up = self.cfg["upstream"]
        self.iface, self.gateway = self.net.primary_route()
        if not self.iface:
            self.report("fail", "no network connection (no default route)")
            return False
        self.ifindex = self.net.ifindex(self.iface)
        self.label = self.net.label(self.iface)
        self.report("info", f"real network: {self.label} via {self.gateway or 'direct link'}")
        keep = resolve_ipv4(up["host"]) or ([up["host"]] if _is_v4(up["host"]) else [])
        if not keep:
            self.report("fail", f"can't resolve the proxy address {up['host']!r}")
            return False
        keep += [ip for ip in self.net.dns_servers() if ip not in keep]
        keep = [ip for ip in keep if self.dry or self.net.via_gateway(ip)]

        # The state file goes first: from now on the local proxy pins its own connections
        # to the real network card, so nothing it sends can loop back into the tunnel.
        self._save_state()

        binary = find_tun2socks() or ("tun2socks.exe" if system.IS_WINDOWS else "tun2socks")
        cmd = self.net.tun2socks_cmd(binary, self.listen) + ([] if self.dry else _loglevel_args(binary))
        if self.dry:
            print("    $", " ".join(cmd))
        else:
            out = _output_stream()
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, **system.hidden())
            problem = self.net.wait_device(self.proc)
            if problem:
                self.report("fail", f"{problem} (see {config.TUN_LOG})")
                return False
            self._save_state()

        for attempt in self.net.configure_commands():
            result = self._run(attempt)
            if self.dry or (result is not None and result.returncode == 0):
                break
        else:
            self.report("fail", f"couldn't configure the virtual interface {self.net.device}")
            return False
        self.report("ok", f"virtual network interface {self.net.device} created")

        # Exceptions first, so nothing that must stay direct is ever captured:
        # the proxy server itself, DNS servers (the tunnel carries TCP only), multicast/link-local.
        for ip in keep:
            self._route(*self.net.exception_route(ip, self.iface, self.gateway))
        for add, delete in self.net.extra_routes(self.iface, self.gateway):
            self._route(add, delete)
        for add, delete in self.net.capture_routes():
            if not self._route(add, delete, adopt=True):
                self.report("fail", "couldn't install the capture routes")
                return False
        self.report("ok", "all traffic now flows through pogo (proxy server, DNS and local network stay direct)")

        if self.system_proxy:
            host, port = self.cfg["listen"]["host"], self.cfg["listen"]["port"]
            errors = [] if self.dry else sysproxy.enable(host, port, bypass_list(self.cfg))
            self.sysproxy_set = True
            self._save_state()
            if errors:
                self.report("warn", "system proxy: " + "; ".join(errors))
            else:
                self.report("ok", "system proxy set too, so apps that support it send hostnames directly")
        return True

    def watch(self, interval: float = 1.0) -> str:
        """Block until something ends the capture; return the reason."""
        while True:
            if self.stopping:
                return "stop signal received"
            if config.TUN_STOP.exists():
                return "turned off with 'pogo off'"
            if self.proc is not None and self.proc.poll() is not None:
                return "tun2socks exited unexpectedly"
            if not config.running_pid():
                return "the local proxy stopped"
            if not self.net.network_ok(self.iface, self.gateway):
                return "the network changed (run 'pogo on' again)"
            time.sleep(interval)

    def teardown(self) -> None:
        if not self.dry:
            for cmd in reversed(self.undo):      # routes first: traffic stops entering the tunnel
                system.run(cmd)
            if self.sysproxy_set:
                sysproxy.disable(match=self.listen)
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            system.remove_file(config.TUN_FILE)   # last: the proxy stops pinning its sockets
            system.remove_file(config.TUN_STOP)
        self.undo.clear()


def _install_stop_signals(capture: Capture) -> None:
    def stop(*_):
        capture.stopping = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, stop)


# --- entry points ---------------------------------------------------------------------------

def run_foreground(cfg: dict, report: Report, dry_run: bool = False) -> int:
    """`sudo pogo tun` (or `pogo tun` in an Administrator terminal): run in this terminal until
    Ctrl-C. Useful for debugging."""
    if not dry_run:
        problem = preflight(cfg)
        if problem:
            report("fail", problem)
            return 1
    capture = Capture(cfg, report, system_proxy=False, dry_run=dry_run)
    _install_stop_signals(capture)
    try:
        if not capture.setup():
            return 1
        if dry_run:
            report("info", "dry run: nothing was changed")
            return 0
        report("info", "running; press Ctrl-C to stop and remove everything")
        report("info", capture.watch())
        return 0
    finally:
        capture.teardown()
        if not dry_run:
            report("ok", "capture removed; network back to normal")


def start_background(cfg: dict, report: Report) -> int:
    """macOS, as root (via sudo): launch the capture worker and relay its progress through a pipe."""
    problem = preflight(cfg)
    if problem:
        report("fail", problem)
        return 1
    system.remove_file(config.TUN_STOP)
    log_fd = _open_log()
    read_end, write_end = os.pipe()
    try:
        worker = subprocess.Popen(self_command("tun", "--worker", "--report-fd", str(write_end),
                                               home=str(config.HOME_DIR)),
                                  stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                                  pass_fds=(write_end,), start_new_session=True)
    finally:
        os.close(write_end)
        os.close(log_fd)
    ready = False
    with os.fdopen(read_end) as progress:
        for line in progress:
            kind, _, msg = line.rstrip("\n").partition("\t")
            if kind == "ready":
                ready = True
                break
            report(kind, msg)
    if not ready:
        try:
            worker.wait(5)  # it is shutting down after a failed setup: reap it
        except subprocess.TimeoutExpired:
            pass
        report("fail", f"all-apps capture didn't start; details in {config.TUN_LOG}")
    return 0 if ready else 1  # on success the worker keeps running on its own


def read_progress(path: Path, offset: int) -> tuple[list[tuple[str, str]], int]:
    """New complete "kind<TAB>message" lines of a progress file, from offset on."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n") + 1
    lines = [line.partition("\t")[::2] for line in data[:end].decode("utf-8", "replace").splitlines()]
    return lines, offset + end


def start_elevated(cfg: dict, report: Report) -> int:
    """Windows half of `pogo on`: start the admin worker (through a UAC prompt unless this
    terminal is already elevated) and relay its progress. Progress travels through a file,
    because an elevated process can't share a pipe with a normal one."""
    problem = preflight(cfg, need_admin=False)
    if problem:
        report("fail", problem)
        return 1
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    progress = config.HOME_DIR / "tun.progress"
    system.remove_file(progress)
    system.remove_file(config.TUN_STOP)
    cmd = self_command("tun", "--worker", "--report-file", str(progress), "--log-file", str(config.TUN_LOG),
                       home=str(config.HOME_DIR), windowless=True)
    handle = None
    if system.is_admin():
        worker = system.spawn_background(cmd, subprocess.DEVNULL)

        def alive() -> bool:
            return worker.poll() is None
    else:
        report("info", "Windows asks for permission to change network settings (UAC prompt)")
        try:
            handle = system.elevate(cmd)
        except PermissionError:
            report("fail", "admin permission was declined, so pogo can't capture all apps")
            return 1
        except OSError as e:
            report("fail", f"couldn't start the admin helper: {e}")
            return 1

        def alive() -> bool:
            return system.wait_process(handle, 0) is None
    try:
        offset, deadline = 0, time.monotonic() + 120
        while True:
            running = alive()
            lines, offset = read_progress(progress, offset)
            for kind, msg in lines:
                if kind == "ready":
                    return 0
                report(kind, msg)
            if not running:
                break
            if time.monotonic() > deadline:
                config.TUN_STOP.touch()  # don't leave a half-started capture behind
                report("fail", "the admin helper didn't finish starting within 2 minutes")
                break
            time.sleep(0.2)
    finally:
        system.close_handle(handle)
    report("fail", f"all-apps capture didn't start; details in {config.TUN_LOG}")
    return 1


def _redirect_output(path: str) -> None:
    """The Windows worker runs under pythonw.exe (no console): send everything to the log."""
    try:
        stream = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = sys.stderr = stream


def run_worker(cfg: dict, report_fd: int = -1, report_file: str | None = None,
               log_file: str | None = None) -> int:
    """The long-running admin process. Output goes to tun.log; progress to `pogo on`."""
    if log_file:
        _redirect_output(log_file)
    channel = {"fd": report_fd, "file": report_file}

    def report(kind: str, msg: str) -> None:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {kind}: {msg}", flush=True)
        if channel["fd"] >= 0:
            try:
                os.write(channel["fd"], f"{kind}\t{msg}\n".encode())
            except OSError:
                pass
        if channel["file"]:
            try:
                with open(channel["file"], "a", encoding="utf-8") as f:
                    f.write(f"{kind}\t{msg}\n")
            except OSError:
                pass

    if report_file:  # Windows: this process is the admin one, so it checks for itself
        problem = preflight(cfg)
        if problem:
            report("fail", problem)
            return 1
    capture = Capture(cfg, report, system_proxy=not system.IS_WINDOWS)
    _install_stop_signals(capture)
    try:
        if not capture.setup():
            return 1
        report("ready", "")
        if channel["fd"] >= 0:
            os.close(channel["fd"])
        channel.update(fd=-1, file=None)
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} stopping: {capture.watch()}", flush=True)
        return 0
    finally:
        capture.teardown()
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} capture removed", flush=True)


def cleanup(report: Report) -> int:
    """Admin: remove every trace of a capture, even one whose process crashed or was killed."""
    if not system.is_admin():
        report("fail", "needs admin rights: run 'pogo reset'")
        return 1
    try:
        state = json.loads(config.TUN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    pid = state.get("pid")
    if isinstance(pid, int) and system.pid_alive(pid):
        config.HOME_DIR.mkdir(parents=True, exist_ok=True)
        config.TUN_STOP.touch()  # a live worker cleans up after itself
        for _ in range(60):
            if not system.pid_alive(pid):
                break
            time.sleep(0.1)
        if system.pid_alive(pid):
            system.kill(pid)
    default_net().cleanup(state)
    if state.get("system_proxy") and isinstance(state.get("listen"), str):
        sysproxy.disable(match=state["listen"])
    system.remove_file(config.TUN_FILE)
    system.remove_file(config.TUN_STOP)
    report("ok", "capture routes, tunnel helper and state removed")
    return 0
