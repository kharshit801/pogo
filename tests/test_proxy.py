"""End-to-end tests against a fake campus proxy. Run: python3 -m unittest discover -s tests"""
import asyncio
import base64
import os
import socket
import ssl
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pogo.config import ConfigStore  # noqa: E402
from pogo.rules import decide, matches  # noqa: E402
from pogo import server  # noqa: E402
from pogo.server import http_host, serve, tls_sni  # noqa: E402

USER, PASS = "student", "p@ss:word"
GOOD_AUTH = "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()


class Env:
    """target web server + fake upstream proxies (HTTP and SOCKS5) + our pogo server."""

    def __init__(self, upstream_type="http"):
        self.upstream_type = upstream_type
        self.target_seen, self.upstream_seen = [], []

    async def start(self):
        self.target = await asyncio.start_server(self._target, "127.0.0.1", 0)
        self.tport = self.target.sockets[0].getsockname()[1]
        handler = self._fake_http_proxy if self.upstream_type == "http" else self._fake_socks_proxy
        self.upstream = await asyncio.start_server(handler, "127.0.0.1", 0)
        uport = self.upstream.sockets[0].getsockname()[1]
        cfg = {
            "upstream": {"type": self.upstream_type, "host": "127.0.0.1", "port": uport, "username": USER},
            "listen": {"host": "127.0.0.1", "port": 0},
            "default_action": "proxy",
            "rules": [
                {"pattern": "127.0.0.0/8", "action": "direct"},
                {"pattern": "*.blocked.test", "action": "block"},
            ],
        }
        self.stop = asyncio.Event()
        ready = asyncio.Event()
        # port 0 -> pick a free port ourselves
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.pport = s.getsockname()[1]; s.close()
        self.task = asyncio.create_task(
            serve(ConfigStore(fixed=(cfg, PASS)), "127.0.0.1", self.pport, ready, self.stop))
        await ready.wait()

    async def close(self):
        self.stop.set()
        await self.task
        for srv in (self.target, self.upstream):
            srv.close()

    async def _target(self, r, w):
        head = await r.readuntil(b"\r\n\r\n")
        self.target_seen.append(head.split(b"\r\n")[0].decode())
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await w.drain(); w.close()

    async def _fake_http_proxy(self, r, w):
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        first, *hdrs = head.strip().split("\r\n")
        h = {k.lower(): v for k, v in (x.split(": ", 1) for x in hdrs)}
        self.upstream_seen.append(first)
        if h.get("proxy-authorization") != GOOD_AUTH:
            w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            await w.drain(); w.close(); return
        if first.startswith("CONNECT"):
            _, hp, _ = first.split(" ")
            tr, tw = await asyncio.open_connection("127.0.0.1", int(hp.rsplit(":", 1)[1]))
            w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await w.drain()
            await asyncio.gather(self._copy(r, tw), self._copy(tr, w))
        else:  # plain request: answer ourselves
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nviaproxy")
            await w.drain(); w.close()

    async def _fake_socks_proxy(self, r, w):
        _, nmethods = await r.readexactly(2)
        await r.readexactly(nmethods)
        w.write(b"\x05\x02")  # demand username/password
        await r.readexactly(1)
        u = await r.readexactly((await r.readexactly(1))[0])
        p = await r.readexactly((await r.readexactly(1))[0])
        ok = (u.decode(), p.decode()) == (USER, PASS)
        w.write(b"\x01" + (b"\x00" if ok else b"\x01"))
        if not ok:
            await w.drain(); w.close(); return
        _, _, _, atyp = await r.readexactly(4)
        assert atyp == 3
        name = await r.readexactly((await r.readexactly(1))[0])
        port = int.from_bytes(await r.readexactly(2), "big")
        self.upstream_seen.append(f"SOCKS {name.decode()}:{port}")
        tr, tw = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await w.drain()
        await asyncio.gather(self._copy(r, tw), self._copy(tr, w))

    @staticmethod
    async def _copy(r, w):
        try:
            while d := await r.read(65536):
                w.write(d); await w.drain()
        finally:
            w.close()

    async def client(self, payload: bytes, read_until_close=True) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", self.pport)
        w.write(payload); await w.drain()
        data = await asyncio.wait_for(r.read(), 5)
        w.close()
        return data

    async def tunnel(self, connect_target: str) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", self.pport)
        w.write(f"CONNECT {connect_target} HTTP/1.1\r\nHost: {connect_target}\r\n\r\n".encode())
        await w.drain()
        status = await r.readuntil(b"\r\n\r\n")
        if b" 200 " not in status.split(b"\r\n")[0]:
            rest = await asyncio.wait_for(r.read(), 5)
            w.close(); return status + rest
        w.write(b"GET /t HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"); await w.drain()
        body = await asyncio.wait_for(r.read(), 5)
        w.close()
        return status + body


def run(coro):
    return asyncio.run(coro)


class ProxyTests(unittest.TestCase):
    def test_connect_via_http_upstream_sends_credentials(self):
        async def go():
            e = Env(); await e.start()
            out = await e.tunnel(f"campus-outside.test:{e.tport}")
            await e.close()
            return e, out
        e, out = run(go())
        self.assertTrue(out.endswith(b"ok"), out)
        self.assertEqual(e.upstream_seen, [f"CONNECT campus-outside.test:{e.tport} HTTP/1.1"])

    def test_direct_rule_bypasses_upstream(self):
        async def go():
            e = Env(); await e.start()
            out = await e.tunnel(f"127.0.0.1:{e.tport}")
            await e.close()
            return e, out
        e, out = run(go())
        self.assertTrue(out.endswith(b"ok"))
        self.assertEqual(e.upstream_seen, [])
        self.assertEqual(e.target_seen, ["GET /t HTTP/1.1"])

    def test_plain_http_via_upstream_uses_absolute_uri(self):
        async def go():
            e = Env(); await e.start()
            out = await e.client(b"GET http://campus-outside.test/page?q=1 HTTP/1.1\r\n"
                                 b"Host: campus-outside.test\r\nProxy-Connection: keep-alive\r\n\r\n")
            await e.close()
            return e, out
        e, out = run(go())
        self.assertTrue(out.endswith(b"viaproxy"), out)
        self.assertEqual(e.upstream_seen, ["GET http://campus-outside.test/page?q=1 HTTP/1.1"])

    def test_plain_http_direct_uses_origin_form(self):
        async def go():
            e = Env(); await e.start()
            out = await e.client(f"GET http://127.0.0.1:{e.tport}/page?q=1 HTTP/1.1\r\n"
                                 f"Host: 127.0.0.1\r\n\r\n".encode())
            await e.close()
            return e, out
        e, out = run(go())
        self.assertTrue(out.endswith(b"ok"))
        self.assertEqual(e.target_seen, ["GET /page?q=1 HTTP/1.1"])

    def test_block_rule(self):
        async def go():
            e = Env(); await e.start()
            out = await e.tunnel("ads.blocked.test:443")
            await e.close()
            return out
        self.assertIn(b"403", run(go()))

    def test_bad_password_reports_407(self):
        async def go():
            e = Env(); await e.start()
            e.stop.set(); await e.task
            # restart our proxy with wrong password
            cfg = {"upstream": {"type": "http", "host": "127.0.0.1",
                                "port": e.upstream.sockets[0].getsockname()[1], "username": USER},
                   "listen": {}, "default_action": "proxy", "rules": []}
            e.stop = asyncio.Event(); ready = asyncio.Event()
            e.task = asyncio.create_task(serve(ConfigStore(fixed=(cfg, "WRONG")), "127.0.0.1", e.pport, ready, e.stop))
            await ready.wait()
            out = await e.tunnel("campus-outside.test:443")
            await e.close()
            return out
        out = run(go())
        self.assertIn(b"502", out)
        self.assertIn(b"407", out)

    def test_socks5_inbound_to_socks5_upstream(self):
        async def go():
            e = Env("socks5"); await e.start()
            r, w = await asyncio.open_connection("127.0.0.1", e.pport)
            w.write(b"\x05\x01\x00")
            assert await r.readexactly(2) == b"\x05\x00"
            name = b"campus-outside.test"
            w.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + e.tport.to_bytes(2, "big"))
            reply = await r.readexactly(10)
            w.write(b"GET /s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"); await w.drain()
            body = await asyncio.wait_for(r.read(), 5)
            w.close(); await e.close()
            return e, reply, body
        e, reply, body = run(go())
        self.assertEqual(reply[:2], b"\x05\x00")
        self.assertTrue(body.endswith(b"ok"))
        self.assertEqual(e.upstream_seen, [f"SOCKS campus-outside.test:{e.tport}"])


def client_hello(name: str) -> bytes:
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ssl.create_default_context().wrap_bio(inc, out, server_hostname=name)
    try:
        obj.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return out.read()


async def socks_via_ip(e: Env, ip: str, first_bytes: bytes) -> bytes:
    """What tun2socks does: SOCKS5 CONNECT to a bare IP, then send app data."""
    r, w = await asyncio.open_connection("127.0.0.1", e.pport)
    w.write(b"\x05\x01\x00")
    await r.readexactly(2)
    w.write(b"\x05\x01\x00\x01" + socket.inet_aton(ip) + e.tport.to_bytes(2, "big"))
    await r.readexactly(10)
    w.write(first_bytes); await w.drain()
    try:
        return await asyncio.wait_for(r.read(), 5)
    finally:
        w.close()


class SniffTests(unittest.TestCase):
    def test_tls_sni_parsed_from_real_client_hello(self):
        self.assertEqual(tls_sni(client_hello("www.example.org")), ("www.example.org", False))

    def test_tls_sni_partial_hello_asks_for_more(self):
        hello = client_hello("www.example.org")
        self.assertEqual(tls_sni(hello[:40]), (None, True))

    def test_http_host(self):
        self.assertEqual(http_host(b"GET / HTTP/1.1\r\nHost: a.example.com:8080\r\n\r\n"), "a.example.com")
        self.assertIsNone(http_host(b"\x16\x03\x01"))

    def test_ip_connection_is_proxied_by_sniffed_hostname(self):
        async def go():
            e = Env(); await e.start()
            out = await socks_via_ip(e, "203.0.113.9", b"GET /x HTTP/1.1\r\nHost: sniffed.test\r\n\r\n")
            await e.close()
            return e, out
        e, out = run(go())
        self.assertTrue(out.endswith(b"ok"), out)
        self.assertEqual(e.upstream_seen, [f"CONNECT sniffed.test:{e.tport} HTTP/1.1"])

    def test_domain_block_rule_applies_to_ip_only_connection(self):
        async def go():
            e = Env(); await e.start()
            out = await socks_via_ip(e, "203.0.113.9", b"GET / HTTP/1.1\r\nHost: ads.blocked.test\r\n\r\n")
            await e.close()
            return e, out
        e, out = run(go())
        self.assertEqual(out, b"")
        self.assertEqual(e.upstream_seen, [])

    @unittest.skipUnless(hasattr(socket, "if_nametoindex") and sys.platform == "darwin", "macOS only")
    def test_outbound_sockets_pinned_to_interface(self):
        async def go():
            e = Env(); await e.start()
            used = []
            orig = server._connect_bound

            async def spy(host, port, ifname):
                used.append(ifname)
                return await orig(host, port, ifname)
            server._connect_bound, server.tun_interface = spy, (lambda: "lo0")
            try:
                out = await e.tunnel(f"127.0.0.1:{e.tport}")   # direct rule -> must use bound connect
            finally:
                server._connect_bound, server.tun_interface = orig, (lambda: None)
            await e.close()
            return used, out
        used, out = run(go())
        self.assertEqual(used, ["lo0"])
        self.assertTrue(out.endswith(b"ok"))


class RuleTests(unittest.TestCase):
    def test_matching(self):
        self.assertTrue(matches("example.com", "example.com"))
        self.assertTrue(matches("example.com", "a.b.example.com"))
        self.assertFalse(matches("example.com", "notexample.com"))
        self.assertTrue(matches("*.edu", "mit.edu"))
        self.assertTrue(matches("*.example.com", "example.com"))
        self.assertTrue(matches("10.0.0.0/8", "10.20.30.40"))
        self.assertFalse(matches("10.0.0.0/8", "11.0.0.1"))
        self.assertFalse(matches("10.0.0.0/8", "example.com"))
        self.assertTrue(matches("192.168.*", "192.168.1.1"))
        self.assertTrue(matches("*", "anything"))

    def test_first_match_wins_and_default(self):
        cfg = {"default_action": "proxy", "rules": [
            {"pattern": "a.example.com", "action": "direct"},
            {"pattern": "example.com", "action": "block"}]}
        self.assertEqual(decide(cfg, "a.example.com"), ("direct", "a.example.com"))
        self.assertEqual(decide(cfg, "b.example.com"), ("block", "example.com"))
        self.assertEqual(decide(cfg, "other.org"), ("proxy", "default"))


if __name__ == "__main__":
    unittest.main()
