"""A tiny authenticating HTTP CONNECT proxy that stands in for a campus proxy in end-to-end tests.

Like pogo itself, its own outgoing connections are pinned to the real network card while the
all-apps capture runs, so they don't loop back through the capture.

    python tests/e2e/upstream.py PORT
"""
import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pogo import server  # noqa: E402

USER, PASSWORD = "ci", "secret"
EXPECTED = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


async def handle(reader, writer):
    try:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        lines = head.split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:] if ": " in line)}
        if headers.get("proxy-authorization") != EXPECTED:
            writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="ci"\r\n\r\n')
            return
        if method != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, port = server.split_hostport(target, 443)
        up_reader, up_writer = await server._connect_tcp(host, port)  # pinned while the capture runs
        print(f"CONNECT {host}:{port}", flush=True)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await server.pipe(reader, writer, up_reader, up_writer)
    except Exception as e:  # keep serving; this is a test fixture
        print(f"error: {type(e).__name__}: {e}", flush=True)
    finally:
        writer.close()


async def main(port: int) -> None:
    listener = await asyncio.start_server(handle, "127.0.0.1", port)
    print(f"listening on 127.0.0.1:{port}", flush=True)
    async with listener:
        await listener.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
