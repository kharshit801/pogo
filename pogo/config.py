"""Config file, runtime state files, and password storage
(macOS Keychain, or Windows Credential Manager)."""
from __future__ import annotations

import copy
import json
import os
import socket
import time
from pathlib import Path

from . import system


def _user_home() -> Path:
    # `sudo pogo ...` on macOS must still use the invoking user's config, not root's.
    if not system.IS_WINDOWS:
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user and os.geteuid() == 0:
            import pwd
            try:
                return Path(pwd.getpwnam(sudo_user).pw_dir)
            except KeyError:
                pass
    return Path.home()


def _default_home() -> Path:
    if system.IS_WINDOWS:
        # Deliberately not under AppData: the Microsoft Store Python silently redirects AppData
        # writes to a private folder that tun2socks and the admin helper would never see.
        return _user_home() / ".pogo"
    return _user_home() / ".config" / "pogo"


HOME_DIR = Path(os.environ.get("POGO_HOME") or _default_home())
CONFIG_FILE = HOME_DIR / "config.json"
PID_FILE = HOME_DIR / "pogo.pid"
LOG_FILE = HOME_DIR / "pogo.log"      # connections handled by the local proxy
TUN_FILE = HOME_DIR / "tun.json"      # exists while all-apps capture is running
TUN_STOP = HOME_DIR / "tun.stop"      # `pogo off` creates it to ask the capture to stop
TUN_LOG = HOME_DIR / "tun.log"        # log of the all-apps capture (admin process)
KEYCHAIN_SERVICE = "pogo"

ACTIONS = ("proxy", "direct", "block")

# Local / campus-internal ranges go direct by default. Edit with `pogo rules`.
DEFAULT_DIRECT = [
    "localhost",
    "*.local",
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
]

DEFAULTS = {
    "upstream": {"type": "http", "host": "", "port": 8080, "username": ""},
    "listen": {"host": "127.0.0.1", "port": 8899},
    "default_action": "proxy",
    "sniff": True,  # all-apps mode: recover hostnames from TLS SNI / HTTP Host so domain rules work
    "rules": [{"pattern": p, "action": "direct"} for p in DEFAULT_DIRECT],
}


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def save(cfg: dict) -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    system.replace_file(tmp, CONFIG_FILE)


def is_configured(cfg: dict) -> bool:
    return bool(cfg["upstream"].get("host"))


def listen_addr(cfg: dict) -> str:
    return f"{cfg['listen']['host']}:{cfg['listen']['port']}"


# --- runtime state ------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    return system.pid_alive(pid)


def _is_pogo_process(pid: int) -> bool:
    command = system.process_command(pid)
    if system.IS_WINDOWS:
        return "python" in Path(command).name.lower()  # the executable; the PID file says which one
    return "pogo.cli" in command


def running_pid() -> int | None:
    """PID of the local proxy, if it is running (guards against stale PID files)."""
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) and _is_pogo_process(pid) else None


def read_tun_state() -> dict | None:
    """State of the all-apps capture, or None when it isn't running."""
    data = None
    for _ in range(5):
        try:
            data = json.loads(TUN_FILE.read_text(encoding="utf-8"))
            break
        except FileNotFoundError:
            return None
        except PermissionError:  # Windows: the capture is replacing the file right now
            time.sleep(0.02)
        except (OSError, ValueError):
            return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    return data if pid_alive(data["pid"]) else None


def tun_bind_index() -> int | None:
    """Index of the real network interface the local proxy must pin its own connections to
    while all-apps capture runs (otherwise they would loop back into the tunnel). Read per
    connection, so there is no stale window when the capture starts."""
    state = read_tun_state()
    if not state:
        return None
    index = state.get("ifindex")
    if isinstance(index, int) and index > 0:
        return index
    name = state.get("interface")  # state written by older versions only had the name (macOS)
    if isinstance(name, str) and hasattr(socket, "if_nametoindex"):
        try:
            return socket.if_nametoindex(name)
        except OSError:
            return None
    return None


# --- password (macOS Keychain / Windows Credential Manager, never in config.json) ----

def _account(up: dict) -> str:
    return f"{up['username']}@{up['host']}:{up['port']}"


def get_password(up: dict) -> str:
    env = os.environ.get("POGO_PASSWORD")  # handy for scripts and tests
    if env is not None:
        return env
    if not up.get("username"):
        return ""
    if system.IS_WINDOWS:
        from . import wincred
        return wincred.read(_account(up))
    r = system.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _account(up), "-w"])
    return r.stdout.rstrip("\n") if r.returncode == 0 else ""


def set_password(up: dict, password: str) -> None:
    if system.IS_WINDOWS:
        from . import wincred
        wincred.write(_account(up), up["username"], password)
        return
    r = system.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                    "-a", _account(up), "-w", password])
    if r.returncode != 0:
        raise RuntimeError(f"could not store password in Keychain: {r.stderr.strip()}")


def delete_password(up: dict) -> None:
    if not (up.get("username") and up.get("host")):
        return
    if system.IS_WINDOWS:
        from . import wincred
        wincred.delete(_account(up))
        return
    system.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _account(up)])


class ConfigStore:
    """Hands the server the current config, reloading when the file changes
    so `pogo rules add ...` takes effect without a restart."""

    def __init__(self, fixed: tuple[dict, str] | None = None):
        self._fixed = fixed
        self._cfg: dict | None = None
        self._pw = ""
        self._mtime: int | None = None

    def get(self) -> tuple[dict, str]:
        if self._fixed:
            return self._fixed
        try:
            mtime = CONFIG_FILE.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if self._cfg is None or mtime != self._mtime:
            try:
                self._cfg = load()
            except (PermissionError, ValueError):  # mid-write on Windows: keep the previous copy
                if self._cfg is not None:
                    return self._cfg, self._pw
                raise
            self._pw = get_password(self._cfg["upstream"])
            self._mtime = mtime
        return self._cfg, self._pw
