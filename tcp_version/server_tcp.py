"""
TCP-ONLY version of the server.  Plain TCP sockets only: no HTTP, no WebSocket, no browser.

  * one listening TCP socket (default 0.0.0.0:8000), created with socket.socket(AF_INET, SOCK_STREAM)
  * every client is one TCP connection; messages are JSON, one per line ("\\n" terminated)
  * reliability, ordering and retransmission are provided by TCP itself
  * a disconnection is detected when the connection closes / resets

Run :  python tcp_version/server_tcp.py
Clients: python client/pygame_client.py --proto tcp --host <ip of this PC>
"""
import argparse
import asyncio
import json
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from arena.hub import Hub, Session             # noqa: E402
from arena.netutil import lan_addresses        # noqa: E402

MAX_LINE = 1 << 16
BACKLOG_LIMIT = 32 * 1024            # do not queue more than this for a slow client (snapshots are skipped)


class TcpSession(Session):
    def __init__(self, hub, writer):
        super().__init__(hub, writer.get_extra_info("peername"))
        self.writer = writer

    def send(self, msg, reliable=True):
        if self.dead:
            return
        is_snap = msg.get("t") == "snap"
        if is_snap and self.writer.transport.get_write_buffer_size() > BACKLOG_LIMIT:
            self.snaps_sent += 1                         # TCP never loses data, but a slow reader fills the buffer:
            self.snaps_skipped += 1                      # skip the snapshot instead of letting latency grow forever
            return
        out = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
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


def make_handler(hub):
    async def handle(reader, writer):
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # no Nagle delay: small real-time messages
        sess = TcpSession(hub, writer)
        hub.on_connect(sess)
        try:
            while not sess.dead:
                line = await reader.readline()
                if not line:                                              # peer closed the connection
                    break
                feed(hub, sess, line)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError, OSError):
            pass
        finally:
            hub.disconnect(sess, "connection closed")
    return handle


def open_socket(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(16)
    sock.setblocking(False)
    return sock


async def main():
    ap = argparse.ArgumentParser(description="Bomberman Arena - TCP-only server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "data"))
    ap.add_argument("--db", default=None, help="SQLite file (default: <data-dir>/arena.db)")
    a = ap.parse_args()
    hub = Hub("tcp", a.db or os.path.join(a.data_dir, "arena.db"), a.data_dir)
    server = await asyncio.start_server(make_handler(hub), sock=open_socket(a.host, a.port), limit=MAX_LINE)
    print("=" * 62)
    print(" Bomberman Arena - TCP version")
    print(f" listening on TCP {a.host}:{a.port}")
    for ip in lan_addresses():
        print(f"   clients connect to:  --proto tcp --host {ip} --port {a.port}")
    print("=" * 62, flush=True)
    async with server:
        await asyncio.gather(server.serve_forever(), hub.run())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nserver stopped")
