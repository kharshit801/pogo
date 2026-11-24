"""Local mixed-protocol proxy: accepts HTTP proxy *and* SOCKS5 on one port,
applies rules, and forwards via the upstream (HTTP or SOCKS5) or directly."""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import signal
import socket
import urllib.parse

from .config import ConfigStore, tun_interface
from .rules import decide, is_ip_rule

log = logging.getLogger("pogo")

CONNECT_TIMEOUT = 15
HEAD_TIMEOUT = 30
SNIFF_FIRST, SNIFF_MORE = 0.3, 0.1   # seconds to wait for a client's first bytes
IP_BOUND_IF, IPV6_BOUND_IF = 25, 125  # Darwin socket options: force egress interface
HTTP_METHODS = (b"GET ", b"POST ", b"HEAD ", b"PUT ", b"DELETE ", b"OPTIONS ", b"PATCH ")
HOP_HEADERS = {b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive"}


class UpstreamError(Exception):
    def __init__(self, message: str, code: int | None = None, schemes: list[str] | None = None):
        super().__init__(message)
        self.code = code              # HTTP status from the upstream proxy, when there is one
        self.schemes = schemes or []  # login methods it asked for on 407, e.g. ["basic", "ntlm"]


def describe(e: BaseException) -> str:
    if isinstance(e, asyncio.TimeoutError):
        return "timed out connecting (is the proxy host/port right and reachable?)"
    return str(e) or type(e).__name__


# --- helpers ----------------------------------------------------------------

def split_hostport(s: str, default_port: int) -> tuple[str, int]:
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif s.count(":") == 1:
        host, _, port = s.partition(":")
    else:
        host, port = s, ""
    return host, int(port) if port else default_port


def _fmt_hostport(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def basic_auth_header(up: dict, password: str) -> str | None:
    if not up.get("username"):
        return None
    token = base64.b64encode(f"{up['username']}:{password}".encode()).decode()
    return f"Proxy-Authorization: Basic {token}"


def _log_conn(source: str, host: str, port: int, action: str, rule: str, ip: str | None = None) -> None:
    where = f"{host}:{port}" + (f" [{ip}]" if ip and ip != host else "")
    why = "default" if rule == "default" else f"rule {rule}"
    log.info("%-6s %-44s %s · %s", action.upper(), where, source, why)


async def _reply(writer: asyncio.StreamWriter, code: int, reason: str, body: str = "") -> None:
    data = body.encode()
    writer.write(
        f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\n"
        f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
    )
    await writer.drain()


# --- outbound connections ---------------------------------------------------

async def _connect_bound(host: str, port: int, ifname: str):
    """Connect pinned to a physical interface so we bypass the tunnel (tunnel mode)."""
    loop = asyncio.get_running_loop()
    index = socket.if_nametoindex(ifname)
    last: Exception = OSError(f"cannot resolve {host}")
    for family, type_, proto, _, addr in await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        sock = socket.socket(family, type_, proto)
        try:
            sock.setblocking(False)
            if ipaddress.ip_address(addr[0].split("%")[0]).is_loopback:
                pass  # localhost never enters the tunnel; pinning it to the NIC would break it
            elif family == socket.AF_INET:
                sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, index)
            elif family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, IPV6_BOUND_IF, index)
            await loop.sock_connect(sock, addr)
            return await asyncio.open_connection(sock=sock)
        except OSError as e:
            sock.close()
            last = e
    raise last


async def _connect_tcp(host: str, port: int):
    ifname = tun_interface()
    if ifname:
        return await asyncio.wait_for(_connect_bound(host, port, ifname), CONNECT_TIMEOUT)
    return await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)


async def _tunnel_http(up: dict, pw: str, host: str, port: int):
    r, w = await _connect_tcp(up["host"], up["port"])
    try:
        target = _fmt_hostport(host, port)
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        auth = basic_auth_header(up, pw)
        if auth:
            lines.append(auth)
        w.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await w.drain()
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        code = int(status.split(" ", 2)[1])
        if code != 200:
            schemes = [line.split(b":", 1)[1].strip().split(b" ")[0].decode("latin-1").lower()
                       for line in head.split(b"\r\n")[1:] if line.lower().startswith(b"proxy-authenticate:")]
            hint = ""
            if code == 407:
                hint = (f" (it wants {'/'.join(schemes).upper()} login; pogo only does Basic)"
                        if schemes and "basic" not in schemes else " (wrong username or password)")
            raise UpstreamError(f"upstream proxy replied '{status}'{hint}", code, schemes)
        return r, w
    except BaseException:
        w.close()
        raise


async def _tunnel_socks5(up: dict, pw: str, host: str, port: int):
    r, w = await _connect_tcp(up["host"], up["port"])
    try:
        use_auth = bool(up.get("username"))
        w.write(b"\x05\x02\x00\x02" if use_auth else b"\x05\x01\x00")
        method = (await asyncio.wait_for(r.readexactly(2), CONNECT_TIMEOUT))[1]
        if method == 0x02:
            u, p = up["username"].encode(), pw.encode()
            w.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            if (await r.readexactly(2))[1] != 0:
                raise UpstreamError("SOCKS5 upstream rejected username/password")
        elif method != 0x00:
            raise UpstreamError("SOCKS5 upstream: no acceptable auth method")
        try:
            addr = b"\x01" + ipaddress.IPv4Address(host).packed
        except ValueError:
            try:
                addr = b"\x04" + ipaddress.IPv6Address(host).packed
            except ValueError:
                name = host.encode()
                addr = b"\x03" + bytes([len(name)]) + name
        w.write(b"\x05\x01\x00" + addr + port.to_bytes(2, "big"))
        _, rep, _, atyp = await asyncio.wait_for(r.readexactly(4), CONNECT_TIMEOUT)
        if rep != 0:
            raise UpstreamError(f"SOCKS5 upstream connect failed (code {rep})")
        skip = {1: 4, 4: 16}.get(atyp)
        if skip is None:
            skip = (await r.readexactly(1))[0]
        await r.readexactly(skip + 2)
        return r, w
    except BaseException:
        w.close()
        raise


async def open_via_upstream(up: dict, pw: str, host: str, port: int):
    if up["type"] == "socks5":
        return await _tunnel_socks5(up, pw, host, port)
    return await _tunnel_http(up, pw, host, port)


async def open_outbound(cfg: dict, pw: str, action: str, host: str, port: int):
    if action == "direct":
        return await _connect_tcp(host, port)
    return await open_via_upstream(cfg["upstream"], pw, host, port)


# --- data pump --------------------------------------------------------------

async def _pump(r: asyncio.StreamReader, w: asyncio.StreamWriter, peers) -> None:
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
        if w.can_write_eof():
            w.write_eof()
    except (ConnectionError, OSError):
        for peer in peers:
            peer.close()


async def pipe(r1, w1, r2, w2) -> None:
    try:
        await asyncio.gather(_pump(r1, w2, (w1, w2)), _pump(r2, w1, (w1, w2)))
    finally:
        w1.close()
        w2.close()


# --- inbound protocol handlers ---------------------------------------------

async def _handle_http(first: bytes, store: ConfigStore, reader, writer) -> None:
    head = first + await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    lines = head[:-4].split(b"\r\n")
    try:
        method, target, version = lines[0].decode("latin-1").split(" ", 2)
    except ValueError:
        return await _reply(writer, 400, "Bad Request")
    cfg, pw = store.get()

    if method.upper() == "CONNECT":
        host, port = split_hostport(target, 443)
        action, rule = decide(cfg, host)
        _log_conn("app", host, port, action, rule)
        if action == "block":
            return await _reply(writer, 403, "Forbidden", "blocked by pogo rule\n")
        try:
            ur, uw = await open_outbound(cfg, pw, action, host, port)
        except (UpstreamError, OSError, asyncio.TimeoutError) as e:
            log.warning("       ↳ failed: %s", describe(e))
            return await _reply(writer, 502, "Bad Gateway", f"{describe(e)}\n")
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        return await pipe(reader, writer, ur, uw)

    # plain http:// request in absolute form
    u = urllib.parse.urlsplit(target)
    if u.scheme != "http" or not u.hostname:
        return await _reply(writer, 400, "Bad Request", "this is a proxy; send absolute http:// URLs\n")
    host, port = u.hostname, u.port or 80
    action, rule = decide(cfg, host)
    _log_conn("app", host, port, action, rule)
    if action == "block":
        return await _reply(writer, 403, "Forbidden", "blocked by pogo rule\n")

    up = cfg["upstream"]
    headers = [ln for ln in lines[1:] if ln.split(b":", 1)[0].strip().lower() not in HOP_HEADERS]
    try:
        if action == "proxy" and up["type"] == "http":
            ur, uw = await _connect_tcp(up["host"], up["port"])
            request_line = f"{method} {target} {version}"
            auth = basic_auth_header(up, pw)
            if auth:
                headers.append(auth.encode())
        else:
            ur, uw = await open_outbound(cfg, pw, action, host, port)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            request_line = f"{method} {path} {version}"
    except (UpstreamError, OSError, asyncio.TimeoutError) as e:
        log.warning("       ↳ failed: %s", describe(e))
        return await _reply(writer, 502, "Bad Gateway", f"{describe(e)}\n")
    uw.write(b"\r\n".join([request_line.encode("latin-1"), *headers, b"Connection: close"]) + b"\r\n\r\n")
    await uw.drain()
    await pipe(reader, writer, ur, uw)


# --- hostname sniffing (tunnel mode only sees IPs) --------------------------

def tls_sni(data: bytes) -> tuple[str | None, bool]:
    """Parse a TLS ClientHello. Returns (server_name, need_more_bytes)."""
    if len(data) < 5 or data[0] != 0x16:
        return None, False
    end = 5 + int.from_bytes(data[3:5], "big")
    if len(data) < end:
        return None, True
    try:
        i = 5 + 4 + 2 + 32                       # record hdr, handshake hdr, version, random
        i += 1 + data[i]                         # session id
        i += 2 + int.from_bytes(data[i:i + 2], "big")   # cipher suites
        i += 1 + data[i]                         # compression
        ext_end = i + 2 + int.from_bytes(data[i:i + 2], "big")
        i += 2
        while i + 4 <= min(ext_end, end):
            etype = int.from_bytes(data[i:i + 2], "big")
            elen = int.from_bytes(data[i + 2:i + 4], "big")
            if etype == 0:                       # server_name
                nlen = int.from_bytes(data[i + 7:i + 9], "big")
                return data[i + 9:i + 9 + nlen].decode("ascii"), False
            i += 4 + elen
    except (IndexError, UnicodeDecodeError):
        pass
    return None, False


def http_host(data: bytes) -> str | None:
    if not data.startswith(HTTP_METHODS):
        return None
    for line in data.split(b"\r\n")[1:]:
        if line.lower().startswith(b"host:"):
            value = line[5:].strip().decode("latin-1")
            return split_hostport(value, 80)[0]
    return None


async def sniff_hostname(reader) -> tuple[bytes, str | None]:
    """Peek at the client's first bytes. Returns (bytes_read, hostname_or_None)."""
    data, timeout = b"", SNIFF_FIRST
    for _ in range(6):
        try:
            chunk = await asyncio.wait_for(reader.read(8192), timeout)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        data, timeout = data + chunk, SNIFF_MORE
        name, need_more = tls_sni(data)
        if name or not need_more:
            return data, name or http_host(data)
    return data, http_host(data)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


async def _handle_socks(store: ConfigStore, reader, writer) -> None:
    n = (await reader.readexactly(1))[0]
    await reader.readexactly(n)  # offered auth methods; we only do "none" (loopback)
    writer.write(b"\x05\x00")
    _, cmd, _, atyp = await reader.readexactly(4)
    if atyp == 1:
        host = socket.inet_ntoa(await reader.readexactly(4))
    elif atyp == 3:
        host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
    else:
        writer.write(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    port = int.from_bytes(await reader.readexactly(2), "big")
    if cmd != 1:
        writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    cfg, pw = store.get()
    action, rule = decide(cfg, host)
    ip, replied, early = host, False, b""
    # Tunnel mode hands us bare IPs. Unless a CIDR rule already settled it, accept the
    # connection, peek at the first bytes for the real hostname, then decide.
    if cfg.get("sniff", True) and _is_ip(host) and not (rule != "default" and is_ip_rule(rule)):
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()
        replied = True
        early, name = await sniff_hostname(reader)
        if name:
            host = name
            action, rule = decide(cfg, host)
    _log_conn("captured", host, port, action, rule, ip)
    if action == "block":
        if not replied:
            writer.write(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    # direct connections keep the IP the client resolved; proxied ones use the name
    target = ip if action == "direct" else host
    try:
        ur, uw = await open_outbound(cfg, pw, action, target, port)
    except (UpstreamError, OSError, asyncio.TimeoutError) as e:
        log.warning("       ↳ failed: %s", describe(e))
        if not replied:
            writer.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
        return
    if not replied:
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()
    if early:
        uw.write(early)
        await uw.drain()
    await pipe(reader, writer, ur, uw)


async def handle_client(store: ConfigStore, reader, writer) -> None:
    try:
        first = await asyncio.wait_for(reader.readexactly(1), HEAD_TIMEOUT)
        if first == b"\x05":
            await _handle_socks(store, reader, writer)
        else:
            await _handle_http(first, store, reader, writer)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            asyncio.TimeoutError, ConnectionError, OSError):
        pass
    except Exception:
        log.exception("unexpected error")
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def serve(store: ConfigStore, host: str, port: int, ready: asyncio.Event | None = None,
                stop: asyncio.Event | None = None) -> None:
    server = await asyncio.start_server(lambda r, w: handle_client(store, r, w), host, port)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    log.info("listening on %s:%d (HTTP + SOCKS5)", host, port)
    if ready:
        ready.set()
    await stop.wait()
    server.close()
    await server.wait_closed()
