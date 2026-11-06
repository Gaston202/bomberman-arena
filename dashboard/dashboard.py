"""
Dashboard for the SERVER PC (read only).  It does NOT take part in the game protocol: it only reads
the local database (data/arena.db) and the live_*.json files that the servers write every second.

    python dashboard/dashboard.py            -> http://localhost:8080   (only reachable from this PC)
    python dashboard/dashboard.py --console  -> text scoreboard in the terminal (no HTTP at all)
    python dashboard/dashboard.py --public   -> reachable from other devices too
"""
import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from arena.storage import Database  # noqa: E402

DATA = os.path.join(ROOT, "data")


def state(db):
    live = {}
    for proto in ("tcp", "udp"):
        try:
            with open(os.path.join(DATA, f"live_{proto}.json"), encoding="utf-8") as f:
                d = json.load(f)
            d["age"] = round(time.time() - d["ts"], 1)
            live[proto] = d if d["age"] < 5 else None        # server stopped => not live
        except (OSError, ValueError):
            live[proto] = None
    return {"live": live, "leaderboard": db.leaderboard(20), "matches": db.recent_matches(8),
            "comparison": db.comparison(), "users": db.user_count()}


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Arena dashboard</title>
<style>
body{margin:0;font:15px system-ui,Segoe UI,sans-serif;background:#182036;color:#eef0f6;padding:18px}
h1{font:900 26px "Arial Black",Arial,sans-serif;margin:0 0 14px;color:#ffb703}
h2{font:900 16px "Arial Black",Arial,sans-serif;margin:0 0 8px}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}
.c{background:#222d4a;border:2px solid #3a4a75;border-radius:6px;padding:14px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:#9aa7c7;font-weight:600}
td,th{padding:4px 6px;border-bottom:1px solid #3a4a75}.n{text-align:right;font-variant-numeric:tabular-nums}
.off{color:#9aa7c7}.ok{color:#6bbf59}.w{color:#ffb703}pre{margin:0;font-size:12px;white-space:pre-wrap;color:#9aa7c7}
</style></head><body><h1>Bomberman Arena - server dashboard</h1><div class="g" id="g"></div>
<script>
const E=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const tbl=(h,rows)=>"<table><tr>"+h.map((x,i)=>`<th class="${i?'n':''}">${x}</th>`).join("")+"</tr>"+
  (rows.length?rows.map(r=>"<tr>"+r.map((x,i)=>`<td class="${i?'n':''}">${E(x)}</td>`).join("")+"</tr>").join(""):`<tr><td colspan=${h.length} class=off>nothing yet</td></tr>`)+"</table>";
async function tick(){
 const d=await (await fetch("/api/state")).json(); let h="";
 for(const p of ["tcp","udp"]){const l=d.live[p];
  h+=`<div class="c"><h2>${p.toUpperCase()} server <span class="${l?'ok':'off'}">${l?'running, up '+l.uptime_s+'s':'stopped'}</span></h2>`;
  if(l){
   h+="<b>Connected</b>"+tbl(["Player","Room","RTT ms","Lost snaps","Retransmits","KB out"],l.online.map(o=>[o.user,o.room||"-",o.rtt_avg??"-",o.lost,o.retx,(o.bytes_out/1024).toFixed(0)]));
   h+="<br><b>Live matches</b>"+(l.games.length?l.games.map(g=>`<div class=w>${E(g.room)} - ${g.phase}, ${g.left_s}s left</div>`+tbl(["Player","Score","Kills","State"],g.players.map(q=>[q.u,q.score,q.kills,q.dropped?"disconnected":q.alive?"alive":"out"]))).join(""):"<div class=off>no match running</div>");
   h+=`<br><b>Server log</b><pre>${E(l.log.slice(-8).join("\\n"))}</pre>`;}
  h+="</div>";}
 h+=`<div class="c"><h2>Leaderboard (all matches, ${d.users} accounts)</h2>`+tbl(["#","Player","Games","Wins","Points","Best","Kills"],d.leaderboard.map((r,i)=>[i+1,r.username,r.games,r.wins,r.total_score,r.best_score,r.kills]))+"</div>";
 h+=`<div class="c"><h2>Recent matches</h2>`+d.matches.map(m=>`<div class=w>#${m.id} ${m.proto.toUpperCase()} - ${E(m.room)} - winner ${E(m.winner)} (${m.duration}s)</div><div class=off>${m.players.map(p=>p.rank+'. '+E(p.username)+' '+p.score).join(' | ')}</div>`).join("")+"</div>";
 h+=`<div class="c"><h2>TCP vs UDP (measured, all finished matches)</h2>`+tbl(["Proto","Matches","RTT avg ms","RTT max","Snapshots lost %","Retransmits","KB out/player"],d.comparison.map(c=>[c.proto.toUpperCase(),c.matches,(c.rtt_avg??0).toFixed(1),(c.rtt_max??0).toFixed(0),c.snaps_sent?(100*c.snaps_lost/c.snaps_sent).toFixed(2):"-",c.retransmits??0,((c.bytes_out??0)/1024).toFixed(0)]))+"</div>";
 document.getElementById("g").innerHTML=h;}
tick();setInterval(tick,1500);
</script></body></html>"""


def console(db):
    while True:
        s = state(db)
        os.system("cls" if os.name == "nt" else "clear")
        print("=== BOMBERMAN ARENA - scoreboard ===")
        for p, l in s["live"].items():
            print(f"[{p.upper()}] " + ("online: " + ", ".join(o["user"] for o in l["online"]) if l else "server stopped"))
            for g in (l or {}).get("games", []):
                print(f"   live match '{g['room']}' {g['phase']} {g['left_s']}s: " +
                      " | ".join(f"{q['u']} {q['score']}" for q in g["players"]))
        print("\nLeaderboard:")
        for i, r in enumerate(s["leaderboard"][:10], 1):
            print(f" {i:2}. {r['username']:<16} {r['total_score']:>6} pts  {r['wins']} wins  {r['games']} games")
        time.sleep(1.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--public", action="store_true", help="listen on all interfaces instead of localhost only")
    ap.add_argument("--console", action="store_true")
    ap.add_argument("--db", default=os.path.join(DATA, "arena.db"))
    a = ap.parse_args()
    db = Database(a.db)
    if a.console:
        try:
            return console(db)
        except KeyboardInterrupt:
            return

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/api/state"):
                body, ctype = json.dumps(state(Database(a.db))).encode(), "application/json"
            else:
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    host = "0.0.0.0" if a.public else "127.0.0.1"
    print(f"Dashboard: http://localhost:{a.port}   (listening on {host})")
    ThreadingHTTPServer((host, a.port), H).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
