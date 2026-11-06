"""
UDP-ONLY version of the server.  No TCP socket is opened, no HTTP, no WebSocket.

  * one UDP socket (default 0.0.0.0:9000), clients are identified by (ip, port)
  * game snapshots and inputs are plain datagrams: a lost one is simply replaced by the next one
  * login / lobby / start / results use arena.reliable (ack + retransmission written by us)
  * disconnections are detected by silence (IDLE_TIMEOUT) or by a reliable message nobody acknowledges

Run :  python udp_version/server_udp.py
       python udp_version/server_udp.py --loss 0.15      (drop 15% of OUR outgoing datagrams: lab test)
Clients: python client/pygame_client.py --proto udp --host <ip of this PC>
"""
import argparse
import asyncio
import json
import os
import random
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from arena.hub import Hub, Session             # noqa: E402
from arena.netutil import lan_addresses        # noqa: E402
from arena.reliable import ReliableEndpoint    # noqa: E402


class UdpSession(Session):
    def __init__(self, hub, server, addr, cid):
        super().__init__(hub, f"{addr[0]}:{addr[1]}")
        self.server, self.addr, self.cid = server, addr, cid
        self.ep = ReliableEndpoint(self._raw)

    def _raw(self, data: bytes):
        self.bytes_out += len(data)
        if self.server.loss and random.random() < self.server.loss:
            return                                       # simulated network loss (testing only)
        if self.server.transport:
            self.server.transport.sendto(data, self.addr)

    def send(self, msg, reliable=True):
        if self.dead:
            return
        self.msgs_out += 1
        if msg.get("t") == "snap":
            self.snaps_sent += 1
        if reliable:
            self.ep.send_reliable(msg)
        else:
            self.ep.send_unreliable(msg)

    def send_final(self, msg):
        for _ in range(3):                               # the session is about to disappear: no retransmission
            self.ep.send_unreliable(msg)                 # possible, so repeat the datagram a few times

    def poll(self, now):
        self.ep.poll(now)
        self.retransmits = self.ep.retransmits
        if self.ep.dead:
            self.hub.disconnect(self, "peer stopped acknowledging")

    def close(self):
        if self.server.sessions.get(self.addr) is self:
            del self.server.sessions[self.addr]


class UdpServer(asyncio.DatagramProtocol):
    def __init__(self, hub, loss):
        self.hub, self.loss = hub, loss
        self.transport = None
        self.sessions = {}                               # (ip, port) -> UdpSession

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):                       # e.g. ICMP "port unreachable": ignore, timeouts handle it
        pass

    def datagram_received(self, data, addr):
        try:
            pkt = json.loads(data)
            if not isinstance(pkt, dict):
                return
        except ValueError:
            return
        sess = self.sessions.get(addr)
        cid = pkt.get("c")
        if pkt.get("k") == "h":                          # handshake: (re)create the session
            if sess is None or sess.cid != cid:
                if sess is not None:
                    self.hub.disconnect(sess, "client restarted")
                sess = UdpSession(self.hub, self, addr, cid)
                self.sessions[addr] = sess
                self.hub.on_connect(sess)
            return
        if sess is None or sess.cid != cid:
            return                                       # unknown sender: ignore
        sess.bytes_in += len(data)
        for msg in sess.ep.on_packet(pkt):
            self.hub.on_message(sess, msg)


def open_socket(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    if hasattr(socket, "SIO_UDP_CONNRESET"):             # Windows: do not let ICMP errors kill recvfrom()
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except OSError:
            pass
    sock.bind((host, port))
    sock.setblocking(False)
    return sock


async def main():
    ap = argparse.ArgumentParser(description="Bomberman Arena - UDP-only server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--loss", type=float, default=0.0, help="simulated loss of outgoing datagrams, 0..1")
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "data"))
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    hub = Hub("udp", a.db or os.path.join(a.data_dir, "arena.db"), a.data_dir)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: UdpServer(hub, a.loss), sock=open_socket(a.host, a.port))
    print("=" * 62)
    print(" Bomberman Arena - UDP version")
    print(f" listening on UDP {a.host}:{a.port}" + (f"   (simulated loss {a.loss:.0%})" if a.loss else ""))
    for ip in lan_addresses():
        print(f"   clients connect to:  --host {ip} --port {a.port}")
    print("=" * 62, flush=True)
    await hub.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nserver stopped")
