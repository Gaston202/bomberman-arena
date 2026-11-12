"""
Entry point of the downloadable .exe clients (see tools/build_exe.py).

Double-clicking the .exe connects to the public Azure server on port 443, so players need nothing
installed.  The protocol is chosen by the build (BOMBERMAN_PROTO is baked in by tools/build_exe.py),
and every option can still be overridden, e.g. for the LAN demo:

    BombermanArena-UDP.exe --host 192.168.1.20 --port 9000
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pygame_client import main  # noqa: E402

ONLINE_HOST = "bomberman-arena.germanywestcentral.cloudapp.azure.com"
ONLINE_PORT = 443                 # TCP 443 -> nginx -> TCP server ; UDP 443 -> UDP server

try:
    from _build_proto import PROTO  # noqa: E402  (generated at build time)
except ImportError:
    PROTO = "tcp"

if __name__ == "__main__":
    main(proto=PROTO, host=ONLINE_HOST, port=ONLINE_PORT)
