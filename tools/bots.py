"""
Play a complete match with robot players and print the network metrics seen by each bot.
Useful to test the server on one PC and to produce numbers for the TCP vs UDP comparison slides.

    python tools/bots.py --proto tcp --host 127.0.0.1 --bots 3
    python tools/bots.py --proto udp --host 127.0.0.1 --bots 3 --loss 0.1     # 10% of the bots' datagrams dropped
"""
import argparse
import os
import random
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from arena.netclient import make_client  # noqa: E402

results = {}


def bot(idx, a):
    name, pw = f"bot{idx}", "botpass"
    net = make_client(a.proto, a.host, a.port, a.loss)
    net.start()
    state = {"scene": "connect", "gid": None, "n": 0, "b": 0, "d": -1, "room": None}
    t_dir = t_bomb = 0.0
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        for m in net.poll():
            t = m.get("t")
            if t == "_closed":
                results[name] = {"error": m.get("why")}
                return
            if t == "hello":
                net.send({"t": "register", "u": name, "p": pw})
            elif t == "auth_err" and "taken" in m.get("msg", ""):
                net.send({"t": "login", "u": name, "p": pw})
            elif t == "auth_ok":
                state["scene"] = "lobby"
                if idx == 1:
                    net.send({"t": "create", "name": "bots"})
            elif t == "rooms" and state["scene"] == "lobby" and idx != 1 and m["rooms"]:
                net.send({"t": "join", "room": m["rooms"][0]["id"]})
            elif t == "room":
                state["room"] = m
            elif t == "start":
                state.update(scene="game", gid=m["gid"], n=0, b=0)
            elif t == "end":
                results[name] = {"rank": [r["u"] for r in m["ranking"]].index(name) + 1, "winner": m["winner"],
                                 "reason": m["reason"], "rtt_ms": net.rtt_ms, "snap_rx": net.snap_rx,
                                 "snap_lost": net.snap_gap, "retransmits": getattr(net, "retransmits", 0),
                                 "KB_out": round(net.bytes_out / 1024, 1), "KB_in": round(net.bytes_in / 1024, 1)}
                net.close()
                return
        now = time.time()
        if idx == 1 and state["room"] and len(state["room"]["players"]) >= a.bots and state["scene"] == "lobby":
            net.send({"t": "start"})
            state["scene"] = "starting"
        if state["scene"] == "game":
            if now - t_dir > 0.4:
                t_dir, state["d"] = now, random.choice([0, 1, 2, 3, -1])
            if now - t_bomb > 4.0:
                t_bomb = now
                state["b"] += 1
            state["n"] += 1
            net.send({"t": "in", "g": state["gid"], "n": state["n"], "d": state["d"], "b": state["b"]}, reliable=False)
        time.sleep(1 / 15)
    results[name] = {"error": "timeout"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", choices=["tcp", "udp"], required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--bots", type=int, default=3)
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=200)
    a = ap.parse_args()
    ths = [threading.Thread(target=bot, args=(i, a)) for i in range(1, a.bots + 1)]
    for t in ths:
        t.start()
        time.sleep(0.3)
    for t in ths:
        t.join()
    print(f"\n{a.proto.upper()} match with {a.bots} bots" + (f", {a.loss:.0%} loss on bot datagrams" if a.loss else ""))
    for k in sorted(results):
        print(f"  {k}: {results[k]}")


if __name__ == "__main__":
    main()
