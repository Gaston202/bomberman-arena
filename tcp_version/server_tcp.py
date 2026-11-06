"""
TCP-ONLY version of the server.

One listening TCP socket (default 0.0.0.0:8000) serves three kinds of connections, detected from
the first line a client sends:
  * browser  : "GET /..."  -> static files (the game page)  or  WebSocket upgrade (game traffic)
  * native   : JSON lines  -> plain TCP socket, one JSON message per line (pygame client, bots)
Everything runs over TCP.  No UDP is used by this version.

Run :  python tcp_version/server_tcp.py            (then open http://<this PC's IP>:8000 on the other devices)
"""
import argparse
import asyncio
import json
import mimetypes
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from arena import wsproto                      # noqa: E402
from arena.hub import Hub, Session             # noqa: E402
from arena.netutil import lan_addresses        # noqa: E402

WEB_DIR = os.path.join(ROOT, "tcp_version", "web")
IMG_DIR = os.path.join(ROOT, "assets", "images")
MAX_LINE = 1 << 16
BACKLOG_LIMIT = 32 * 1024            # do not queue more than this for a slow client (snapshots are skipped)


class TcpSession(Session):
    def __init__(self, hub, writer, kind):
        super().__init__(hub, writer.get_extra_info("peername"))
        self.writer, self.kind = writer, kind           # kind: "ws" (browser) or "raw" (native client)

    def send(self, msg, reliable=True):
        if self.dead:
            return
        is_snap = msg.get("t") == "snap"
        if is_snap and self.writer.transport.get_write_buffer_size() > BACKLOG_LIMIT:
            self.snaps_sent += 1                         # TCP never loses data, but a slow reader fills the buffer:
            self.snaps_skipped += 1                      # skip the snapshot instead of letting latency grow forever
            return
        data = json.dumps(msg, separators=(",", ":")).encode()
        out = wsproto.encode_frame(data) if self.kind == "ws" else data + b"\n"
        try:
            self.writer.write(out)
        except Exception:
            return
        self.count_out(msg, len(out))

    def close(self):
        try:
            self.writer.close()
        except Exception:
            pass


def feed(hub, sess, raw: bytes):
    sess.bytes_in += len(raw)
    try:
        msg = json.loads(raw)
    except ValueError:
        return
    hub.on_message(sess, msg)


async def serve_http(hub, first, reader, writer):
    """First request line already read.  Serves files or upgrades to WebSocket."""
    try:
        method, target, _ = first.decode("latin-1").split(" ", 2)
    except ValueError:
        return writer.close()
    headers = {}
    for _ in range(100):
        line = await asyncio.wait_for(reader.readline(), 10)
        if line in (b"\r\n", b"\n", b""):
            break
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()

    if headers.get("upgrade", "").lower() == "websocket" and "sec-websocket-key" in headers:
        accept = wsproto.accept_key(headers["sec-websocket-key"])
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        return await ws_loop(hub, reader, writer)

    path = target.split("?", 1)[0]
    if path == "/":
        path = "/index.html"
    base, rel = (IMG_DIR, path[len("/images/"):]) if path.startswith("/images/") else (WEB_DIR, path.lstrip("/"))
    full = os.path.normpath(os.path.join(base, rel))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        body, status, ctype = b"404 Not found", "404 Not Found", "text/plain"
    else:
        with open(full, "rb") as f:
            body = f.read()
        status, ctype = "200 OK", mimetypes.guess_type(full)[0] or "application/octet-stream"
    head = (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Cache-Control: no-cache\r\nConnection: close\r\n\r\n").encode()
    writer.write(head + (b"" if method == "HEAD" else body))
    await writer.drain()
    writer.close()


async def ws_loop(hub, reader, writer):
    sess = TcpSession(hub, writer, "ws")
    hub.on_connect(sess)
    buf = b""
    try:
        while not sess.dead:
            fin, opcode, data = await wsproto.read_frame(reader)
            if opcode == 0x8:                                      # close
                break
            if opcode == 0x9:                                      # ping -> pong
                writer.write(wsproto.encode_frame(data, 0xA))
                continue
            if opcode in (0x1, 0x2, 0x0):
                buf += data
                if fin:
                    feed(hub, sess, buf)
                    buf = b""
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, OSError):
        pass
    finally:
        hub.disconnect(sess, "connection closed")


async def raw_loop(hub, first, reader, writer):
    sess = TcpSession(hub, writer, "raw")
    hub.on_connect(sess)
    try:
        line = first
        while line and not sess.dead:
            feed(hub, sess, line)
            line = await reader.readline()
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, OSError):
        pass
    finally:
        hub.disconnect(sess, "connection closed")


def make_handler(hub):
    async def handle(reader, writer):
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # no Nagle delay: small real-time messages
        try:
            first = await asyncio.wait_for(reader.readline(), 10)
            if not first:
                return writer.close()
            if first.startswith((b"GET ", b"HEAD ")):
                await serve_http(hub, first, reader, writer)
            else:
                await raw_loop(hub, first, reader, writer)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError, OSError):
            try:
                writer.close()
            except Exception:
                pass
    return handle


async def main():
    ap = argparse.ArgumentParser(description="Bomberman Arena - TCP-only server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "data"))
    ap.add_argument("--db", default=None, help="SQLite file (default: <data-dir>/arena.db)")
    a = ap.parse_args()
    hub = Hub("tcp", a.db or os.path.join(a.data_dir, "arena.db"), a.data_dir)
    server = await asyncio.start_server(make_handler(hub), a.host, a.port, limit=MAX_LINE)
    print("=" * 62)
    print(" Bomberman Arena - TCP version")
    print(f" listening on TCP {a.host}:{a.port}")
    for ip in lan_addresses():
        print(f"   other devices open:  http://{ip}:{a.port}")
    print(" (native clients: python client/pygame_client.py --proto tcp --host <ip>)")
    print("=" * 62, flush=True)
    async with server:
        await asyncio.gather(server.serve_forever(), hub.run())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nserver stopped")
