"""macOS system proxy settings (System Settings → Network → Proxies), via `networksetup`."""
from __future__ import annotations

import subprocess


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["networksetup", *args], capture_output=True, text=True)


def services() -> list[str]:
    out = _run(["-listallnetworkservices"]).stdout.splitlines()[1:]  # line 1 is a legend
    return [s for s in out if s and not s.startswith("*")]  # '*' = disabled service


def _failed(r: subprocess.CompletedProcess) -> str | None:
    text = (r.stdout + r.stderr).strip()
    return text if r.returncode != 0 or "Error" in text else None


def web_proxy(service: str) -> tuple[bool, str]:
    """(enabled, 'host:port') of a service's HTTP proxy."""
    fields = dict(line.split(": ", 1) for line in _run(["-getwebproxy", service]).stdout.splitlines()
                  if ": " in line)
    return fields.get("Enabled") == "Yes", f"{fields.get('Server', '')}:{fields.get('Port', '')}"


def enable(host: str, port: int, bypass: list[str]) -> list[str]:
    """Point HTTP, HTTPS and SOCKS proxies of every network service at host:port.
    Returns a list of error strings (empty on success)."""
    errors = []
    for svc in services():
        cmds = [
            ["-setwebproxy", svc, host, str(port)],
            ["-setsecurewebproxy", svc, host, str(port)],
            ["-setsocksfirewallproxy", svc, host, str(port)],
            ["-setproxybypassdomains", svc, *(bypass or ["Empty"])],
        ]
        for cmd in cmds:
            if err := _failed(_run(cmd)):
                errors.append(f"{svc}: {err}")
                break
    return errors


def disable(match: str | None = None) -> list[str]:
    """Turn the proxies off. With match='host:port', only on services that point there,
    so proxy settings that aren't ours are never touched."""
    errors = []
    for svc in services():
        if match is not None:
            enabled, addr = web_proxy(svc)
            if not (enabled and addr == match):
                continue
        for cmd in ("-setwebproxystate", "-setsecurewebproxystate", "-setsocksfirewallproxystate"):
            if err := _failed(_run([cmd, svc, "off"])):
                errors.append(f"{svc}: {err}")
                break
    return errors


def pointing_to(match: str) -> list[str]:
    """Network services whose HTTP proxy is switched on and points at match ('host:port')."""
    return [svc for svc in services() if web_proxy(svc) == (True, match)]


def effective() -> tuple[bool, str]:
    """What apps see right now (the primary network service), from one fast `scutil --proxy`."""
    fields = {}
    for line in subprocess.run(["scutil", "--proxy"], capture_output=True, text=True).stdout.splitlines():
        key, sep, value = line.strip().partition(" : ")
        if sep:
            fields[key] = value
    for kind in ("HTTPS", "HTTP"):
        if fields.get(f"{kind}Enable") == "1":
            return True, f"{fields.get(f'{kind}Proxy', '')}:{fields.get(f'{kind}Port', '')}"
    return False, ""
