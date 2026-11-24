"""All-apps capture: makes pogo work for every app, like Proxifier.

    every app ─► utun233 ─► tun2socks ─► pogo local proxy (rules, login) ─► campus proxy / direct
                                               └ its own sockets are pinned to the real network
                                                 card, so they never loop back into the tunnel.

It runs as root. `pogo on` starts it in the background through sudo and shows its progress.
`pogo off` asks it to stop by creating a file (no password needed). It also stops by itself
when the local proxy dies or the network changes, and always removes every route it added.
"""
from __future__ import annotations

import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from typing import Callable

from . import config, sysproxy
from .launch import self_command
from .rules import bypass_list

TUN_DEVICE = "utun233"      # high unit number so it doesn't collide with VPN interfaces
TUN_ADDR = "198.18.0.1"     # benchmark range: never real internet space
CAPTURE_NETS = ("0.0.0.0/1", "128.0.0.0/1")   # together: everything; more specific than "default"
TUN2SOCKS_MODULE = "github.com/xjasonlyu/tun2socks/v2@latest"

Report = Callable[[str, str], None]   # (kind, message); kind: ok | warn | info | fail


def _out(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout + r.stderr


def _is_v4(s: str) -> bool:
    try:
        return ipaddress.ip_address(s).version == 4
    except ValueError:
        return False


# --- discovery ----------------------------------------------------------------

def find_tun2socks() -> str | None:
    found = shutil.which("tun2socks")
    if found:
        return found
    homes = [os.path.expanduser("~")]
    if os.environ.get("SUDO_USER"):
        try:
            homes.append(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
        except KeyError:
            pass
    candidates = ["/opt/homebrew/bin/tun2socks", "/usr/local/bin/tun2socks"]
    for home in homes:
        candidates += [f"{home}/.local/bin/tun2socks", f"{home}/go/bin/tun2socks"]
    return next((c for c in candidates if os.access(c, os.X_OK)), None)


def primary_route() -> tuple[str | None, str | None]:
    """(interface, gateway) of the primary IPv4 default route. Read from the routing table, so
    our own 0/1 + 128/1 capture routes don't hide it."""
    for line in _out(["netstat", "-rn", "-f", "inet"]).splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "default" and "I" not in parts[2]:  # I = interface-scoped
            return parts[3], parts[1] if _is_v4(parts[1]) else None
    return None, None


def dns_servers() -> list[str]:
    found = []
    for m in re.finditer(r"nameserver\[\d+\]\s*:\s*(\S+)", _out(["scutil", "--dns"])):
        ip = m.group(1)
        if _is_v4(ip) and not ipaddress.ip_address(ip).is_loopback and ip not in found:
            found.append(ip)
    return found


def via_gateway(ip: str) -> bool:
    """True if ip is currently reached through a router (so the capture routes would grab it).
    Addresses on your own subnet keep their more specific local route and need no exception."""
    return "gateway:" in _out(["route", "-n", "get", ip])


def resolve_ipv4(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)})
    except socket.gaierror:
        return []


def _loglevel_args(binary: str) -> list[str]:
    """Quiet tun2socks down (it logs every dropped UDP flow otherwise), using a level name that
    this build lists in its own help text, e.g. "Log level [debug|info|warn|error|silent]"."""
    m = re.search(r"log ?level.*?\[([a-z|]+)\]", _out([binary, "--help"]), re.I | re.S)
    levels = m.group(1).lower().split("|") if m else []
    for level in ("error", "warning", "warn"):
        if level in levels:
            return ["--loglevel", level]
    return []


def leftovers() -> list[str]:
    """Traces of a capture that is no longer running (e.g. after a crash). No root needed."""
    found = []
    if config.TUN_FILE.exists() and not config.read_tun_state():
        found.append("stale state file")
    if subprocess.run(["ifconfig", TUN_DEVICE], capture_output=True).returncode == 0:
        found.append(f"{TUN_DEVICE} interface")
    if re.search(rf"^(0/1|128(\.0)?/1)\s+{re.escape(TUN_ADDR)}", _out(["netstat", "-rn", "-f", "inet"]), re.M):
        found.append("capture routes")
    return found


def preflight(cfg: dict) -> str | None:
    """Why all-apps capture can't start right now, or None if it can."""
    if os.geteuid() != 0:
        return "needs admin rights (use 'pogo on', which asks for your password)"
    if not config.is_configured(cfg):
        return "no proxy configured yet: run 'pogo setup'"
    if not find_tun2socks():
        return "the tun2socks helper isn't installed: run 'pogo helper'"
    if config.read_tun_state():
        return "all-apps capture is already running"
    if not config.running_pid():
        return "the local proxy isn't running: start it with 'pogo start -d'"
    if not primary_route()[0]:
        return "no network connection (no default route)"
    return None


# --- files written as root into the user's (writable) config folder ---------

def _owner() -> tuple[int, int] | None:
    name = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and name:
        try:
            pw = pwd.getpwnam(name)
            return pw.pw_uid, pw.pw_gid
        except KeyError:
            return None
    return None


def _write_state(data: dict) -> None:
    """Atomic write via a fresh temp file + rename, so a planted symlink is never followed."""
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.HOME_DIR, prefix=".tun.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o644)  # the local proxy (not root) reads it
        os.replace(tmp, config.TUN_FILE)
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
    fd = os.open(config.TUN_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    owner = _owner()
    if owner:
        os.fchown(fd, *owner)  # so you can read and delete it without sudo
    return fd


def _remove(path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# --- the capture itself -------------------------------------------------------

class Capture:
    def __init__(self, cfg: dict, report: Report, *, system_proxy: bool, dry_run: bool = False):
        self.cfg, self.report, self.system_proxy, self.dry = cfg, report, system_proxy, dry_run
        self.listen = config.listen_addr(cfg)
        self.proc: subprocess.Popen | None = None
        self.undo: list[list[str]] = []        # `route delete` commands for what we added
        self.sysproxy_set = False
        self.iface: str | None = None
        self.gateway: str | None = None
        self.stopping = False

    def _run(self, cmd: list[str]) -> bool:
        if self.dry:
            print("    $", " ".join(cmd))
            return True
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"$ {' '.join(cmd)}  ->  {(r.stdout + r.stderr).strip()}", flush=True)
        return r.returncode == 0

    def _route(self, add: list[str], delete: list[str]) -> bool:
        if not self._run(["route", "-n", "add", *add]):
            return False  # includes "already exists": then it's not ours to delete later
        self.undo.append(["route", "-n", "delete", *delete])
        self._save_state()
        return True

    def _save_state(self) -> None:
        if not self.dry:
            _write_state({"pid": os.getpid(), "interface": self.iface, "gateway": self.gateway,
                          "device": TUN_DEVICE, "listen": self.listen, "routes": self.undo,
                          "system_proxy": self.sysproxy_set, "started": int(time.time())})

    def setup(self) -> bool:
        up = self.cfg["upstream"]
        self.iface, self.gateway = primary_route()
        self.report("info", f"real network: {self.iface} via {self.gateway or 'direct link'}")
        keep = resolve_ipv4(up["host"]) or ([up["host"]] if _is_v4(up["host"]) else [])
        if not keep:
            self.report("fail", f"can't resolve the proxy address {up['host']!r}")
            return False
        keep += [ip for ip in dns_servers() if ip not in keep]
        keep = [ip for ip in keep if self.dry or via_gateway(ip)]
        via = [self.gateway] if self.gateway else ["-interface", self.iface]

        # The state file goes first: from now on the local proxy pins its own connections
        # to the real network card, so nothing it sends can loop back into the tunnel.
        self._save_state()

        binary = find_tun2socks() or "tun2socks"
        # Two dashes: tun2socks 2.7+ parses POSIX-style flags; older builds accept them too.
        cmd = [binary, "--device", TUN_DEVICE, "--proxy", f"socks5://{self.listen}",
               *([] if self.dry else _loglevel_args(binary))]
        if self.dry:
            print("    $", " ".join(cmd))
        else:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
            for _ in range(50):
                if self.proc.poll() is not None:
                    self.report("fail", f"tun2socks exited right away (see {config.TUN_LOG})")
                    return False
                if subprocess.run(["ifconfig", TUN_DEVICE], capture_output=True).returncode == 0:
                    break
                time.sleep(0.1)
            else:
                self.report("fail", f"the virtual interface {TUN_DEVICE} never appeared")
                return False
        if not self._run(["ifconfig", TUN_DEVICE, "inet", TUN_ADDR, TUN_ADDR, "up"]):
            self.report("fail", f"couldn't configure {TUN_DEVICE}")
            return False
        self.report("ok", f"virtual network interface {TUN_DEVICE} created")

        # Exceptions first, so nothing that must stay direct is ever captured:
        # the proxy server itself, DNS servers (the tunnel carries TCP only), multicast/link-local.
        for ip in keep:
            self._route(["-host", ip, *via], ["-host", ip])
        for net in ("224.0.0.0/4", "169.254.0.0/16"):
            self._route(["-net", net, "-interface", self.iface], ["-net", net])
        self._route(["-host", "255.255.255.255", "-interface", self.iface], ["-host", "255.255.255.255"])
        for net in CAPTURE_NETS:
            if not self._route(["-net", net, TUN_ADDR], ["-net", net]):
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
            if primary_route() != (self.iface, self.gateway):
                return "the network changed (run 'pogo on' again)"
            time.sleep(interval)

    def teardown(self) -> None:
        if not self.dry:
            for cmd in reversed(self.undo):      # routes first: traffic stops entering the tunnel
                subprocess.run(cmd, capture_output=True)
            if self.sysproxy_set:
                sysproxy.disable(match=self.listen)
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            _remove(config.TUN_FILE)              # last: the proxy stops pinning its sockets
            _remove(config.TUN_STOP)
        self.undo.clear()


def _install_stop_signals(capture: Capture) -> None:
    def stop(*_):
        capture.stopping = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)


# --- entry points ---------------------------------------------------------------

def run_foreground(cfg: dict, report: Report, dry_run: bool = False) -> int:
    """`sudo pogo tun`: run in this terminal until Ctrl-C (useful for debugging)."""
    if not dry_run:
        problem = preflight(cfg)
        if problem:
            report("fail", problem)
            return 1
    capture = Capture(cfg, report, system_proxy=False, dry_run=dry_run)
    _install_stop_signals(capture)
    signal.signal(signal.SIGHUP, lambda *_: setattr(capture, "stopping", True))
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
    """Root half of `pogo on`: launch the capture worker and relay its progress here."""
    problem = preflight(cfg)
    if problem:
        report("fail", problem)
        return 1
    _remove(config.TUN_STOP)
    log_fd = _open_log()
    read_end, write_end = os.pipe()
    try:
        worker = subprocess.Popen(self_command("tun", "--worker", "--report-fd", str(write_end)),
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


def run_worker(cfg: dict, report_fd: int) -> int:
    """The long-running root process. Output goes to tun.log; progress to `pogo on`."""
    channel = {"fd": report_fd}

    def report(kind: str, msg: str) -> None:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {kind}: {msg}", flush=True)
        if channel["fd"] >= 0:
            try:
                os.write(channel["fd"], f"{kind}\t{msg}\n".encode())
            except OSError:
                pass

    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    capture = Capture(cfg, report, system_proxy=True)
    _install_stop_signals(capture)
    try:
        if not capture.setup():
            return 1
        report("ready", "")
        os.close(channel["fd"])
        channel["fd"] = -1
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} stopping: {capture.watch()}", flush=True)
        return 0
    finally:
        capture.teardown()
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} capture removed", flush=True)


_ROUTE_ARG = re.compile(r"^(-host|-net|-interface|[0-9A-Fa-f.:/]+|[a-z]+[0-9]+)$")


def cleanup(report: Report) -> int:
    """Root: remove every trace of a capture, even one whose process crashed or was killed."""
    if os.geteuid() != 0:
        report("fail", "needs admin rights: sudo pogo tun cleanup")
        return 1
    try:
        state = json.loads(config.TUN_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    pid = state.get("pid")
    if isinstance(pid, int) and config.pid_alive(pid):
        os.kill(pid, signal.SIGTERM)  # a live worker cleans up after itself
        for _ in range(50):
            if not config.pid_alive(pid):
                break
            time.sleep(0.1)
    for cmd in state.get("routes", []):  # file is user-writable: only run well-formed route deletes
        if (isinstance(cmd, list) and cmd[:3] == ["route", "-n", "delete"] and len(cmd) <= 6
                and all(isinstance(a, str) and _ROUTE_ARG.match(a) for a in cmd[3:])):
            subprocess.run(cmd, capture_output=True)
    for net in CAPTURE_NETS:
        subprocess.run(["route", "-n", "delete", "-net", net], capture_output=True)
    subprocess.run(["pkill", "-f", f"tun2socks.*{TUN_DEVICE}"], capture_output=True)
    if state.get("system_proxy") and isinstance(state.get("listen"), str):
        sysproxy.disable(match=state["listen"])
    _remove(config.TUN_FILE)
    _remove(config.TUN_STOP)
    report("ok", "capture routes, tunnel helper and state removed")
    return 0
