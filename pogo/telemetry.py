"""Anonymous, opt-out usage counting.

pogo sends a tiny signal when it is turned on, and a heartbeat while it keeps running, so the
project can see how many people use it. It is **on by default** and easy to turn off:

    pogo telemetry off          # or set DO_NOT_TRACK=1, or POGO_NO_TELEMETRY=1

What is sent:

- a random install id (a number made up on this computer, not tied to you)
- the pogo version, your operating system and CPU type
- whether you are in all-apps or basic mode

What is never sent: your proxy, username or password, the sites you visit, your files, or your
IP address. Geo lookup is turned off and the IP is overwritten before the event is stored, so a
count is all it can ever be.

The signal is best-effort: it is sent from a background thread with a short timeout, and every
error is ignored, so it can never slow pogo down or stop it working offline.

To receive the events, a PostHog project key is compiled in below. Until one is set, this whole
module does nothing at all.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import threading
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, config

# PostHog project API key: public and write-only, safe to commit. Empty = telemetry off entirely.
POSTHOG_KEY = ""
POSTHOG_HOST = "https://us.i.posthog.com"   # or https://eu.i.posthog.com

ENDPOINT = "/capture/"
TIMEOUT = 5                    # seconds; a slow network must never hold pogo up
HEARTBEAT_SECONDS = 1800       # while pogo runs, count it as active every half hour

NOTICE = (
    "pogo counts anonymous usage (a random id, the version, and your OS) so its author can see\n"
    "how many people use it. No proxy, password, or browsing is ever sent. Turn it off anytime\n"
    "with: pogo telemetry off"
)


def _state_file() -> Path:
    return config.HOME_DIR / "telemetry.json"


def _load() -> dict:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    try:
        config.HOME_DIR.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def configured() -> bool:
    """Is a PostHog key compiled in? If not, telemetry is completely inert."""
    return bool(POSTHOG_KEY)


def _env_opt_out() -> bool:
    # DO_NOT_TRACK is the cross-tool standard (https://consoledonottrack.com); POGO_NO_TELEMETRY
    # is ours, and CI sets it so test runs never reach a real project.
    if os.environ.get("POGO_NO_TELEMETRY", "").strip() not in ("", "0", "false", "False"):
        return True
    return os.environ.get("DO_NOT_TRACK", "").strip() in ("1", "true", "True", "yes")


def enabled() -> bool:
    return configured() and not _env_opt_out() and _load().get("enabled", True)


def set_enabled(on: bool) -> None:
    state = _load()
    state["enabled"] = bool(on)
    _save(state)


def distinct_id() -> str:
    """A random id for this install, made once and then reused."""
    state = _load()
    ident = state.get("id")
    if not isinstance(ident, str) or not ident:
        ident = uuid.uuid4().hex
        state["id"] = ident
        _save(state)
    return ident


def _needs_notice() -> bool:
    return not _load().get("notified", False)


def _mark_notified() -> None:
    state = _load()
    state["notified"] = True
    _save(state)


def current_mode() -> str:
    return "all-apps" if config.read_tun_state() else "basic"


def base_properties() -> dict:
    return {
        "pogo_version": __version__,
        "os": sys.platform,                      # darwin / win32 / linux
        "arch": platform.machine(),              # arm64 / AMD64 / x86_64
        "python": ".".join(map(str, sys.version_info[:2])),
        "$lib": "pogo",
        "$lib_version": __version__,
        "$geoip_disable": True,                  # don't look the IP up for a location
        "$ip": "0.0.0.0",                        # and don't store the real one
    }


def _build(event: str, properties: dict) -> bytes:
    payload = {
        "api_key": POSTHOG_KEY,
        "event": event,
        "distinct_id": distinct_id(),
        "properties": {**base_properties(), **properties},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(payload).encode("utf-8")


def _post(body: bytes) -> None:
    # Ignore any http(s)_proxy in the environment: this is a plain call, not pogo traffic.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(POSTHOG_HOST.rstrip("/") + ENDPOINT, data=body,
                                     headers={"Content-Type": "application/json", "User-Agent": "pogo"})
    try:
        opener.open(request, timeout=TIMEOUT).close()
    except Exception:
        pass   # best-effort: a lost count must never surface as an error


def capture(event: str, properties: dict | None = None) -> threading.Thread | None:
    """Send one event in the background. Returns the thread (handy for tests), or None if off."""
    if not enabled():
        return None
    try:
        body = _build(event, properties or {})
    except Exception:
        return None
    thread = threading.Thread(target=_post, args=(body,), daemon=True)
    thread.start()
    return thread


def record_on(all_apps: bool) -> str | None:
    """Called when pogo is turned on. Counts the session and, the very first time, returns the
    one-off notice for the caller to print."""
    if not enabled():
        return None
    notice = NOTICE if _needs_notice() else None
    if notice:
        _mark_notified()
    capture("pogo on", {"mode": "all-apps" if all_apps else "basic"})
    return notice


def heartbeat() -> None:
    """Called on a timer by the running daemon, so 'active right now' is answerable."""
    capture("pogo heartbeat", {"mode": current_mode()})


def status_lines() -> list[str]:
    """Human-readable status for `pogo telemetry`."""
    if not configured():
        return ["Anonymous usage counting is not built into this copy of pogo."]
    if _env_opt_out():
        var = "POGO_NO_TELEMETRY" if os.environ.get("POGO_NO_TELEMETRY") else "DO_NOT_TRACK"
        return [f"Anonymous usage counting is OFF (turned off by the {var} environment variable)."]
    on = _load().get("enabled", True)
    lines = [f"Anonymous usage counting is {'ON' if on else 'OFF'}."]
    if on:
        lines += [f"  install id: {distinct_id()}  (random, not tied to you)",
                  "  sent: a random id, the version, your OS and CPU, and all-apps vs basic",
                  "  never sent: your proxy, password, the sites you visit, or your IP",
                  "  turn off with: pogo telemetry off"]
    else:
        lines.append("  turn on with: pogo telemetry on")
    return lines
