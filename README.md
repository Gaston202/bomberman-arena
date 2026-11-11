# Bomberman Arena - Computer Networks Project #2

Competitive multiplayer Bomberman (2 to 4 players) built on sockets, in two versions of the SAME game:

| | TCP-only version | UDP-only version |
|---|---|---|
| Server | `tcp_version/server_tcp.py` (TCP 8000) | `udp_version/server_udp.py` (UDP 9000) |
| Clients | **Web browser** (open `http://SERVER_IP:8000`) or pygame client | pygame client (a browser cannot use UDP) |
| Reliability | provided by TCP | written by us in `arena/reliable.py` |

Both servers run the same `arena/hub.py` (accounts, lobby, rooms, match, scores, disconnections) and the same
`arena/engine.py` (the original game rules, now running on the server only).

## Quick start (server PC = your PC)

1. Install Python 3.9+ (tick "Add Python to PATH").  For the UDP clients also: `pip install pygame`
2. Windows, once, as administrator: `open_firewall.bat`
3. Start ONE server:
   * TCP version : `run_tcp_server.bat`   -> it prints the address, e.g. `http://192.168.1.20:8000`
   * UDP version : `run_udp_server.bat`
4. Scores on your PC: `run_dashboard.bat` -> http://localhost:8080 (only reachable from the server PC)
5. Players
   * TCP: on each of the 3 devices open `http://SERVER_IP:8000` in the browser, create an account, join the room.
   * UDP: on each of the 3 PCs `pip install pygame` then `run_client.bat udp SERVER_IP`
     (or `python client/pygame_client.py --proto udp --host SERVER_IP`).
6. One player creates a room, the others join, the host starts the match.
   Controls: arrows (or WASD in the browser) = move, Space = bomb.

Accounts, statistics and match history are stored on the server PC in `data/arena.db` (SQLite,
passwords stored as salted PBKDF2 hashes).

## Deploy the TCP version on Azure (HTTPS, works on the university network)

See [docs/DEPLOYMENT_AZURE.md](docs/DEPLOYMENT_AZURE.md): Azure VM + nginx + Let's Encrypt, and why port 443 was needed.

## Deploy the TCP version on Render

The repo contains a `render.yaml` Blueprint.

1. Push this repo to GitHub.
2. On https://dashboard.render.com click **New > Blueprint**, pick the repo, and confirm.
   (Or **New > Web Service**: runtime *Python*, build command `echo ok`,
   start command `python tcp_version/server_tcp.py --host 0.0.0.0`.)
3. Open `https://<your-service>.onrender.com`. The page connects over `wss://` automatically.

What changes on Render:
* The server reads its port from `$PORT` (Render sets it). Locally it still defaults to 8000.
* **Browsers only.** Render forwards HTTP/HTTPS and WebSockets but not raw TCP, so the pygame
  client (`--proto tcp`, JSON lines) cannot connect to the Render deployment. The UDP version cannot run there either.
* **Free plan limits:** the service sleeps after ~15 min without traffic (the first visit then takes ~1 min),
  and the disk is wiped on every restart or deploy, so `arena.db` (accounts, scores) is reset.
  To keep data, use a paid instance with a persistent disk and set `ARENA_DATA_DIR` to the disk mount path.
* The dashboard (`dashboard/dashboard.py`) is not deployed; it only reads the local database.

## Test without 4 computers
```
python tools/selftest.py                      # 68 automatic checks, both versions
python tools/selftest.py --proto udp --loss 0.25
python tools/bots.py --proto tcp --host 127.0.0.1 --bots 3     # a full match played by robots
python tools/bots.py --proto udp --host 127.0.0.1 --bots 3 --loss 0.1
python udp_version/server_udp.py --loss 0.15  # server drops 15% of its datagrams (lab experiment)
```

## Folders
```
arena/              shared code: engine.py hub.py storage.py reliable.py wsproto.py netclient.py
tcp_version/        server_tcp.py + web/index.html (browser client)
udp_version/        server_udp.py
client/             pygame_client.py (--proto tcp|udp)
dashboard/          dashboard.py (server PC only, reads the database)
tools/              selftest.py bots.py
assets/images/      sprites of the original game
original_bomberman/ the original single-player game, untouched
data/               created at run time (arena.db, live_*.json)
```
