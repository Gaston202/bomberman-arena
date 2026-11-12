"""
Authoritative, headless Bomberman engine.

This is the single-player pygame game (player.py / bomb.py / explosion.py / game.py)
rewritten so that it runs ONLY on the server.  Clients never simulate anything: they
send their inputs and draw the snapshots they receive.  Same rules for TCP and UDP.

Coordinates
  * grid[x][y]        : 13 x 13 tiles.  0 grass, 1 wall, 2 box, 3 bomb (internal only)
  * player x / y      : in 1/4 tile steps (Q = 4) exactly like the original game
Directions            : 0 down, 1 right, 2 up, 3 left (same order as the original sprites)
"""
import math
import random

COLS = 13
Q = 4                       # steps per tile (original Player.TILE_SIZE)
TICK_RATE = 15              # original: clock.tick(15)
TICK_MS = 1000.0 / TICK_RATE
FUSE_MS = 3000              # original Bomb.time
BLAST_MS = 300              # original Explosion.time
COUNTDOWN_MS = 3000
MATCH_MS = 120_000
MAX_RANGE = 8
MAX_BOMBS = 5

SCORE_KILL = 100
SCORE_BOX = 10
SCORE_POWERUP = 20
SCORE_SUICIDE = -50
SCORE_SURVIVOR = 200        # last player standing

SPAWNS = [(1, 1), (11, 1), (1, 11), (11, 11)]
DIRS = {0: (0, 1), 1: (1, 0), 2: (0, -1), 3: (-1, 0)}

# Same map as GRID_BASE in the original game.py
GRID_BASE = [[1 if (x in (0, 12) or y in (0, 12) or (x % 2 == 0 and y % 2 == 0)) else 0
              for y in range(COLS)] for x in range(COLS)]


class Bomb:
    __slots__ = ("x", "y", "owner", "ms")

    def __init__(self, x, y, owner):
        self.x, self.y, self.owner, self.ms = x, y, owner, FUSE_MS


class Blast:
    __slots__ = ("sectors", "owner", "ms")

    def __init__(self, sectors, owner):
        self.sectors, self.owner, self.ms = sectors, owner, BLAST_MS


class Pickup:
    __slots__ = ("x", "y", "kind")          # kind 0 = extra bomb, 1 = longer fire

    def __init__(self, x, y, kind):
        self.x, self.y, self.kind = x, y, kind


class P:
    """Player state (server side)."""

    def __init__(self, slot, name):
        sx, sy = SPAWNS[slot]
        self.slot, self.name = slot, name
        self.x, self.y = sx * Q, sy * Q
        self.dir, self.frame = 0, 0
        self.alive = True
        self.dropped = False            # disconnected or left during the match
        self.rng, self.bombs_left = 3, 1
        self.score = self.kills = 0
        self.died_at = None
        # latest input
        self.want_dir, self.bomb_req, self.bomb_seen, self.last_n = -1, 0, 0, 0


class Game:
    def __init__(self, names, seed=None, match_ms=MATCH_MS, countdown_ms=COUNTDOWN_MS):
        self.rng = random.Random(seed)
        self.grid = [col[:] for col in GRID_BASE]
        self._generate_map()
        self.players = [P(i, n) for i, n in enumerate(names)]
        self.bombs, self.blasts, self.pickups = [], [], []
        self.phase = "countdown"
        self.cd = float(countdown_ms)
        self.left_ms = float(match_ms)
        self.elapsed = 0.0
        self.seq = 0
        self.finished = False
        self.result = None

    # ------------------------------------------------------------------ setup
    def _generate_map(self):
        """Same algorithm as generate_map() in the original game.py."""
        g = self.grid
        for i in range(1, COLS - 1):
            for j in range(1, COLS - 1):
                if g[i][j] != 0:
                    continue
                if (i < 3 or i > COLS - 4) and (j < 3 or j > COLS - 4):
                    continue
                if self.rng.randint(0, 9) < 7:
                    g[i][j] = 2

    # ------------------------------------------------------------------ input
    def set_input(self, slot, direction, bomb_counter, n):
        """Latest input of a player. `n` increases with every packet: stale/duplicated
        packets (possible with UDP) are ignored."""
        if not (0 <= slot < len(self.players)):
            return
        p = self.players[slot]
        if not isinstance(n, int) or n <= p.last_n:
            return
        p.last_n = n
        p.want_dir = direction if direction in (0, 1, 2, 3) else -1
        if isinstance(bomb_counter, int) and p.bomb_req < bomb_counter <= p.bomb_req + 20:
            p.bomb_req = bomb_counter

    def player_left(self, slot):
        p = self.players[slot]
        p.dropped = True
        if p.alive:
            p.alive = False
            p.died_at = self.elapsed

    # ------------------------------------------------------------------ tick
    def tick(self):
        if self.finished:
            return
        self.seq += 1
        if self.phase == "countdown":
            self.cd -= TICK_MS
            if self.cd <= 0:
                self.phase, self.cd = "play", 0.0
            return
        self.elapsed += TICK_MS
        self.left_ms -= TICK_MS
        for p in self.players:
            self._act(p)
        for b in self.bombs:
            b.ms -= TICK_MS
        due = [b for b in self.bombs if b.ms < 1]
        if due:
            self._detonate(due)
        self._check_deaths()
        for bl in self.blasts:
            bl.ms -= TICK_MS
        self.blasts = [bl for bl in self.blasts if bl.ms >= 1]
        self._check_end()

    def _act(self, p):
        if not p.alive:
            return
        d = p.want_dir
        if d in DIRS:
            dx, dy = DIRS[d]
            self._move(p, dx, dy)
            if d != p.dir:
                p.frame, p.dir = 0, d
            p.frame = 0 if p.frame == 2 else p.frame + 1
        if p.bomb_req != p.bomb_seen:
            p.bomb_seen = p.bomb_req
            self._plant(p)

    # ------------------------------------------------------------------ movement (port of Player.move)
    def _move(self, p, dx, dy):
        g = self.grid
        tx, ty = int(p.x / Q), int(p.y / Q)
        blocked = {(int(o.x / Q), int(o.y / Q)) for o in self.players if o is not p and o.alive}

        def free(cx, cy):
            return g[cx][cy] == 0 and (cx, cy) not in blocked

        if p.x % Q != 0 and dx == 0:
            if p.x % Q == 1:
                p.x -= 1
            elif p.x % Q == 3:
                p.x += 1
            return
        if p.y % Q != 0 and dy == 0:
            if p.y % Q == 1:
                p.y -= 1
            elif p.y % Q == 3:
                p.y += 1
            return
        if dx == 1:
            if free(tx + 1, ty):
                p.x += 1
        elif dx == -1:
            tx = math.ceil(p.x / Q)
            if free(tx - 1, ty):
                p.x -= 1
        if dy == 1:
            if free(tx, ty + 1):
                p.y += 1
        elif dy == -1:
            ty = math.ceil(p.y / Q)
            if free(tx, ty - 1):
                p.y -= 1
        cx, cy = math.ceil(p.x / Q), math.ceil(p.y / Q)
        for u in list(self.pickups):
            if u.x == cx and u.y == cy:
                self._take(p, u)

    def _take(self, p, u):
        if u.kind == 0:
            p.bombs_left = min(MAX_BOMBS, p.bombs_left + 1)
        else:
            p.rng = min(MAX_RANGE, p.rng + 1)
        p.score += SCORE_POWERUP
        self.pickups.remove(u)

    # ------------------------------------------------------------------ bombs
    def _plant(self, p):
        bx, by = round(p.x / Q), round(p.y / Q)
        if p.bombs_left <= 0 or self.grid[bx][by] != 0:
            return
        self.bombs.append(Bomb(bx, by, p.slot))
        self.grid[bx][by] = 3
        p.bombs_left -= 1

    def _sectors(self, x, y, reach):
        """Cells hit by a bomb (fixed version of Bomb.get_range: the first direction of the
        original compared the wrong cell when looking for another bomb)."""
        cells = {(x, y)}
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for i in range(1, reach):
                cx, cy = x + dx * i, y + dy * i
                c = self.grid[cx][cy]
                if c == 1:
                    break
                cells.add((cx, cy))
                if c == 2:
                    break
        return cells

    def _detonate(self, first):
        queue, done = list(first), []
        while queue:
            b = queue.pop()
            if b not in self.bombs:
                continue
            self.bombs.remove(b)
            self.grid[b.x][b.y] = 0
            owner = self.players[b.owner]
            owner.bombs_left = min(MAX_BOMBS, owner.bombs_left + 1)
            sectors = self._sectors(b.x, b.y, owner.rng)
            done.append((owner, sectors))
            for other in self.bombs:                       # chain reaction
                if (other.x, other.y) in sectors:
                    queue.append(other)
        hit = set().union(*(s for _, s in done))
        self.pickups = [u for u in self.pickups if (u.x, u.y) not in hit]
        for owner, sectors in done:
            for (x, y) in sectors:
                if self.grid[x][y] == 2:                   # destroyed box
                    self.grid[x][y] = 0
                    owner.score += SCORE_BOX
                    r = self.rng.randint(0, 9)
                    if r == 0:
                        self.pickups.append(Pickup(x, y, 0))
                    elif r == 1:
                        self.pickups.append(Pickup(x, y, 1))
            self.blasts.append(Blast(sectors, owner.slot))

    def _check_deaths(self):
        for p in self.players:
            if not p.alive:
                continue
            cell = (int(p.x / Q), int(p.y / Q))
            for bl in self.blasts:
                if cell in bl.sectors:
                    self._kill(p, bl.owner)
                    break

    def _kill(self, victim, killer_slot):
        victim.alive = False
        victim.died_at = self.elapsed
        if killer_slot == victim.slot:
            victim.score += SCORE_SUICIDE
        else:
            k = self.players[killer_slot]
            k.kills += 1
            k.score += SCORE_KILL

    # ------------------------------------------------------------------ end of match
    def _check_end(self):
        alive = [p for p in self.players if p.alive]
        if len(alive) > 1:
            if self.left_ms <= 0:
                self._finish("time_up")
            return
        if alive:
            self._finish("last_standing")
            return
        # Nobody is left.  If the last 2+ players (still connected) died on this same tick,
        # nobody outlived anybody: it is a draw, whatever the scores are.
        tied = [p for p in self.players if not p.dropped and p.died_at == self.elapsed]
        if len(tied) >= 2:
            self._finish("draw", tied)
        else:
            self._finish("all_dead")

    def _finish(self, reason, tied=()):
        if reason == "last_standing":
            for p in self.players:
                if p.alive:
                    p.score += SCORE_SURVIVOR
        # drawn players first (they share rank 1), then the usual order
        order = sorted(self.players, key=lambda p: (p.dropped, p not in tied, -p.score, not p.alive,
                                                    -(p.died_at if p.died_at is not None else 1e12), p.slot))
        self.result = {
            "reason": reason,
            "winner": None if tied else order[0].name,
            "duration_s": round(self.elapsed / 1000.0, 1),
            "ranking": [{"rank": 1 if p in tied else i + 1, "u": p.name, "slot": p.slot, "score": p.score,
                         "kills": p.kills, "alive": p.alive, "dropped": p.dropped} for i, p in enumerate(order)],
        }
        self.finished = True

    # ------------------------------------------------------------------ snapshot
    def snapshot(self):
        frames = {}
        for bl in self.blasts:
            f = 2 if bl.ms < 100 else 1 if bl.ms < 200 else 0
            for c in bl.sectors:
                frames[c] = f
        chars = []
        for x in range(COLS):
            for y in range(COLS):
                if (x, y) in frames:
                    chars.append("abc"[frames[(x, y)]])
                else:
                    v = self.grid[x][y]
                    chars.append(str(0 if v == 3 else v))
        return {
            "t": "snap", "s": self.seq, "ph": self.phase, "cd": int(self.cd), "tl": int(max(0, self.left_ms)),
            "g": "".join(chars),
            "p": [[p.slot, p.x, p.y, p.dir, p.frame, int(p.alive), p.score, p.bombs_left, p.rng, p.kills]
                  for p in self.players],
            "b": [[b.x, b.y, 2 if b.ms < 1000 else 1 if b.ms < 2000 else 0] for b in self.bombs],
            "u": [[u.x, u.y, u.kind] for u in self.pickups],
        }
