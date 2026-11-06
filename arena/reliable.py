"""
Reliability on top of UDP, written from scratch (the UDP version may not use TCP).

Packet kinds (one JSON object per datagram):
  {"k":"u","m":{...}}            unreliable message  (game snapshots, inputs, pings)
  {"k":"r","i":N,"m":{...}}      reliable message number N
  {"k":"a","i":N}                acknowledgement of reliable message N
  {"k":"h","c":CID}              client hello (handshake)

Reliable messages use stop-and-wait: ONE message in flight, resent every `rto` seconds until
acknowledged, delivered to the application in order, duplicates discarded.  It is simple, ordered
and good enough for the control traffic (login, lobby, start, results).  If a message is still
unacknowledged after `max_tries` attempts the peer is declared dead (= disconnection detection).
"""
import json
import time
from collections import deque


def encode(pkt):
    return json.dumps(pkt, separators=(",", ":")).encode()


class ReliableEndpoint:
    def __init__(self, send_raw, extra=None, rto=0.25, max_tries=24):
        self.send_raw = send_raw
        self.extra = extra or {}
        self.rto, self.max_tries = rto, max_tries
        self.queue = deque()
        self.inflight = None                    # [id, raw_bytes, tries, last_sent_time]
        self.next_id, self.expect = 1, 1
        self.retransmits = 0
        self.dead = False

    def _send(self, pkt):
        pkt.update(self.extra)
        raw = encode(pkt)
        self.send_raw(raw)
        return raw

    def send_unreliable(self, msg):
        self._send({"k": "u", "m": msg})

    def send_reliable(self, msg):
        self.queue.append(msg)
        self._pump()

    def _pump(self):
        if self.inflight is None and self.queue:
            msg = self.queue.popleft()
            i = self.next_id
            self.next_id += 1
            raw = self._send({"k": "r", "i": i, "m": msg})
            self.inflight = [i, raw, 1, time.monotonic()]

    def on_packet(self, pkt):
        """Feed a decoded datagram; returns the list of messages to hand to the application."""
        out, k = [], pkt.get("k")
        if k == "u":
            if isinstance(pkt.get("m"), dict):
                out.append(pkt["m"])
        elif k == "a":
            if self.inflight and pkt.get("i") == self.inflight[0]:
                self.inflight = None
                self._pump()
        elif k == "r":
            i = pkt.get("i")
            if isinstance(i, int):
                if i == self.expect and isinstance(pkt.get("m"), dict):
                    self.expect += 1
                    out.append(pkt["m"])
                if i < self.expect:                 # new message or duplicate: (re)acknowledge
                    self._send({"k": "a", "i": i})
        return out

    def poll(self, now=None):
        """Call often (every ~50 ms): resend the unacknowledged message, detect a dead peer."""
        now = now or time.monotonic()
        if self.inflight:
            i, raw, tries, last = self.inflight
            if now - last >= self.rto:
                if tries >= self.max_tries:
                    self.dead = True
                    return
                self.inflight[2] += 1
                self.inflight[3] = now
                self.retransmits += 1
                self.send_raw(raw)
