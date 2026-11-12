"""
Native pygame client.  Same screens and same messages for both versions: only --proto changes.
It is the client of BOTH versions: a plain TCP socket (--proto tcp) or a plain UDP socket (--proto udp).

    python client/pygame_client.py --proto udp --host 192.168.1.20
    python client/pygame_client.py --proto tcp --host 192.168.1.20
    options: --port N   --loss 0.1 (simulate 10% lost outgoing datagrams, UDP only)
"""
import argparse
import os
import sys
import time

SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# In a PyInstaller .exe the bundled files are unpacked to a temp folder given by sys._MEIPASS
ROOT = getattr(sys, "_MEIPASS", SOURCE_ROOT)
sys.path.insert(0, SOURCE_ROOT)

import pygame                                   # noqa: E402
from arena.netclient import make_client          # noqa: E402

IMG = os.path.join(ROOT, "assets", "images")
TILE, COLS = 48, 13
W, H = COLS * TILE + 260, COLS * TILE
BG, PANEL, CHALK, DIM = (24, 32, 54), (34, 45, 74), (238, 240, 246), (154, 167, 199)
SPARK, RED = (255, 183, 3), (239, 71, 111)
PCOL = [(245, 158, 11), (107, 191, 89), (106, 164, 216), (178, 123, 240)]
SKIN_DIR = "frbl"
TICK = 15


class App:
    def __init__(self, net, screen):
        self.net, self.screen = net, screen
        self.fonts = {s: pygame.font.Font(None, s) for s in (20, 26, 34, 60, 90)}
        self.scene = "connecting"
        self.user = None
        self.fields = {"u": "", "p": ""}
        self.focus = "u"
        self.msg = ""
        self.msg_until = 0
        self.rooms, self.sel, self.lb, self.room = [], 0, [], None
        self.game, self.snap, self.result = None, None, None
        self.bomb_count = self.n = 0
        self.last_input = self.last_lb = 0.0
        self.why = ""
        self.load_images()

    # ------------------------------------------------------------ assets
    def load_images(self):
        def ld(p, size=TILE):
            im = pygame.image.load(os.path.join(IMG, p)).convert_alpha()
            return pygame.transform.scale(im, (size, size))
        self.terrain = [ld("terrain/grass.png"), ld("terrain/block.png"), ld("terrain/box.png")]
        self.bomb = [ld(f"bomb/{i}.png") for i in (1, 2, 3)]
        self.expl = [ld(f"explosion/{i}.png") for i in (1, 2, 3)]
        self.pu = [ld("power_up/bomb.png"), ld("power_up/fire.png")]
        self.skin = []
        for slot in range(4):
            self.skin.append([[ld(f"hero/p{d}{f}.png") if slot == 0 else ld(f"enemy/e{slot}{d}{f}.png")
                               for f in range(3)] for d in SKIN_DIR])

    # ------------------------------------------------------------ helpers
    def text(self, s, pos, size=26, color=CHALK, center=False, right=False):
        im = self.fonts[size].render(str(s), True, color)
        r = im.get_rect()
        if center:
            r.center = pos
        elif right:
            r.topright = pos
        else:
            r.topleft = pos
        self.screen.blit(im, r)

    def flash(self, s, secs=3.5):
        self.msg, self.msg_until = s, time.time() + secs

    # ------------------------------------------------------------ network messages
    def on_msg(self, m):
        t = m.get("t")
        if t == "hello":
            if self.scene == "connecting":
                self.scene = "login"
        elif t == "_closed":
            self.scene, self.why = "closed", m.get("why", "")
        elif t == "auth_ok":
            self.user, self.scene = m["user"], "lobby"
            self.fields["p"] = ""
            self.msg = ""
        elif t == "auth_err":
            self.flash(m["msg"])
        elif t == "err" or t == "notice":
            self.flash(m["msg"])
        elif t == "kicked":
            self.scene, self.why = "closed", m["msg"]
        elif t == "rooms":
            self.rooms = m["rooms"]
            self.sel = min(self.sel, max(0, len(self.rooms) - 1))
        elif t == "lb":
            self.lb = m["rows"]
        elif t == "room":
            self.room = m
            if self.scene not in ("game", "results"):
                self.scene = "room"
        elif t == "left":
            self.room, self.scene = None, "lobby"
        elif t == "start":
            self.game = {"gid": m["gid"], "you": m["you"], "players": m["players"]}
            self.snap, self.bomb_count, self.n = None, 0, 0
            self.scene = "game"
        elif t == "snap":
            self.snap = m
        elif t == "end":
            self.result, self.scene = m, "results"

    # ------------------------------------------------------------ events
    def on_event(self, e):
        if e.type != pygame.KEYDOWN:
            return
        k, sc = e.key, self.scene
        if sc == "login":
            if k == pygame.K_TAB:
                self.focus = "p" if self.focus == "u" else "u"
            elif k == pygame.K_RETURN:
                self.net.send({"t": "login", **self.fields})
            elif k == pygame.K_F2:
                self.net.send({"t": "register", **self.fields})
            elif k == pygame.K_BACKSPACE:
                self.fields[self.focus] = self.fields[self.focus][:-1]
            elif e.unicode and e.unicode.isprintable() and len(self.fields[self.focus]) < 32:
                self.fields[self.focus] += e.unicode
        elif sc == "lobby":
            if k == pygame.K_UP:
                self.sel = max(0, self.sel - 1)
            elif k == pygame.K_DOWN:
                self.sel = min(max(0, len(self.rooms) - 1), self.sel + 1)
            elif k == pygame.K_RETURN and self.rooms:
                self.net.send({"t": "join", "room": self.rooms[self.sel]["id"]})
            elif k == pygame.K_c:
                self.net.send({"t": "create", "name": ""})
            elif k == pygame.K_ESCAPE:
                self.net.send({"t": "logout"})
                self.user, self.scene = None, "login"
        elif sc == "room":
            if k == pygame.K_RETURN and self.room and self.room["host"] == self.user:
                self.net.send({"t": "start"})
            elif k == pygame.K_ESCAPE:
                self.net.send({"t": "leave"})
        elif sc == "game":
            if k == pygame.K_SPACE:
                self.bomb_count += 1
            elif k == pygame.K_ESCAPE:
                self.net.send({"t": "leave"})
                self.scene = "lobby"
        elif sc == "results":
            if k in (pygame.K_RETURN, pygame.K_ESCAPE):
                self.scene = "room" if self.room else "lobby"

    # ------------------------------------------------------------ per frame
    def current_dir(self):
        keys = pygame.key.get_pressed()
        if keys[pygame.K_DOWN]:                     # same priority as the original game
            return 0
        if keys[pygame.K_RIGHT]:
            return 1
        if keys[pygame.K_UP]:
            return 2
        if keys[pygame.K_LEFT]:
            return 3
        return -1

    def update(self):
        for m in self.net.poll():
            self.on_msg(m)
        now = time.time()
        if self.scene == "game" and self.game and now - self.last_input >= 1.0 / TICK:
            self.last_input = now
            self.n += 1
            self.net.send({"t": "in", "g": self.game["gid"], "n": self.n, "d": self.dir_override
                           if getattr(self, "dir_override", None) is not None else self.current_dir(),
                           "b": self.bomb_count}, reliable=False)
        if self.scene == "lobby" and now - self.last_lb > 5:
            self.last_lb = now
            self.net.send({"t": "lb"})

    # ------------------------------------------------------------ drawing
    def draw(self):
        s = self.screen
        s.fill(BG)
        getattr(self, "draw_" + self.scene)()
        if self.msg and time.time() < self.msg_until:
            pygame.draw.rect(s, RED, (0, H - 34, W, 34))
            self.text(self.msg, (W // 2, H - 17), 26, (255, 255, 255), center=True)
        net = f"{self.net.proto.upper()}"
        if self.net.rtt_ms is not None:
            net += f" {self.net.rtt_ms:.0f} ms"
        self.text(net, (W - 10, 8), 20, DIM, right=True)

    def draw_connecting(self):
        self.text("Connecting to the server...", (W // 2, H // 2), 34, center=True)

    def draw_closed(self):
        self.text("Disconnected", (W // 2, H // 2 - 30), 60, RED, center=True)
        self.text(self.why, (W // 2, H // 2 + 20), 26, DIM, center=True)
        self.text("Close the window and start the client again", (W // 2, H // 2 + 60), 26, DIM, center=True)

    def draw_login(self):
        self.text("Bomberman Arena", (W // 2, 90), 90, SPARK, center=True)
        for i, (k, label) in enumerate((("u", "Username"), ("p", "Password"))):
            y = 190 + i * 80
            self.text(label, (W // 2 - 200, y - 26), 26, DIM)
            r = pygame.Rect(W // 2 - 200, y, 400, 40)
            pygame.draw.rect(self.screen, PANEL, r)
            pygame.draw.rect(self.screen, SPARK if self.focus == k else DIM, r, 2)
            shown = self.fields[k] if k == "u" else "*" * len(self.fields[k])
            self.text(shown + ("_" if self.focus == k and int(time.time() * 2) % 2 else ""), (r.x + 10, r.y + 9), 26)
        self.text("TAB: switch field     ENTER: sign in     F2: create account", (W // 2, 400), 26, DIM, center=True)

    def draw_lobby(self):
        self.text(f"Lobby - {self.user}", (30, 25), 60, SPARK)
        self.text("Rooms", (30, 100), 34)
        if not self.rooms:
            self.text("No room yet. Press C to create one.", (30, 145), 26, DIM)
        for i, r in enumerate(self.rooms[:10]):
            y = 145 + i * 44
            pygame.draw.rect(self.screen, PANEL, (30, y, 520, 38))
            if i == self.sel:
                pygame.draw.rect(self.screen, SPARK, (30, y, 520, 38), 2)
            self.text(f"{r['name']}  (host {r['host']})", (42, y + 10), 26)
            self.text(f"{r['n']}/{r['max']}  {r['state']}", (540, y + 10), 26, DIM, right=True)
        self.text("Best players", (600, 100), 34)
        for i, r in enumerate(self.lb[:8]):
            self.text(f"{i + 1}. {r['username']}", (600, 145 + i * 30), 26)
            self.text(f"{r['wins']}w {r['total_score']}", (W - 20, 145 + i * 30), 26, DIM, right=True)
        self.text("UP/DOWN select    ENTER join    C create room    ESC sign out", (30, H - 70), 26, DIM)

    def draw_room(self):
        r = self.room
        if not r:
            return
        self.text(r["name"], (30, 25), 60, SPARK)
        for i, n in enumerate(r["players"]):
            y = 110 + i * 70
            self.screen.blit(self.skin[i][0][1], (40, y))
            self.text(n + ("  (host)" if n == r["host"] else ""), (110, y + 12), 34, PCOL[i])
        host = r["host"] == self.user
        hint = ("ENTER: start the match" if len(r["players"]) >= r["min"] else f"Need {r['min']} players") if host \
            else f"Waiting for {r['host']} to start"
        self.text(hint, (30, H - 100), 34, SPARK)
        self.text("ESC: leave the room", (30, H - 60), 26, DIM)

    def draw_game(self):
        s, m = self.screen, self.snap
        if not m:
            return self.text("Waiting for the first snapshot...", (W // 2, H // 2), 34, center=True)
        g = m["g"]
        for x in range(COLS):
            for y in range(COLS):
                c = g[x * COLS + y]
                s.blit(self.terrain[0], (x * TILE, y * TILE))
                if c in "12":
                    s.blit(self.terrain[int(c)], (x * TILE, y * TILE))
        for x, y, k in m["u"]:
            s.blit(self.pu[k], (x * TILE, y * TILE))
        for x, y, f in m["b"]:
            s.blit(self.bomb[f], (x * TILE, y * TILE))
        for x in range(COLS):
            for y in range(COLS):
                i = "abc".find(g[x * COLS + y])
                if i >= 0:
                    s.blit(self.expl[i], (x * TILE, y * TILE))
        for p in m["p"]:
            if p[5]:
                px, py = p[1] * TILE // 4, p[2] * TILE // 4
                s.blit(self.skin[p[0]][p[3]][p[4]], (px, py))
                nm = self.fonts[20].render(self.game["players"][p[0]]["u"], True, PCOL[p[0]])
                s.blit(nm, nm.get_rect(midbottom=(px + TILE // 2, py)))
        x0 = COLS * TILE + 14
        secs = int(m["tl"] / 1000 + 0.99)
        self.text(f"{secs // 60}:{secs % 60:02d}", (x0, 12), 60)
        for p in m["p"]:
            y = 80 + p[0] * 56
            name = self.game["players"][p[0]]["u"] + (" (you)" if p[0] == self.game["you"] else "")
            col = PCOL[p[0]] if p[5] else DIM
            self.text(name, (x0, y), 26, col)
            self.text(f"{p[6]} pts  {p[9]} kills" + ("" if p[5] else "  OUT"), (x0, y + 24), 20, col)
        self.text("Arrows move, SPACE bomb", (x0, H - 54), 20, DIM)
        self.text("ESC leave match", (x0, H - 30), 20, DIM)
        if m["ph"] == "countdown":
            self.text(str(int(m["cd"] / 1000) + 1), (COLS * TILE // 2, H // 2), 90, (255, 255, 255), center=True)

    def draw_results(self):
        r = self.result
        if r["winner"] is None:
            title = "Draw!"
        elif r["winner"] == self.user:
            title = "You win!"
        else:
            title = f"{r['winner']} wins"
        self.text(title, (W // 2, 70), 90, SPARK, center=True)
        self.text({"last_standing": "Last player standing", "time_up": "Time is up",
                   "all_dead": "Everyone was eliminated",
                   "draw": "Eliminated at the same time"}.get(r["reason"], r["reason"]), (W // 2, 130), 26, DIM, center=True)
        for i, row in enumerate(r["ranking"]):
            y = 190 + i * 52
            tag = " (disconnected)" if row["dropped"] else " (survived)" if row["alive"] else ""
            self.text(f"{row['rank']}. {row['u']}{tag}", (180, y), 34, PCOL[row["slot"]])
            self.text(f"{row['kills']} kills", (560, y + 4), 26, DIM, right=True)
            self.text(f"{row['score']}", (700, y), 34, right=True)
        self.text("ENTER: back to the room", (W // 2, H - 60), 26, DIM, center=True)


def main(proto=None, host="127.0.0.1", port=None):
    """Arguments are the defaults; command-line options override them.
    proto=None means --proto must be given (plain `python client/pygame_client.py`)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--proto", choices=["tcp", "udp"], default=proto, required=proto is None)
    ap.add_argument("--host", default=host)
    ap.add_argument("--port", type=int, default=port)
    ap.add_argument("--loss", type=float, default=0.0)
    a = ap.parse_args()
    pygame.init()
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption(f"Bomberman Arena [{a.proto.upper()}] - {a.host}")
    net = make_client(a.proto, a.host, a.port, a.loss)
    net.start()
    app = App(net, screen)
    clock = pygame.time.Clock()
    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                net.close()
                return
            app.on_event(e)
        app.update()
        app.draw()
        pygame.display.flip()
        clock.tick(60)


if __name__ == "__main__":
    main()
