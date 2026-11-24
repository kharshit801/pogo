"""Windows system proxy (Settings → Network & internet → Proxy), through WinINet.

* pogo saves your previous proxy settings (including automatic setup scripts) when it turns
  the proxy on, and puts them back on `pogo off`.
* If Windows restarts while the proxy is on, a RunOnce entry puts them back at your next
  sign-in, so a restart can never leave apps pointing at a proxy that isn't running.
* Only "http=" and "https=" are set: Windows apps (Chrome, Edge) read a "socks=" entry as
  SOCKS4, which pogo doesn't speak.
* The bypass list uses wildcards like "10.*" because WinINet has no reliable CIDR support.
  Anything not bypassed still reaches pogo, whose own rules send it direct when they should.
"""
from __future__ import annotations

import ctypes
import ipaddress
import json
import sys
from ctypes import wintypes
from pathlib import Path

from . import config

INTERNET_OPTION_REFRESH = 37
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_PER_CONNECTION_OPTION = 75
PER_CONN_FLAGS = 1
PER_CONN_PROXY_SERVER = 2
PER_CONN_PROXY_BYPASS = 3
PER_CONN_AUTOCONFIG_URL = 4
PER_CONN_FLAGS_UI = 10
PROXY_TYPE_DIRECT = 0x1
PROXY_TYPE_PROXY = 0x2

SERVICE = "Windows proxy settings"
BACKUP_FILE = config.HOME_DIR / "winproxy-backup.json"
RESTORE_SCRIPT = config.HOME_DIR / "restore-proxy.py"
RUNONCE_KEY = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
RUNONCE_VALUE = "!pogo-restore-proxy"  # the "!" means: delete only after it ran successfully
POLICY_KEY = r"Software\Policies\Microsoft\Windows\CurrentVersion\Internet Settings"


class _Value(ctypes.Union):
    _fields_ = [("dwValue", wintypes.DWORD), ("pszValue", ctypes.c_void_p), ("ftValue", wintypes.FILETIME)]


class _Option(ctypes.Structure):
    _fields_ = [("dwOption", wintypes.DWORD), ("Value", _Value)]


class _OptionList(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("pszConnection", wintypes.LPWSTR),
                ("dwOptionCount", wintypes.DWORD), ("dwOptionError", wintypes.DWORD),
                ("pOptions", ctypes.POINTER(_Option))]


def _wininet():
    dll = ctypes.WinDLL("wininet", use_last_error=True)
    dll.InternetSetOptionW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dll.InternetSetOptionW.restype = wintypes.BOOL
    dll.InternetQueryOptionW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                         ctypes.POINTER(wintypes.DWORD)]
    dll.InternetQueryOptionW.restype = wintypes.BOOL
    return dll


# --- pure helpers (tested on every platform) -------------------------------------------

def server_string(host: str, port: int) -> str:
    return f"http={host}:{port};https={host}:{port}"


def first_address(server: str) -> str:
    """'http=1.2.3.4:80;https=1.2.3.4:443' -> the HTTPS (or else first) proxy address."""
    entries = {}
    for part in server.split(";"):
        part = part.strip()
        if not part:
            continue
        scheme, sep, address = part.partition("=")
        entries[scheme.lower() if sep else "*"] = address if sep else scheme
    for key in ("https", "http", "*"):
        if entries.get(key):
            return entries[key]
    return next(iter(entries.values()), "")


def _bypass_entries(pattern: str) -> list[str]:
    p = pattern.strip().lower()
    if not p or p == "*":
        return []
    try:
        net = ipaddress.ip_network(p, strict=False)
    except ValueError:
        net = None
    if net is not None:
        if net.version == 6:
            return []
        if net.prefixlen == 32:
            return [str(net.network_address)]
        for boundary in (8, 16, 24):
            if net.prefixlen <= boundary:
                subnets = [net] if net.prefixlen == boundary else list(net.subnets(new_prefix=boundary))
                if len(subnets) > 64:
                    return []  # too many entries; pogo's rules still handle these addresses
                keep = boundary // 8
                return [".".join(str(s.network_address).split(".")[:keep]) + ".*" for s in subnets]
        hosts = list(net)
        return [str(h) for h in hosts] if len(hosts) <= 16 else []
    if any(c in p for c in "?["):
        return []  # WinINet only understands *
    if p.startswith("*."):
        return [p, p[2:]]
    if "*" in p:
        return [p]
    return [p, "*." + p]  # pogo's "example.com" also covers its subdomains


def bypass_string(patterns: list[str]) -> str:
    entries: list[str] = []
    for pattern in patterns:
        entries += _bypass_entries(pattern)
    entries.append("<local>")  # names without a dot (intranet hosts)
    return ";".join(dict.fromkeys(entries))


def points_at(settings: dict, address: str) -> bool:
    return bool(settings.get("flags", 0) & PROXY_TYPE_PROXY) and address in settings.get("server", "")


# --- reading and writing the settings ------------------------------------------------

def query() -> dict:
    """Current settings: {"flags", "server", "bypass", "autoconfig"}."""
    dll = _wininet()
    options = (_Option * 4)()
    for i, option in enumerate((PER_CONN_FLAGS_UI, PER_CONN_PROXY_SERVER, PER_CONN_PROXY_BYPASS,
                                PER_CONN_AUTOCONFIG_URL)):
        options[i].dwOption = option
    request = _OptionList(ctypes.sizeof(_OptionList), None, 4, 0, options)
    size = wintypes.DWORD(ctypes.sizeof(request))
    if not dll.InternetQueryOptionW(None, INTERNET_OPTION_PER_CONNECTION_OPTION, ctypes.byref(request),
                                    ctypes.byref(size)):
        options[0].dwOption = PER_CONN_FLAGS  # FLAGS_UI needs IE8+; fall back
        if not dll.InternetQueryOptionW(None, INTERNET_OPTION_PER_CONNECTION_OPTION, ctypes.byref(request),
                                        ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    result = {"flags": int(options[0].Value.dwValue)}
    for index, key in ((1, "server"), (2, "bypass"), (3, "autoconfig")):
        pointer = options[index].Value.pszValue
        result[key] = ctypes.wstring_at(pointer) if pointer else ""
        if pointer:
            kernel32.GlobalFree(pointer)
    return result


def _apply(flags: int, server: str, bypass: str, autoconfig: str | None = None) -> None:
    entries = [(PER_CONN_FLAGS, flags), (PER_CONN_PROXY_SERVER, server), (PER_CONN_PROXY_BYPASS, bypass)]
    if autoconfig is not None:
        entries.append((PER_CONN_AUTOCONFIG_URL, autoconfig))
    options = (_Option * len(entries))()
    buffers = []
    for i, (option, value) in enumerate(entries):
        options[i].dwOption = option
        if option == PER_CONN_FLAGS:
            options[i].Value.dwValue = value
        else:
            buffer = ctypes.create_unicode_buffer(value or "")
            buffers.append(buffer)
            options[i].Value.pszValue = ctypes.cast(buffer, ctypes.c_void_p).value
    request = _OptionList(ctypes.sizeof(_OptionList), None, len(entries), 0, options)
    dll = _wininet()
    if not dll.InternetSetOptionW(None, INTERNET_OPTION_PER_CONNECTION_OPTION, ctypes.byref(request),
                                  ctypes.sizeof(request)):
        raise ctypes.WinError(ctypes.get_last_error())
    # tell running apps to re-read the settings
    dll.InternetSetOptionW(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
    dll.InternetSetOptionW(None, INTERNET_OPTION_REFRESH, None, 0)


def policy_machine_wide() -> bool:
    """True when Group Policy makes proxy settings per-machine: per-user changes then do nothing."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, POLICY_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "ProxySettingsPerUser")
            return int(value) == 0
    except (OSError, ValueError):
        return False


# --- the sysproxy interface (same functions as the macOS implementation) ---------------

def services() -> list[str]:
    return [SERVICE]


def web_proxy(service: str = SERVICE) -> tuple[bool, str]:
    return effective()


def effective() -> tuple[bool, str]:
    try:
        settings = query()
    except OSError:
        return False, ""
    if settings["flags"] & PROXY_TYPE_PROXY and settings["server"]:
        return True, first_address(settings["server"])
    return False, ""


def pointing_to(match: str) -> list[str]:
    try:
        return [SERVICE] if points_at(query(), match) else []
    except OSError:
        return []


def enable(host: str, port: int, bypass: list[str]) -> list[str]:
    if policy_machine_wide():
        return ["Group Policy makes proxy settings machine-wide on this PC, so pogo can't set them for "
                "your account. All-apps mode (pogo on) doesn't need them."]
    address = f"{host}:{port}"
    try:
        current = query()
        if not points_at(current, address):
            _save_backup(current)
        _apply(PROXY_TYPE_DIRECT | PROXY_TYPE_PROXY, server_string(host, port), bypass_string(bypass))
    except OSError as e:
        return [f"couldn't change the Windows proxy settings: {e}"]
    _register_restore()
    return []


def disable(match: str | None = None) -> list[str]:
    """Put the settings back as they were before pogo (or plain direct). With match, only when
    the current settings point at that address, so a proxy that isn't pogo's is left alone."""
    try:
        current = query()
        if match is not None and not points_at(current, match):
            return []
        backup = _load_backup()
        if backup and not (match and points_at(backup, match)):
            _apply(backup["flags"], backup["server"], backup["bypass"], backup["autoconfig"])
        else:
            _apply(PROXY_TYPE_DIRECT, "", "")
    except OSError as e:
        return [f"couldn't change the Windows proxy settings: {e}"]
    try:
        BACKUP_FILE.unlink()
    except OSError:
        pass
    _unregister_restore()
    return []


# --- backup and the restart safety net ------------------------------------------------

def _save_backup(settings: dict) -> None:
    config.HOME_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_FILE.write_text(json.dumps(settings), encoding="utf-8")


def _load_backup() -> dict | None:
    try:
        data = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (isinstance(data, dict) and isinstance(data.get("flags"), int)):
        return None
    return {"flags": data["flags"], "server": str(data.get("server") or ""),
            "bypass": str(data.get("bypass") or ""), "autoconfig": str(data.get("autoconfig") or "")}


def _register_restore() -> None:
    import winreg
    from .launch import PKG_PARENT
    try:
        RESTORE_SCRIPT.write_text(
            "# Written by pogo. Puts your proxy settings back if Windows restarted while pogo was on.\n"
            "import sys\n"
            f"sys.path.insert(0, {PKG_PARENT!r})\n"
            "from pogo import winproxy\n"
            "winproxy.restore_after_restart()\n", encoding="utf-8")
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        command = f'"{pythonw if pythonw.exists() else python}" "{RESTORE_SCRIPT}"'
        if len(command) >= 260:
            return  # RunOnce ignores longer command lines; `pogo off` still restores as usual
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUNONCE_KEY) as key:
            winreg.SetValueEx(key, RUNONCE_VALUE, 0, winreg.REG_SZ, command)
    except OSError:
        pass  # best effort


def _unregister_restore() -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUNONCE_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUNONCE_VALUE)
    except OSError:
        pass


def restore_after_restart() -> None:
    """Runs once at sign-in after a restart. If pogo isn't running but the proxy still points
    at it, put the previous settings back."""
    cfg = config.load()
    if config.running_pid():
        _register_restore()  # pogo is running after all: keep the safety net armed
        return
    disable(match=config.listen_addr(cfg))
