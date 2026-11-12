"""
Build the downloadable Windows clients (no Python or pygame needed on the players' PCs):

    pip install pyinstaller
    python tools/build_exe.py            ->  dist/BombermanArena-TCP.exe  and  dist/BombermanArena-UDP.exe

Each .exe is the normal pygame client (client/pygame_client.py, raw TCP or raw UDP socket)
started through client/play_online.py, so it connects to the Azure server on port 443 by default.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "client")
PROTO_FILE = os.path.join(CLIENT, "_build_proto.py")
IMAGES = os.path.join(ROOT, "assets", "images")


def build(proto):
    name = f"BombermanArena-{proto.upper()}"
    with open(PROTO_FILE, "w") as f:
        f.write(f'PROTO = "{proto}"\n')
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed",                          # one file, no console window
        "--name", name,
        "--paths", ROOT, "--paths", CLIENT,                 # so `arena` and `pygame_client` are found
        "--hidden-import", "_build_proto",
        "--add-data", f"{IMAGES}{os.pathsep}assets/images",  # sprites, read through sys._MEIPASS
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build", proto),
        "--specpath", os.path.join(ROOT, "build"),
        os.path.join(CLIENT, "play_online.py"),
    ]
    print(f"--- building {name}.exe")
    try:
        subprocess.run(cmd, check=True)
    finally:
        os.remove(PROTO_FILE)                               # build-time only, never committed


if __name__ == "__main__":
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("PyInstaller is missing: pip install pyinstaller")
    for p in ("tcp", "udp"):
        build(p)
    print("\nDone: see the dist/ folder")
