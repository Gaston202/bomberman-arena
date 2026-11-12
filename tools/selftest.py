"""
End-to-end self test: starts a real server (TCP or UDP) on this PC with a throw-away database,
connects real socket clients and checks every mandatory feature of the project statement.

    python tools/selftest.py              # both versions
    python tools/selftest.py --proto udp --loss 0.25     # UDP with 25% simulated packet loss
"""
import argparse
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from arena import engine, storage      # noqa: E402
from arena.netclient import make_client          # noqa: E402

OK = FAIL = 0


def check(cond, label):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"   PASS  {label}")
    else:
        FAIL += 1
        print(f"   FAIL  {label}")
    return cond


class T:
    """Tiny test client: collects messages, waits for a message matching a predicate."""

    def __init__(self, proto, port, loss=0.0):
        self.net = make_client(proto, "127.0.0.1", port, loss)
        self.net.start()
        self.msgs, self.snap = [], None

    def pump(self):
        for m in self.net.poll():
            if m.get("t") == "snap":
                self.snap = m
            else:
                self.msgs.append(m)

    def wait(self, t, timeout=8.0, **match):
        end = time.time() + timeout
        while time.time() < end:
            self.pump()
            for i, m in enumerate(self.msgs):
                if m.get("t") == t and all(m.get(k) == v for k, v in match.items()):
                    return self.msgs.pop(i)
            time.sleep(0.02)
        return None

    def send(self, **m):
        self.net.send(m)


def start_server(proto, port, data_dir, loss):
    script = os.path.join(ROOT, f"{proto}_version", f"server_{proto}.py")
    cmd = [sys.executable, script, "--port", str(port), "--data-dir", data_dir]
    if proto == "udp" and loss:
        cmd += ["--loss", str(loss)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.0)
    return p


def run(proto, loss):
    global OK, FAIL
    port = 18000 if proto == "tcp" else 19000
    tmp = tempfile.mkdtemp()
    print(f"\n=== {proto.upper()} version" + (f" with {loss:.0%} simulated loss" if loss else "") + " ===")
    srv = start_server(proto, port, tmp, loss)
    clients = []
    try:
        a = T(proto, port, loss)
        clients.append(a)
        check(a.wait("hello"), "connection established (hello)")
        # ---- registration / login
        a.send(t="register", u="x", p="secret")
        check(a.wait("auth_err"), "register: too short username rejected")
        a.send(t="register", u="alice", p="abc")
        check(a.wait("auth_err"), "register: too short password rejected")
        a.send(t="register", u="alice", p="secret1")
        check(a.wait("auth_ok"), "register alice -> signed in")
        b = T(proto, port, loss)
        clients.append(b)
        b.wait("hello")
        b.send(t="register", u="Alice", p="other")
        check(b.wait("auth_err"), "register: duplicate name rejected (case-insensitive)")
        b.send(t="login", u="alice", p="wrong")
        check(b.wait("auth_err"), "login: wrong password rejected")
        b.send(t="login", u="alice", p="secret1")
        check(b.wait("auth_ok"), "login: correct password accepted")
        check(a.wait("kicked"), "login: old session of the same account is kicked")
        b.send(t="logout")
        time.sleep(0.2)
        b.send(t="register", u="bob", p="secret2")
        check(b.wait("auth_ok"), "second account bob")
        a = T(proto, port, loss)
        clients[0] = a
        a.wait("hello")
        a.send(t="login", u="alice", p="secret1")
        a.wait("auth_ok")
        c = T(proto, port, loss)
        clients.append(c)
        c.wait("hello")
        c.send(t="register", u="carol", p="secret3")
        c.wait("auth_ok")
        db = storage.Database(os.path.join(tmp, "arena.db"))
        row = db.conn.execute("SELECT pwhash, salt FROM users WHERE username='alice'").fetchone()
        check(row and b"secret1" not in bytes(row["pwhash"]) and len(row["salt"]) == 16,
              "storage: password stored as salted hash in the local database")
        # ---- lobby / rooms
        b.wait("rooms")
        a.send(t="create", name="Arena 1")
        room = a.wait("room")
        check(room and room["host"] == "alice", "lobby: room created, alice is host")
        rid = room["id"]
        check(b.wait("rooms") and True, "lobby: other players are notified")
        b.send(t="join", room=rid)
        check(b.wait("room") and a.wait("room"), "join: bob enters, members updated")
        c.send(t="join", room=rid)
        check(c.wait("room"), "join: carol enters")
        c.send(t="leave")
        check(c.wait("left"), "leave: carol leaves the room")
        c.send(t="join", room=rid)
        c.wait("room")
        b.send(t="start")
        check(b.wait("err"), "start: only the host may start")
        # ---- match
        a.send(t="start")
        st = {n: x.wait("start") for n, x in (("a", a), ("b", b), ("c", c))}
        check(all(st.values()), "start: every player receives the start message")
        end = time.time() + 1.0
        while time.time() < end:
            a.pump()
            time.sleep(0.02)
        check(a.snap is not None and len(a.snap["g"]) == 169, "game: snapshots stream to the clients")
        end = time.time() + 6
        while time.time() < end and (a.snap is None or a.snap["ph"] != "play"):
            a.pump()
            time.sleep(0.05)
        check(a.snap and a.snap["ph"] == "play", "game: countdown finished, play phase")
        gid, slot_a = st["a"]["gid"], st["a"]["you"]
        for n in range(1, 4):                                      # alice plants a bomb on her own tile and stays
            a.send(t="in", g=gid, n=n, d=-1, b=1)
            time.sleep(0.07)
        end = time.time() + 5
        while time.time() < end and not a.snap["b"]:
            a.pump()
            time.sleep(0.05)
        check(a.snap and len(a.snap["b"]) == 1, "game: bomb planted (real-time action)")
        b.net.close()                                              # bob loses connection mid-game
        check(a.wait("notice", timeout=10), "disconnect: others are told that bob dropped")
        res = a.wait("end", timeout=15)
        check(res is not None, "match ends and results are sent")
        if res:
            names = [r["u"] for r in res["ranking"]]
            check(res["winner"] == "carol" and names[-1] == "bob", "results: winner = last survivor, dropped player ranked last")
            check(res["ranking"][0]["score"] >= engine.SCORE_SURVIVOR, "scores: survivor bonus counted")
        check(c.wait("end", timeout=5) is not None, "results received by every remaining player")
        r2 = a.wait("room", timeout=5, state="waiting", players=["alice", "carol"])
        check(r2 and r2["state"] == "waiting" and r2["players"] == ["alice", "carol"],
              "room goes back to waiting without the dropped player")
        db2 = storage.Database(os.path.join(tmp, "arena.db"))
        lb = {r["username"]: r for r in db2.leaderboard()}
        check("carol" in lb and lb["carol"]["wins"] == 1, "storage: match saved, carol has 1 win in the leaderboard")
        check(db2.conn.execute("SELECT COUNT(*) FROM matches WHERE proto=?", (proto,)).fetchone()[0] == 1,
              "storage: match history row written")
        a.send(t="lb")
        check(a.wait("lb"), "leaderboard can be requested by clients")
        # ---- garbage must not crash the server
        if proto == "tcp":
            s = socket.create_connection(("127.0.0.1", port), timeout=3)
            s.sendall(b"\x00\xff garbage\n{\"t\":\"login\",\"u\":5}\n")
            s.close()
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(b"\x00\xff garbage", ("127.0.0.1", port))
            s.close()
        time.sleep(0.5)
        d = T(proto, port, loss)
        clients.append(d)
        check(d.wait("hello"), "robustness: server still accepts clients after garbage input")
    finally:
        for x in clients:
            x.net.close()
        srv.terminate()
        try:
            srv.wait(timeout=3)
        except subprocess.TimeoutExpired:
            srv.kill()


def engine_tests():
    print("\n=== engine ===")
    g = engine.Game(["a", "b"], seed=3, countdown_ms=0)
    g.tick()
    for x in range(1, 6):
        g.grid[x][1] = 0
    g.players[1].x, g.players[1].y = 3 * 4, 4
    g.players[0].bomb_req = 1
    for _ in range(200):
        g.tick()
        if g.finished:
            break
    check(g.finished and g.players[0].kills == 1, "engine: bomb kills the opponent in range, owner gets the kill")
    g = engine.Game(["a", "b"], seed=3, countdown_ms=0)
    g.tick()
    g.set_input(0, 1, 0, 5)
    g.set_input(0, 3, 0, 4)                                      # older packet (UDP reorder) must be ignored
    check(g.players[0].want_dir == 1, "engine: stale input packets are ignored")
    snap = g.snapshot()
    check(len(str(snap)) < 1200, f"engine: snapshot fits in one UDP datagram ({len(str(snap))} bytes)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", choices=["tcp", "udp", "both"], default="both")
    ap.add_argument("--loss", type=float, default=0.0)
    a = ap.parse_args()
    engine_tests()
    for p in (["tcp", "udp"] if a.proto == "both" else [a.proto]):
        run(p, a.loss if p == "udp" else 0.0)
    print(f"\nRESULT: {OK} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
