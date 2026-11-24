"""Rule matching: first matching rule wins, otherwise the default action."""
from __future__ import annotations

import fnmatch
import ipaddress


def matches(pattern: str, host: str) -> bool:
    """Pattern forms:
    *                 everything
    10.0.0.0/8        CIDR range (also a bare IP)
    example.com       the domain and all its subdomains
    *.example.com     subdomains (and example.com itself)
    192.168.*, *.edu  any other glob
    """
    p = pattern.strip().lower()
    h = host.strip().lower().rstrip(".")
    if p == "*":
        return True
    try:
        net = ipaddress.ip_network(p, strict=False)
    except ValueError:
        net = None
    if net is not None:
        try:
            return ipaddress.ip_address(h) in net
        except ValueError:
            return False
    if any(c in p for c in "*?["):
        return fnmatch.fnmatchcase(h, p) or (p.startswith("*.") and h == p[2:])
    return h == p or h.endswith("." + p)


def decide(cfg: dict, host: str) -> tuple[str, str]:
    """Returns (action, matched_pattern). matched_pattern is 'default' if none."""
    for rule in cfg["rules"]:
        if matches(rule["pattern"], host):
            return rule["action"], rule["pattern"]
    return cfg["default_action"], "default"


def bypass_list(cfg: dict) -> list[str]:
    """Patterns of 'direct' rules: handed to the system proxy and NO_PROXY as exceptions."""
    return [r["pattern"] for r in cfg["rules"] if r["action"] == "direct" and r["pattern"] != "*"]


def normalize_pattern(text: str) -> str:
    """Accept what people paste: 'https://www.youtube.com/watch?v=1' -> 'www.youtube.com'."""
    text = text.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    if "/" in text and not _looks_like_cidr(text):
        text = text.split("/", 1)[0]
    return text.lower()


def _looks_like_cidr(text: str) -> bool:
    try:
        ipaddress.ip_network(text, strict=False)
        return True
    except ValueError:
        return False


def is_ip_rule(pattern: str) -> bool:
    """True for CIDR / bare-IP patterns (those can be decided without knowing the hostname)."""
    try:
        ipaddress.ip_network(pattern.strip(), strict=False)
        return True
    except ValueError:
        return False
