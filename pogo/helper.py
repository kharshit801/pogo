"""Installs the helpers all-apps mode needs: tun2socks (both platforms) and, on Windows, the
Wintun driver DLL. Official builds are downloaded (through pogo's own proxy when it's running)
and checked against pinned SHA-256 checksums before anything is written."""
from __future__ import annotations

import hashlib
import io
import os
import platform
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import config, system

TUN2SOCKS_VERSION = "v2.7.0"
TUN2SOCKS_URL = "https://github.com/xjasonlyu/tun2socks/releases/download/{version}/{asset}"
# (os, arch) -> (release asset, SHA-256 of the zip, file inside the zip)
TUN2SOCKS_BUILDS = {
    ("darwin", "arm64"): ("tun2socks-darwin-arm64.zip",
                          "7c5ebfe2ffb60ecf6e958cc5bbf3e06e74b8b33575ffbb4ba4f6f785a647f1ad", "tun2socks-darwin-arm64"),
    ("darwin", "amd64"): ("tun2socks-darwin-amd64.zip",
                          "6e654da8bab9ca1645862f0e251a69980e0966680713011feea7b1e5901b2a95", "tun2socks-darwin-amd64"),
    ("windows", "amd64"): ("tun2socks-windows-amd64.zip",
                           "c5d46e9452f6c9cc7c15ab9158d6d6a0169ceecd6bca019ce476b49337d2be43",
                           "tun2socks-windows-amd64.exe"),
    ("windows", "arm64"): ("tun2socks-windows-arm64.zip",
                           "74497771068da13f42921adfc540f2abb9ac822404582c8cbe34d30e8c0ea1f5",
                           "tun2socks-windows-arm64.exe"),
    ("windows", "386"): ("tun2socks-windows-386.zip",
                         "ad870a2ca99d617e004ec09f8f5cca2c134b4d16ae12b4eac0ff6d9fe05d9044", "tun2socks-windows-386.exe"),
}
WINTUN_URL = "https://www.wintun.net/builds/wintun-0.14.1.zip"
WINTUN_SHA256 = "07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51"  # from wintun.net
WINTUN_DLL = {"amd64": "wintun/bin/amd64/wintun.dll", "arm64": "wintun/bin/arm64/wintun.dll",
              "386": "wintun/bin/x86/wintun.dll"}

ARCH_NAMES = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64",
              "x86": "386", "i386": "386", "i686": "386"}


class HelperError(Exception):
    pass


def bin_dir() -> Path:
    return config.HOME_DIR / "bin"


def _windows_native_arch() -> str | None:
    """The OS architecture, even when Python itself runs emulated (x64 Python on ARM64 Windows)."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.IsWow64Process2.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ushort),
                                             ctypes.POINTER(ctypes.c_ushort)]
        process_machine, native_machine = ctypes.c_ushort(), ctypes.c_ushort()
        if kernel32.IsWow64Process2(kernel32.GetCurrentProcess(), ctypes.byref(process_machine),
                                    ctypes.byref(native_machine)):
            return {0x8664: "amd64", 0xAA64: "arm64", 0x014C: "386"}.get(native_machine.value)
    except (AttributeError, OSError):
        pass
    return None


def machine() -> tuple[str, str] | None:
    """('windows'|'darwin', 'amd64'|'arm64'|'386') for this computer, or None if unsupported."""
    if system.IS_WINDOWS:
        arch = _windows_native_arch() or ARCH_NAMES.get(
            (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE")
             or platform.machine()).lower())
        return ("windows", arch) if arch else None
    if system.IS_MAC:
        arch = ARCH_NAMES.get(platform.machine().lower())
        return ("darwin", arch) if arch else None
    return None


def describe() -> str:
    parts = [f"tun2socks {TUN2SOCKS_VERSION} from GitHub"]
    if system.IS_WINDOWS:
        parts.append("the Wintun driver DLL from wintun.net")
    return "one-time download of " + " and ".join(parts) + ", checksums verified"


def _download(url: str, proxy: str | None) -> bytes:
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": "pogo-helper-installer"})
    try:
        with opener.open(request, timeout=90) as response:
            return response.read()
    except OSError as e:
        raise HelperError(f"couldn't download {url}: {getattr(e, 'reason', e)}")


def _verify(data: bytes, expected: str, name: str) -> None:
    if hashlib.sha256(data).hexdigest() != expected:
        raise HelperError(f"{name} didn't match its known checksum, so nothing was installed "
                          "(a proxy or antivirus may have altered the download)")


def _write(target: Path, payload: bytes, executable: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".download")
    tmp.write_bytes(payload)
    if executable and not system.IS_WINDOWS:
        os.chmod(tmp, 0o755)
    try:
        system.replace_file(tmp, target)
    except PermissionError:
        system.remove_file(tmp)
        raise HelperError(f"{target.name} is in use. Turn pogo off first (pogo off), then try again")


def install(proxy: str | None = None, report: Callable[[str, str], None] = lambda kind, msg: None) -> Path:
    """Download, verify and install the helpers. Returns the tun2socks path."""
    found = machine()
    if found not in TUN2SOCKS_BUILDS:
        raise HelperError("there's no tun2socks build for this computer's processor")
    asset, checksum, member = TUN2SOCKS_BUILDS[found]
    report("info", f"downloading {asset}")
    data = _download(TUN2SOCKS_URL.format(version=TUN2SOCKS_VERSION, asset=asset), proxy)
    _verify(data, checksum, asset)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        payload = archive.read(member)
    target = bin_dir() / ("tun2socks.exe" if system.IS_WINDOWS else "tun2socks")
    _write(target, payload, executable=True)
    installed = [target]
    if system.IS_WINDOWS:
        report("info", "downloading the Wintun driver (wintun.net)")
        data = _download(WINTUN_URL, proxy)
        _verify(data, WINTUN_SHA256, "wintun.zip")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            payload = archive.read(WINTUN_DLL[found[1]])
        dll = bin_dir() / "wintun.dll"
        _write(dll, payload, executable=False)
        installed.append(dll)
    time.sleep(0.5)  # antivirus sometimes deletes freshly downloaded files right away
    for path in installed:
        if not path.exists():
            raise HelperError(f"{path.name} disappeared right after it was saved, probably removed by "
                              f"antivirus. Allow {path} in your antivirus, then run: pogo helper")
    return target
