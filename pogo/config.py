"""Config file, runtime state files, and macOS Keychain password storage."""
from __future__ import annotations

import copy
import json
import os
import pwd
import subprocess
from pathlib import Path


def _user_home() -> Path:
    # `sudo pogo ...` must still use the invoking user's config, not root's.
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


HOME_DIR = Path(os.environ.get("POGO_HOME") or _user_home() / ".config" / "pogo")
CONFIG_FILE = HOME_DIR / "config.json"
PID_FILE = HOME_DIR / "pogo.pid"
LOG_FILE = HOME_DIR / "pogo.log"      # connections handled by the local proxy
TUN_FILE = HOME_DIR / "tun.json"         # exists while all-apps capture is running
TUN_STOP = HOME_DIR / "tun.stop"         # `pogo off` creates it to ask the capture to stop
TUN_LOG = HOME_DIR / "tun.log"           # log of the all-apps capture (root process)
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
        data = json.loads(CONFIG_FILE.read_text())
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
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(CONFIG_FILE)


def is_configured(cfg: dict) -> bool:
    return bool(cfg["upstream"].get("host"))


def listen_addr(cfg: dict) -> str:
    return f"{cfg['listen']['host']}:{cfg['listen']['port']}"


# --- runtime state ------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by root (the capture process)
        return True


def _is_pogo_process(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return "pogo.cli" in out


def running_pid() -> int | None:
    """PID of the local proxy, if it is running (guards against stale PID files)."""
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) and _is_pogo_process(pid) else None


def read_tun_state() -> dict | None:
    """State of the all-apps capture, or None when it isn't running."""
    try:
        data = json.loads(TUN_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    return data if pid_alive(data["pid"]) else None


def tun_interface() -> str | None:
    """Real network interface the local proxy must pin its own connections to while all-apps
    capture runs (otherwise they would loop back into the tunnel). Cheap enough to read per
    connection, which also means there is no stale window when the capture starts."""
    state = read_tun_state()
    iface = state.get("interface") if state else None
    return iface if isinstance(iface, str) else None


# --- password (kept in the login Keychain, never in config.json) -----------

def _account(up: dict) -> str:
    return f"{up['username']}@{up['host']}:{up['port']}"


def get_password(up: dict) -> str:
    env = os.environ.get("POGO_PASSWORD")  # handy for scripts / tests
    if env is not None:
        return env
    if not up.get("username"):
        return ""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _account(up), "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.rstrip("\n") if r.returncode == 0 else ""


def set_password(up: dict, password: str) -> None:
    r = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
         "-a", _account(up), "-w", password],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"could not store password in Keychain: {r.stderr.strip()}")


def delete_password(up: dict) -> None:
    if up.get("username") and up.get("host"):
        subprocess.run(
            ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", _account(up)],
            capture_output=True,
        )


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
            self._cfg = load()
            self._pw = get_password(self._cfg["upstream"])
            self._mtime = mtime
        return self._cfg, self._pw
