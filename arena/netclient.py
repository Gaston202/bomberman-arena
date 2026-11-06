"""
Client-side networking for native clients (pygame client, bots, tests).
The two classes expose the SAME interface; only the transport underneath differs.

    net = make_client("tcp" | "udp", host, port)
    net.start()
    for msg in net.poll(): ...      # call every frame; returns decoded server messages
    net.send({"t": "login", ...})   # reliable=True by default
    net.send({"t": "in", ...}, reliable=False)
"""
import json
import os
import queue
import random
import socket
import threading
import time

from .reliable import ReliableEndpoint, encode

SERVER_SILENCE = 6.0         # no packet from the server for this long => connection lost


class _Base:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.inbox = queue.Queue()
        self.state = "connecting"            # connecting | connected | closed
        self.rtt_ms = None
        self.snap_rx = self.snap_gap = 0
        self._last_snap = -1
        self._stop = threading.Event()
        self.last_rx = time.monotonic()
        self.bytes_in = self.bytes_out = 0

    def start(self):
        raise NotImplementedError

    def send(self, msg, reliable=True):
        raise NotImplementedError

    def close(self):
        self._stop.set()

    # ---- shared logic
    def _deliver(self, msg):
        self.last_rx = time.monotonic()
        if isinstance(msg, dict):
            if msg.get("t") == "pong" and isinstance(msg.get("ts"), (int, float)):
                self.rtt_ms = round((time.time() - msg["ts"]) * 1000, 1)     # stamped at arrival, not at poll()
                return
            self.inbox.put(msg)

    def _fail(self, why):
        if self.state != "closed":
            self.state = "closed"
            self.inbox.put({"t": "_closed", "why": why})
        self._stop.set()

    def _heartbeat(self):
        while not self._stop.wait(1.0):
            if self.state == "connected":
                self.send({"t": "ping", "ts": time.time(), "rtt": self.rtt_ms,
                           "rx": self.snap_rx, "gap": self.snap_gap}, reliable=False)

    def poll(self):
        out = []
        while True:
            try:
                m = self.inbox.get_nowait()
            except queue.Empty:
                return out
            t = m.get("t")
            if t == "hello":
                self.state = "connected"
            elif t == "pong":
                if isinstance(m.get("ts"), (int, float)):
                    self.rtt_ms = round((time.time() - m["ts"]) * 1000, 1)
                continue
            elif t == "start":
                self._last_snap = -1
            elif t == "snap":
                s = m.get("s", 0)
                if s <= self._last_snap:                     # stale or duplicated (UDP can reorder)
                    continue
                if self._last_snap >= 0 and s > self._last_snap + 1:
                    self.snap_gap += s - self._last_snap - 1  # missing snapshots = lost updates
                self._last_snap = s
                self.snap_rx += 1
            out.append(m)


class TcpClient(_Base):
    """Plain TCP socket, one JSON message per line."""
    proto = "tcp"

    def start(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=5)
        except OSError as e:
            self._fail(f"cannot connect: {e}")
            return
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        # The server tells browsers ("GET ...") from native clients by the FIRST line received,
        # so a native client must speak first.
        self.send({"t": "ping", "ts": time.time()}, reliable=False)

    def _reader(self):
        buf = b""
        try:
            while not self._stop.is_set():
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self.bytes_in += len(chunk)
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        self._deliver(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        self._fail("server closed the connection")

    def send(self, msg, reliable=True):
        data = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
        try:
            with self._lock:
                self.sock.sendall(data)
            self.bytes_out += len(data)
        except (OSError, AttributeError):
            self._fail("send failed")

    def close(self):
        super().close()
        try:
            self.sock.close()
        except Exception:
            pass


class UdpClient(_Base):
    """UDP datagrams + arena.reliable.  `loss` simulates dropped outgoing datagrams (lab tests)."""
    proto = "udp"

    def __init__(self, host, port, loss=0.0):
        super().__init__(host, port)
        self.loss = loss
        self.cid = "%08x" % random.getrandbits(32)
        self._lock = threading.Lock()
        self.ep = ReliableEndpoint(self._raw, extra={"c": self.cid})

    def start(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, "SIO_UDP_CONNRESET"):
                try:
                    self.sock.ioctl(socket.SIO_UDP_CONNRESET, False)
                except OSError:
                    pass
            self.sock.connect((self.host, self.port))        # fixes the peer address only (no handshake in UDP)
            self.sock.settimeout(0.05)
        except OSError as e:
            self._fail(f"cannot open socket: {e}")
            return
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _raw(self, data):
        self.bytes_out += len(data)
        if self.loss and random.random() < self.loss:
            return
        try:
            self.sock.send(data)
        except OSError:
            pass

    def _loop(self):
        last_hello = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if self.state == "connecting" and now - last_hello > 0.3:
                last_hello = now
                with self._lock:
                    self._raw(encode({"k": "h", "c": self.cid}))
            try:
                data = self.sock.recv(65536)
                self.bytes_in += len(data)
                pkt = json.loads(data)
                if isinstance(pkt, dict):
                    with self._lock:
                        msgs = self.ep.on_packet(pkt)
                    for m in msgs:
                        self._deliver(m)
            except (socket.timeout, ValueError):
                pass
            except OSError:
                time.sleep(0.05)
            with self._lock:
                self.ep.poll(now)
            if self.ep.dead:
                return self._fail("server stopped acknowledging")
            if now - self.last_rx > SERVER_SILENCE:
                return self._fail("no packet from the server")

    def send(self, msg, reliable=True):
        with self._lock:
            if reliable:
                self.ep.send_reliable(msg)
            else:
                self.ep.send_unreliable(msg)

    def close(self):
        super().close()
        try:
            self.sock.close()
        except Exception:
            pass

    @property
    def retransmits(self):
        return self.ep.retransmits


def make_client(proto, host, port=None, loss=0.0):
    if proto == "tcp":
        return TcpClient(host, port or 8000)
    return UdpClient(host, port or 9000, loss)
