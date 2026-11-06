"""
Local storage on the server PC (SQLite file, nothing leaves the machine).

  users          : accounts (salted PBKDF2 password hash) + career statistics
  matches        : one row per finished match
  match_players  : ranking of each match + network metrics (used for the TCP vs UDP comparison)
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time

USER_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
PBKDF2_ROUNDS = 120_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username    TEXT PRIMARY KEY COLLATE NOCASE,
    salt        BLOB NOT NULL,
    pwhash      BLOB NOT NULL,
    created_at  REAL NOT NULL,
    last_login  REAL,
    games       INTEGER NOT NULL DEFAULT 0,
    wins        INTEGER NOT NULL DEFAULT 0,
    total_score INTEGER NOT NULL DEFAULT 0,
    best_score  INTEGER NOT NULL DEFAULT 0,
    kills       INTEGER NOT NULL DEFAULT 0,
    deaths      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS matches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    proto      TEXT NOT NULL,
    room       TEXT,
    started_at REAL NOT NULL,
    duration   REAL,
    winner     TEXT,
    reason     TEXT
);
CREATE TABLE IF NOT EXISTS match_players (
    match_id    INTEGER NOT NULL REFERENCES matches(id),
    username    TEXT NOT NULL,
    rank        INTEGER,
    score       INTEGER,
    kills       INTEGER,
    survived    INTEGER,
    dropped     INTEGER,
    rtt_avg     REAL,
    rtt_max     REAL,
    snaps_sent  INTEGER,
    snaps_lost  INTEGER,
    retransmits INTEGER,
    bytes_out   INTEGER,
    bytes_in    INTEGER
);
"""


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)


class Database:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------ accounts
    def register(self, username, password):
        """-> (canonical_username or None, error or None)"""
        if not isinstance(username, str) or not USER_RE.match(username):
            return None, "Username: 3-16 letters, digits or _"
        if not isinstance(password, str) or not (4 <= len(password) <= 64):
            return None, "Password: 4 to 64 characters"
        salt = secrets.token_bytes(16)
        try:
            self.conn.execute("INSERT INTO users(username, salt, pwhash, created_at) VALUES (?,?,?,?)",
                              (username, salt, _hash(password, salt), time.time()))
            self.conn.commit()
        except sqlite3.IntegrityError:
            return None, "Username already taken"
        return username, None

    def verify(self, username, password):
        """-> (canonical_username or None, error or None)"""
        if not isinstance(username, str) or not isinstance(password, str):
            return None, "Invalid login"
        row = self.conn.execute("SELECT username, salt, pwhash FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            _hash(password, b"0" * 16)                      # same cost whether the user exists or not
            return None, "Wrong username or password"
        if not hmac.compare_digest(_hash(password, row["salt"]), row["pwhash"]):
            return None, "Wrong username or password"
        self.conn.execute("UPDATE users SET last_login=? WHERE username=?", (time.time(), row["username"]))
        self.conn.commit()
        return row["username"], None

    def user_stats(self, username):
        r = self.conn.execute("SELECT games, wins, total_score, best_score, kills, deaths FROM users WHERE username=?",
                              (username,)).fetchone()
        return dict(r) if r else {}

    # ------------------------------------------------------------ matches
    def record_match(self, proto, room, started_at, result, metrics):
        """result = Game.result ; metrics = {username: dict or None}"""
        cur = self.conn.execute(
            "INSERT INTO matches(proto, room, started_at, duration, winner, reason) VALUES (?,?,?,?,?,?)",
            (proto, room, started_at, result["duration_s"], result["winner"], result["reason"]))
        mid = cur.lastrowid
        for r in result["ranking"]:
            m = metrics.get(r["u"]) or {}
            self.conn.execute(
                "INSERT INTO match_players VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, r["u"], r["rank"], r["score"], r["kills"], int(r["alive"]), int(r["dropped"]),
                 m.get("rtt_avg"), m.get("rtt_max"), m.get("snaps_sent"), m.get("snaps_lost"),
                 m.get("retransmits"), m.get("bytes_out"), m.get("bytes_in")))
            self.conn.execute(
                "UPDATE users SET games=games+1, wins=wins+?, total_score=total_score+?, "
                "best_score=MAX(best_score,?), kills=kills+?, deaths=deaths+? WHERE username=?",
                (1 if (r["rank"] == 1 and not r["dropped"]) else 0, r["score"], r["score"], r["kills"],
                 0 if r["alive"] else 1, r["u"]))
        self.conn.commit()
        return mid

    # ------------------------------------------------------------ read side (hub + dashboard)
    def leaderboard(self, limit=10):
        rows = self.conn.execute(
            "SELECT username, games, wins, total_score, best_score, kills FROM users "
            "WHERE games>0 ORDER BY total_score DESC, wins DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def recent_matches(self, limit=10):
        out = []
        for m in self.conn.execute("SELECT * FROM matches ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
            players = self.conn.execute(
                "SELECT username, rank, score, kills, survived, dropped FROM match_players "
                "WHERE match_id=? ORDER BY rank", (m["id"],)).fetchall()
            out.append({**dict(m), "players": [dict(p) for p in players]})
        return out

    def comparison(self):
        """Aggregated network metrics per protocol (TCP vs UDP slide)."""
        rows = self.conn.execute(
            "SELECT m.proto AS proto, COUNT(DISTINCT m.id) AS matches, COUNT(*) AS players, "
            "AVG(p.rtt_avg) AS rtt_avg, MAX(p.rtt_max) AS rtt_max, "
            "SUM(p.snaps_sent) AS snaps_sent, SUM(p.snaps_lost) AS snaps_lost, "
            "SUM(p.retransmits) AS retransmits, AVG(p.bytes_out) AS bytes_out, AVG(p.bytes_in) AS bytes_in "
            "FROM match_players p JOIN matches m ON m.id=p.match_id "
            "WHERE p.dropped=0 AND p.snaps_sent IS NOT NULL GROUP BY m.proto").fetchall()
        return [dict(r) for r in rows]

    def user_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
