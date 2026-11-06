"""
The Hub is the whole game server EXCEPT the transport.

  transport (TCP / WebSocket / UDP)  ->  Session.send() / Hub.on_message()  ->  Hub (this file)

Both versions (TCP-only and UDP-only) run exactly the same Hub, so the rules and features are
identical and only the transport changes.

Messages are JSON objects with a type field "t".
  client -> server : ping register login logout rooms lb create join leave start in
  server -> client : hello pong auth_ok auth_err err kicked rooms lb room left notice start snap end
"""
import asyncio
import collections
import json
import os
import time

from . import engine, storage

MIN_PLAYERS = 2
MAX_PLAYERS = 4
IDLE_TIMEOUT = 6.0                  # seconds without any packet from a client => disconnected
LOGIN_MAX_FAILS, LOGIN_WINDOW, LOGIN_LOCK = 5, 60.0, 30.0


class Session:
    """One connected client. Transports subclass it and implement send_raw()/close()/poll()."""

    def __init__(self, hub, peer):
        self.hub, self.peer = hub, peer
        self.id = hub.next_sid()
        self.user = None
        self.room = None
        self.slot = None
        self.dead = False
        self.t_last = time.monotonic()
        # --- metrics (TCP vs UDP comparison)
        self.bytes_in = self.bytes_out = self.msgs_in = self.msgs_out = 0
        self.snaps_sent = self.snaps_skipped = self.retransmits = 0
        self.client_gap = self.client_rx = 0
        self.rtt_last = None
        self.rtt_samples = []
        self.mark = {}

    # ---- implemented by transports
    def send(self, msg, reliable=True):
        raise NotImplementedError

    def send_final(self, msg):
        """Last message before the session is closed (UDP overrides it: best effort, repeated)."""
        self.send(msg)

    def close(self):
        pass

    def poll(self, now):
        pass

    # ---- helpers
    def count_out(self, msg, nbytes):
        self.msgs_out += 1
        self.bytes_out += nbytes
        if msg.get("t") == "snap":
            self.snaps_sent += 1

    def mark_start(self):
        self.mark = {"sent": self.snaps_sent, "gap": self.client_gap, "retx": self.retransmits,
                     "bout": self.bytes_out, "bin": self.bytes_in}
        self.rtt_samples = []

    def match_metrics(self):
        m, r = self.mark or {}, self.rtt_samples
        return {"rtt_avg": round(sum(r) / len(r), 1) if r else None, "rtt_max": round(max(r), 1) if r else None,
                "snaps_sent": self.snaps_sent - m.get("sent", 0), "snaps_lost": max(0, self.client_gap - m.get("gap", 0)),
                "retransmits": self.retransmits - m.get("retx", 0),
                "bytes_out": self.bytes_out - m.get("bout", 0), "bytes_in": self.bytes_in - m.get("bin", 0)}

    def rtt_avg(self):
        r = self.rtt_samples[-30:]
        return round(sum(r) / len(r), 1) if r else None


class Room:
    def __init__(self, rid, name, host):
        self.id, self.name, self.host = rid, name, host
        self.members = collections.OrderedDict()      # username -> Session
        self.state = "waiting"                        # waiting | playing
        self.game = None
        self.gid = 0
        self.started = 0.0


class Hub:
    def __init__(self, proto, db_path, data_dir):
        self.proto = proto
        self.db = storage.Database(db_path)
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.sessions, self.online, self.rooms = {}, {}, {}
        self._sid = self._rid = self._gid = 0
        self.fails = {}                                # login throttling
        self.logs = collections.deque(maxlen=60)
        self.t0 = time.time()

    # ------------------------------------------------------------------ utils
    def next_sid(self):
        self._sid += 1
        return self._sid

    def log(self, text):
        line = time.strftime("%H:%M:%S ") + text
        self.logs.append(line)
        print(line, flush=True)

    def _err(self, s, text):
        s.send({"t": "err", "msg": text})

    # ------------------------------------------------------------------ connection life cycle
    def on_connect(self, s):
        self.sessions[s.id] = s
        s.send({"t": "hello", "proto": self.proto, "tick": engine.TICK_RATE})
        self.log(f"[{self.proto}] connection #{s.id} from {s.peer}")

    def disconnect(self, s, why):
        if s.dead:
            return
        s.dead = True
        self.sessions.pop(s.id, None)
        if s.user and self.online.get(s.user.lower()) is s:
            del self.online[s.user.lower()]
        if s.room:
            self._leave_room(s, forced=True)
        s.close()
        self.log(f"[{self.proto}] #{s.id} {s.user or '-'} disconnected ({why})")

    def on_message(self, s, msg):
        if s.dead or not isinstance(msg, dict):
            return
        s.t_last = time.monotonic()
        s.msgs_in += 1
        handler = getattr(self, "_h_" + str(msg.get("t")), None)
        if handler is None:
            return
        try:
            handler(s, msg)
        except Exception as e:                           # a bad client must never crash the server
            self.log(f"!! handler {msg.get('t')} failed for #{s.id}: {e!r}")
            try:
                self._err(s, "Server error")
            except Exception:
                pass

    # ------------------------------------------------------------------ heartbeat
    def _h_ping(self, s, m):
        s.send({"t": "pong", "ts": m.get("ts")}, reliable=False)
        rtt = m.get("rtt")
        if isinstance(rtt, (int, float)) and 0 <= rtt < 10000:
            s.rtt_last = rtt
            s.rtt_samples.append(rtt)
            del s.rtt_samples[:-600]
        if isinstance(m.get("gap"), int) and isinstance(m.get("rx"), int):
            s.client_gap, s.client_rx = m["gap"], m["rx"]

    # ------------------------------------------------------------------ accounts
    def _throttled(self, name):
        rec = self.fails.get(str(name).lower())
        now = time.monotonic()
        if rec and rec[2] > now:
            return int(rec[2] - now) + 1
        return 0

    def _fail(self, name):
        key, now = str(name).lower(), time.monotonic()
        cnt, first, _ = self.fails.get(key, (0, now, 0))
        if now - first > LOGIN_WINDOW:
            cnt, first = 0, now
        cnt += 1
        self.fails[key] = (cnt, first, now + LOGIN_LOCK if cnt >= LOGIN_MAX_FAILS else 0)

    def _h_register(self, s, m):
        if s.user:
            return self._err(s, "Already signed in")
        name, err = self.db.register(m.get("u"), m.get("p"))
        if err:
            return s.send({"t": "auth_err", "msg": err})
        self.log(f"[{self.proto}] new account: {name}")
        self._signed_in(s, name)

    def _h_login(self, s, m):
        if s.user:
            return self._err(s, "Already signed in")
        wait = self._throttled(m.get("u"))
        if wait:
            return s.send({"t": "auth_err", "msg": f"Too many attempts, wait {wait}s"})
        name, err = self.db.verify(m.get("u"), m.get("p"))
        if err:
            self._fail(m.get("u"))
            self.log(f"[{self.proto}] failed login for {str(m.get('u'))[:16]!r} from {s.peer}")
            return s.send({"t": "auth_err", "msg": err})
        self.fails.pop(name.lower(), None)
        self._signed_in(s, name)

    def _signed_in(self, s, name):
        old = self.online.get(name.lower())
        if old is not None and old is not s:
            old.send_final({"t": "kicked", "msg": "Signed in from another device"})
            self.disconnect(old, "replaced by a new login")
        s.user = name
        self.online[name.lower()] = s
        s.send({"t": "auth_ok", "user": name, "stats": self.db.user_stats(name)})
        s.send(self._rooms_msg())
        s.send(self._lb_msg())
        self.log(f"[{self.proto}] {name} signed in from {s.peer}")

    def _h_logout(self, s, m):
        if not s.user:
            return
        if s.room:
            self._leave_room(s, forced=False)
        if self.online.get(s.user.lower()) is s:
            del self.online[s.user.lower()]
        self.log(f"[{self.proto}] {s.user} signed out")
        s.user = None

    # ------------------------------------------------------------------ lobby / rooms
    def _rooms_msg(self):
        return {"t": "rooms", "rooms": [
            {"id": r.id, "name": r.name, "host": r.host, "n": len(r.members), "max": MAX_PLAYERS, "state": r.state}
            for r in self.rooms.values()]}

    def _lb_msg(self):
        return {"t": "lb", "rows": self.db.leaderboard(10)}

    def _room_msg(self, r):
        return {"t": "room", "id": r.id, "name": r.name, "host": r.host, "state": r.state,
                "max": MAX_PLAYERS, "min": MIN_PLAYERS, "players": list(r.members.keys())}

    def _broadcast_rooms(self):
        msg = self._rooms_msg()
        for s in list(self.online.values()):
            if s.room is None:
                s.send(msg)

    def _send_room(self, r):
        msg = self._room_msg(r)
        for s in list(r.members.values()):
            s.send(msg)

    def _need_user(self, s):
        if not s.user:
            self._err(s, "Sign in first")
            return False
        return True

    def _h_rooms(self, s, m):
        if self._need_user(s):
            s.send(self._rooms_msg())

    def _h_lb(self, s, m):
        if self._need_user(s):
            s.send(self._lb_msg())

    def _h_create(self, s, m):
        if not self._need_user(s):
            return
        if s.room:
            return self._err(s, "Leave your current room first")
        name = str(m.get("name") or "").strip()[:20] or f"{s.user}'s room"
        self._rid += 1
        room = Room(self._rid, name, s.user)
        self.rooms[room.id] = room
        self.log(f"[{self.proto}] {s.user} created room #{room.id} '{name}'")
        self._join(room, s)

    def _join(self, room, s):
        room.members[s.user] = s
        s.room = room
        self._send_room(room)
        self._broadcast_rooms()

    def _h_join(self, s, m):
        if not self._need_user(s):
            return
        if s.room:
            return self._err(s, "Leave your current room first")
        room = self.rooms.get(m.get("room"))
        if room is None:
            return self._err(s, "This room no longer exists")
        if room.state != "waiting":
            return self._err(s, "A match is already running in this room")
        if len(room.members) >= MAX_PLAYERS:
            return self._err(s, "Room is full")
        self.log(f"[{self.proto}] {s.user} joined room #{room.id}")
        self._join(room, s)

    def _h_leave(self, s, m):
        if s.user and s.room:
            self._leave_room(s, forced=False)

    def _leave_room(self, s, forced):
        room = s.room
        room.members.pop(s.user, None)
        if room.game is not None and s.slot is not None:
            room.game.player_left(s.slot)
            for o in room.members.values():
                o.send({"t": "notice", "msg": f"{s.user} {'lost connection' if forced else 'left the match'}"})
        s.room, s.slot = None, None
        if not forced:
            s.send({"t": "left"})
        if not room.members:
            self.rooms.pop(room.id, None)
            self.log(f"[{self.proto}] room #{room.id} closed (empty)")
        else:
            if room.host == s.user:
                room.host = next(iter(room.members))
            self._send_room(room)
        self._broadcast_rooms()

    def _h_start(self, s, m):
        if not self._need_user(s):
            return
        room = s.room
        if room is None:
            return self._err(s, "You are not in a room")
        if room.host != s.user:
            return self._err(s, "Only the host can start the match")
        if room.state != "waiting":
            return self._err(s, "Match already running")
        if len(room.members) < MIN_PLAYERS:
            return self._err(s, f"Need at least {MIN_PLAYERS} players")
        names = list(room.members.keys())
        room.game = engine.Game(names)
        room.state, room.started = "playing", time.time()
        self._gid += 1
        room.gid = self._gid
        players = [{"slot": i, "u": n} for i, n in enumerate(names)]
        for i, n in enumerate(names):
            sess = room.members[n]
            sess.slot = i
            sess.mark_start()
            sess.send({"t": "start", "gid": room.gid, "you": i, "players": players,
                       "tick": engine.TICK_RATE, "match_ms": engine.MATCH_MS})
        self.log(f"[{self.proto}] match started in room #{room.id}: {', '.join(names)}")
        self._send_room(room)
        self._broadcast_rooms()

    def _h_in(self, s, m):
        room = s.room
        if room is None or room.game is None or s.slot is None or m.get("g") != room.gid:
            return
        room.game.set_input(s.slot, m.get("d", -1), m.get("b", 0), m.get("n", 0))

    # ------------------------------------------------------------------ match loop
    def _tick_rooms(self):
        for room in list(self.rooms.values()):
            g = room.game
            if g is None:
                continue
            g.tick()
            snap = g.snapshot()
            for s in list(room.members.values()):
                s.send(snap, reliable=False)
            if g.finished:
                self._finish(room)

    def _finish(self, room):
        g, res = room.game, room.game.result
        metrics = {n: s.match_metrics() for n, s in room.members.items()}
        mid = self.db.record_match(self.proto, room.name, room.started, res, metrics)
        board = ", ".join(f"{r['rank']}.{r['u']}={r['score']}" for r in res["ranking"])
        self.log(f"[{self.proto}] match #{mid} over ({res['reason']}): winner {res['winner']} | {board}")
        end = {"t": "end", "match": mid, **res}
        for s in list(room.members.values()):
            s.slot = None
            s.send(end)
        room.game, room.state = None, "waiting"
        self._send_room(room)
        self._broadcast_rooms()

    # ------------------------------------------------------------------ background loop
    async def run(self):
        dt = 1.0 / engine.TICK_RATE
        nxt = time.monotonic()
        last_slow = 0.0
        while True:
            nxt += dt
            now = time.monotonic()
            for s in list(self.sessions.values()):
                s.poll(now)
            self._tick_rooms()
            if now - last_slow >= 1.0:
                last_slow = now
                for s in list(self.sessions.values()):
                    if now - s.t_last > IDLE_TIMEOUT:
                        self.disconnect(s, f"no packet for {IDLE_TIMEOUT:.0f}s")
                self._dump_live()
            delay = nxt - time.monotonic()
            if delay < -1.0:                              # we fell far behind: resynchronise
                nxt = time.monotonic()
            await asyncio.sleep(max(0.0, delay))

    def _dump_live(self):
        """Snapshot of the server state for the dashboard (written atomically every second)."""
        games = []
        for r in self.rooms.values():
            if r.game:
                g = r.game
                games.append({"room": r.name, "phase": g.phase, "left_s": int(max(0, g.left_ms) / 1000),
                              "players": [{"u": p.name, "score": p.score, "kills": p.kills, "alive": p.alive,
                                           "dropped": p.dropped} for p in g.players]})
        data = {
            "proto": self.proto, "ts": time.time(), "uptime_s": int(time.time() - self.t0),
            "online": [{"user": s.user or "(connecting)", "peer": str(s.peer), "room": s.room.name if s.room else None,
                        "rtt": s.rtt_last, "rtt_avg": s.rtt_avg(), "bytes_in": s.bytes_in, "bytes_out": s.bytes_out,
                        "retx": s.retransmits, "lost": s.client_gap, "skipped": s.snaps_skipped}
                       for s in self.sessions.values()],
            "rooms": self._rooms_msg()["rooms"], "games": games, "log": list(self.logs)[-25:],
        }
        path = os.path.join(self.data_dir, f"live_{self.proto}.json")
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
