# Bomberman Arena - Computer Networks Project #2

Competitive multiplayer Bomberman (2 to 4 players) built on sockets, in two versions of the SAME game:

| | TCP-only version | UDP-only version |
|---|---|---|
| Server | `tcp_version/server_tcp.py` (raw TCP socket, port 8000) | `udp_version/server_udp.py` (raw UDP socket, port 9000) |
| Clients | pygame client (plain TCP socket) | pygame client (plain UDP socket) |
| Reliability | provided by TCP | written by us in `arena/reliable.py` |

Both servers run the same `arena/hub.py` (accounts, lobby, rooms, match, scores, disconnections) and the same
`arena/engine.py` (the original game rules, now running on the server only).

## Quick start (server PC = your PC)

1. Install Python 3.9+ (tick "Add Python to PATH").  For the UDP clients also: `pip install pygame`
2. Windows, once, as administrator: `open_firewall.bat`
3. Start ONE server:
   * TCP version : `run_tcp_server.bat`   -> it prints the address, e.g. `192.168.1.20`
   * UDP version : `run_udp_server.bat`
4. Scores on your PC: `run_dashboard.bat` -> http://localhost:8080 (only reachable from the server PC)
5. Players
   * TCP: on each of the 3 PCs `pip install pygame` then `run_client.bat tcp SERVER_IP`
   * UDP: on each of the 3 PCs `pip install pygame` then `run_client.bat udp SERVER_IP`
     (or `python client/pygame_client.py --proto udp --host SERVER_IP`).
6. One player creates a room, the others join, the host starts the match.
   Controls: arrows = move, Space = bomb.

Accounts, statistics and match history are stored on the server PC in `data/arena.db` (SQLite,
passwords stored as salted PBKDF2 hashes).

## Online server (Azure, both versions)

`run_client.bat` is for a LAN server. To play on the deployed server (port 443, because the university network blocks 8000):
```
python client/pygame_client.py --proto tcp --host bomberman-arena.germanywestcentral.cloudapp.azure.com --port 443
python client/pygame_client.py --proto udp --host bomberman-arena.germanywestcentral.cloudapp.azure.com --port 443
```
See [docs/DEPLOYMENT_AZURE.md](docs/DEPLOYMENT_AZURE.md): Azure VM, nginx TCP pass-through on TCP 443, UDP server on UDP 443, and why.

## Play without installing anything (Windows .exe)

`BombermanArena-TCP.exe` and `BombermanArena-UDP.exe` are the same pygame client bundled with Python and pygame.
Double-click to connect to the online server; options still work, e.g. for a LAN server:
`BombermanArena-UDP.exe --host 192.168.1.20 --port 9000`.

Windows may show "Windows protected your PC" (the files are not code-signed): *More info* -> *Run anyway*.

Build them yourself: `pip install pyinstaller` then `python tools/build_exe.py` -> `dist/`.

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
arena/              shared code: engine.py hub.py storage.py reliable.py netclient.py
tcp_version/        server_tcp.py
udp_version/        server_udp.py
client/             pygame_client.py (--proto tcp|udp)
dashboard/          dashboard.py (server PC only, reads the database)
tools/              selftest.py bots.py
assets/images/      sprites of the original game
original_bomberman/ the original single-player game, untouched
data/               created at run time (arena.db, live_*.json)
```
