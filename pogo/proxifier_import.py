"""Read a Proxifier for Mac profile (.ppx) and turn it into pogo settings."""
from __future__ import annotations

import ipaddress
import os
import plistlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import system
from .config import DEFAULT_DIRECT

CONTAINER = Path.home() / "Library/Containers/com.initex.proxifier.v3.macos/Data"
PREFS_FILE = CONTAINER / "Library/Preferences/com.initex.proxifier.v3.macos.plist"
if system.IS_WINDOWS:
    # Proxifier v4 for Windows: per-user profiles, plus an optional machine-wide Default.ppx
    PROFILE_DIRS = [Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Proxifier4" / "Profiles",
                    Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "Proxifier"]
else:
    PROFILE_DIRS = [CONTAINER / "Profiles"]

# Proxifier "HTTPS" proxies are ordinary HTTP proxies used with CONNECT (what pogo does).
TYPE_MAP = {"HTTPS": "http", "HTTP": "http", "SOCKS5": "socks5"}
ACTION_MAP = {"direct": "direct", "block": "block", "proxy": "proxy", "chain": "proxy"}


class ImportProblem(Exception):
    pass


@dataclass
class Profile:
    name: str
    path: Path
    active: bool = False
    note: str = ""   # why it's marked: "active in Proxifier" or "most recently changed"


@dataclass
class Converted:
    upstream: dict | None
    proxy_label: str                  # e.g. "HTTPS 10.0.0.1:3128"
    rules: list[dict]                 # imported site/IP rules (without pogo's defaults)
    default_action: str
    skipped_apps: list[tuple[str, str]] = field(default_factory=list)  # (app, proxifier action)
    notes: list[str] = field(default_factory=list)


def active_profile_name() -> str | None:
    if system.IS_WINDOWS:
        return None  # Proxifier for Windows doesn't record it in a documented place
    try:
        return plistlib.loads(PREFS_FILE.read_bytes()).get("SettingsActiveProfile")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def list_profiles() -> list[Profile]:
    files: list[Path] = []
    for folder in PROFILE_DIRS:
        try:
            files += sorted(folder.glob("*.ppx"))
        except PermissionError:
            raise ImportProblem("access to Proxifier's files was blocked. Allow it if a popup asks, or "
                                "export the profile from Proxifier and run: pogo import <file.ppx>")
    if not files:
        return []
    active = active_profile_name()
    if active is not None:
        return [Profile(p.stem, p, p.stem == active, "active in Proxifier" if p.stem == active else "")
                for p in files]
    newest = max(files, key=lambda p: p.stat().st_mtime)  # best guess on Windows
    return [Profile(p.stem, p, p == newest, "most recently changed" if p == newest else "") for p in files]


def locate(choice: str | None = None) -> Profile:
    """A profile by name, by path, or Proxifier's active one."""
    if choice and (choice.endswith(".ppx") or "/" in choice):
        path = Path(choice).expanduser()
        if not path.is_file():
            raise ImportProblem(f"no such file: {path}")
        return Profile(path.stem, path)
    profiles = list_profiles()
    if not profiles:
        raise ImportProblem("no Proxifier profiles found on this Mac. In Proxifier use "
                            "File → Export Profile…, then run: pogo import <file.ppx>")
    if choice:
        for p in profiles:
            if p.name.lower() == choice.lower():
                return p
        names = ", ".join(p.name for p in profiles)
        raise ImportProblem(f"no Proxifier profile named {choice!r} (found: {names})")
    return next((p for p in profiles if p.active), profiles[0])


def _split(text: str) -> list[str]:
    return [t.strip().strip('"') for t in text.replace(",", ";").split(";") if t.strip()]


def _target_patterns(text: str) -> list[str]:
    patterns = []
    for token in _split(text):
        if token.startswith("%") and token.endswith("%"):
            continue  # Proxifier macros like %ComputerName%
        if "-" in token:  # IP range: 10.1.1.1-10.1.1.255
            start, _, end = token.partition("-")
            try:
                nets = ipaddress.summarize_address_range(ipaddress.ip_address(start.strip()),
                                                         ipaddress.ip_address(end.strip()))
                patterns += [str(n) for n in nets]
                continue
            except ValueError:
                pass  # a hostname with a dash
        patterns.append(token.lower())
    return patterns


def convert(path: Path) -> Converted:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as e:
        raise ImportProblem(f"can't read {path.name}: {e}")

    proxies = {}
    for p in root.iterfind("./ProxyList/Proxy"):
        auth = p.find("Authentication")
        user = ""
        if auth is not None and auth.get("enabled") == "true":
            user = (auth.findtext("Username") or "").strip()
        try:
            port = int((p.findtext("Port") or "0").strip())
        except ValueError:
            port = 0
        proxies[p.get("id")] = {"kind": (p.get("type") or "").upper(),
                                "host": (p.findtext("Address") or "").strip(), "port": port, "username": user}

    rules, skipped_apps, notes = [], [], []
    default_action, default_proxy = "proxy", None
    for r in root.iterfind("./RuleList/Rule"):
        if r.get("enabled", "true") != "true":
            continue
        name = (r.findtext("Name") or "").strip()
        apps = (r.findtext("Applications") or "").strip()
        targets = (r.findtext("Targets") or "").strip()
        ports = (r.findtext("Ports") or "").strip()
        act = r.find("Action")
        kind = (act.get("type") if act is not None else "Direct") or "Direct"
        action = ACTION_MAP.get(kind.lower(), "proxy")
        proxy_ref = (act.text or "").strip() if act is not None else ""
        if kind.lower() == "chain":
            notes.append(f"rule '{name}' uses a proxy chain; pogo sends it through your one proxy")

        if not (apps or targets or ports):  # the catch-all "Default" rule
            default_action = action
            default_proxy = proxy_ref if kind.lower() == "proxy" else None
        elif apps:
            skipped_apps += [(app, action) for app in _split(apps)]
        elif ports:
            notes.append(f"rule '{name}' depends on ports ({ports}); pogo can't match ports, skipped")
        else:
            rules += [{"pattern": p, "action": action} for p in _target_patterns(targets)]

    chosen = proxies.get(default_proxy) if default_proxy else None
    if chosen is None and proxies:
        chosen = next(iter(proxies.values()))
    upstream, label = None, "none"
    if chosen:
        label = f"{chosen['kind']} {chosen['host']}:{chosen['port']}"
        ptype = TYPE_MAP.get(chosen["kind"])
        if ptype and chosen["host"] and chosen["port"]:
            upstream = {"type": ptype, "host": chosen["host"], "port": chosen["port"],
                        "username": chosen["username"]}
        else:
            notes.append(f"proxy type {chosen['kind']} isn't supported (use HTTP/HTTPS or SOCKS5)")
    if upstream:
        own = upstream["host"].lower()
        if any(r["pattern"] == own for r in rules):
            rules = [r for r in rules if r["pattern"] != own]
            notes.append(f"skipped the rule for the proxy server itself ({own}): pogo always "
                         "connects to its proxy directly")
    if len(proxies) > 1:
        notes.append(f"the profile has {len(proxies)} proxies; pogo uses one ({label})")
    resolve = root.find("./Options/Resolve/ViaProxy")
    if resolve is not None and resolve.get("enabled") == "true":
        notes.append("Proxifier resolved hostnames through the proxy; pogo uses your Mac's DNS "
                     "(apps that support the system proxy still let the proxy resolve names)")

    first_rule_per_app: dict[str, str] = {}
    for app, action in skipped_apps:  # Proxifier: the first matching rule wins
        first_rule_per_app.setdefault(app, action)
    return Converted(upstream, label, _dedupe(rules), default_action,
                     list(first_rule_per_app.items()), notes)


def _dedupe(rules: list[dict]) -> list[dict]:
    seen, out = set(), []
    for rule in rules:
        if rule["pattern"] not in seen:
            seen.add(rule["pattern"])
            out.append(rule)
    return out


def merged_rules(imported: list[dict]) -> list[dict]:
    """Imported rules first (they're your choices), then pogo's local-network defaults."""
    have = {r["pattern"] for r in imported}
    return imported + [{"pattern": p, "action": "direct"} for p in DEFAULT_DIRECT if p not in have]
