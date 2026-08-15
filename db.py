"""SQLite persistence for the bot.

Tables:
  users         telegram users, home/work coordinates
  saved_places  extra named locations (e.g. "dacha")
  watches       per-user store watches (sniper notifications)
  stores        cached Vienna store/item map (result of the grid scan)
  travel_times  Google Distance Matrix cache (TTL, saves API quota)
  meta          key/value (last scan ts, pending chain requests)

WAL mode + one connection guarded by a lock; every method is short and
safe to call straight from async handlers.
"""
import os
import sqlite3
import threading
import time

DB_PATH = os.environ.get("TGTG_DB_PATH") or os.path.join(
    os.path.dirname(__file__), "tgtg_bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    home_lat    REAL, home_lng REAL,
    work_lat    REAL, work_lng REAL
);
CREATE TABLE IF NOT EXISTS saved_places (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    name        TEXT NOT NULL,
    lat         REAL NOT NULL, lng REAL NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS watches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id   INTEGER NOT NULL,
    item_id       TEXT NOT NULL,
    store_id      TEXT,
    store_name    TEXT,
    address       TEXT,
    lat           REAL, lng REAL,
    last_in_stock INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (telegram_id, item_id)
);
CREATE TABLE IF NOT EXISTS stores (
    item_id        TEXT PRIMARY KEY,
    store_id       TEXT,
    store_name     TEXT,
    item_name      TEXT,
    address        TEXT,
    lat            REAL, lng REAL,
    price          REAL, currency TEXT,
    rating         REAL, rating_count INTEGER,
    in_sales_window INTEGER,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS travel_times (
    origin  TEXT NOT NULL,
    dest    TEXT NOT NULL,
    mode    TEXT NOT NULL DEFAULT 'driving',
    minutes REAL,
    ts      INTEGER NOT NULL,
    PRIMARY KEY (origin, dest, mode)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, path=DB_PATH):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _rows(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        with self._lock:
            r = self._conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def _exec(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    # ---- users --------------------------------------------------------
    def get_user(self, telegram_id):
        return self._one("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))

    def upsert_user(self, telegram_id, username=None, first_name=None):
        u = self.get_user(telegram_id)
        if u is None:
            self._exec("INSERT INTO users (telegram_id, username, first_name) VALUES (?,?,?)",
                       (telegram_id, username, first_name))
        else:
            self._exec("UPDATE users SET username=COALESCE(?,username), "
                       "first_name=COALESCE(?,first_name) WHERE telegram_id=?",
                       (username, first_name, telegram_id))

    def set_home(self, telegram_id, lat, lng):
        self._exec("UPDATE users SET home_lat=?, home_lng=? WHERE telegram_id=?",
                   (lat, lng, telegram_id))

    def set_work(self, telegram_id, lat, lng):
        self._exec("UPDATE users SET work_lat=?, work_lng=? WHERE telegram_id=?",
                   (lat, lng, telegram_id))

    # ---- saved places -------------------------------------------------
    def add_place(self, telegram_id, name, lat, lng):
        self._exec("INSERT INTO saved_places (telegram_id, name, lat, lng) VALUES (?,?,?,?)",
                   (telegram_id, name, lat, lng))

    def places(self, telegram_id):
        return self._rows("SELECT * FROM saved_places WHERE telegram_id=? ORDER BY id",
                          (telegram_id,))

    def del_place(self, place_id, telegram_id):
        self._exec("DELETE FROM saved_places WHERE id=? AND telegram_id=?",
                   (place_id, telegram_id))

    # ---- watches ------------------------------------------------------
    def add_watch(self, telegram_id, item_id, store_id, store_name, address, lat, lng):
        self._exec(
            "INSERT OR IGNORE INTO watches (telegram_id, item_id, store_id, store_name,"
            " address, lat, lng) VALUES (?,?,?,?,?,?,?)",
            (telegram_id, item_id, store_id, store_name, address, lat, lng))

    def has_watch(self, telegram_id, item_id):
        return self._one("SELECT id FROM watches WHERE telegram_id=? AND item_id=?",
                         (telegram_id, item_id)) is not None

    def watches(self, telegram_id):
        return self._rows("SELECT * FROM watches WHERE telegram_id=? ORDER BY id",
                          (telegram_id,))

    def all_watches(self):
        return self._rows("SELECT * FROM watches")

    def unwatch(self, watch_id, telegram_id):
        self._exec("DELETE FROM watches WHERE id=? AND telegram_id=?",
                   (watch_id, telegram_id))

    def unwatch_item(self, item_id, telegram_id):
        self._exec("DELETE FROM watches WHERE item_id=? AND telegram_id=?",
                   (item_id, telegram_id))

    def set_watch_state(self, watch_id, in_stock):
        self._exec("UPDATE watches SET last_in_stock=? WHERE id=?",
                   (1 if in_stock else 0, watch_id))

    def unwatch_all(self, telegram_id):
        self._exec("DELETE FROM watches WHERE telegram_id=?", (telegram_id,))

    # ---- stores cache -------------------------------------------------
    def upsert_stores(self, entries):
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO stores (item_id, store_id, store_name, item_name,"
                " address, lat, lng, price, currency, rating, rating_count,"
                " in_sales_window, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,"
                " datetime('now'))",
                [(e["item_id"], e.get("store_id"), e["store_name"], e["item_name"],
                  e["address"], e.get("lat"), e.get("lng"), e.get("price"),
                  e.get("currency", "EUR"), e.get("rating"), e.get("rating_count"),
                  1 if e.get("in_sales_window") else 0) for e in entries])
            self._conn.commit()

    def stores_count(self):
        return self._one("SELECT COUNT(*) AS n FROM stores")["n"]

    def chain_stores(self, name):
        """All stores whose name contains the query (case-insensitive)."""
        like = f"%{name}%"
        return self._rows(
            "SELECT * FROM stores WHERE store_name LIKE ? COLLATE NOCASE ORDER BY"
            " rating IS NULL, rating DESC", (like,))

    def store_names(self):
        """Distinct store names, most common first (for the chain menu)."""
        return self._rows(
            "SELECT store_name, COUNT(*) AS n FROM stores GROUP BY store_name"
            " ORDER BY n DESC, store_name")

    # ---- travel time cache --------------------------------------------
    def get_travel(self, origin_key, dest_key, mode="driving", max_age_s=6 * 3600):
        r = self._one("SELECT minutes, ts FROM travel_times WHERE origin=? AND dest=? AND mode=?",
                      (origin_key, dest_key, mode))
        if r and time.time() - r["ts"] < max_age_s:
            return r["minutes"]
        return None

    def set_travel(self, origin_key, dest_key, minutes, mode="driving"):
        self._exec("INSERT OR REPLACE INTO travel_times (origin, dest, mode, minutes, ts)"
                   " VALUES (?,?,?,?,?)",
                   (origin_key, dest_key, mode, minutes, time.time()))

    # ---- meta ---------------------------------------------------------
    def get_meta(self, key, default=None):
        r = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key, value):
        self._exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))

    # pending chain requests: (telegram_id, query) that wait for a scan
    def add_pending_chain(self, telegram_id, query):
        self._exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                   (f"pending_chain:{telegram_id}", query))

    def pop_pending_chain(self):
        rows = self._rows("SELECT key, value FROM meta WHERE key LIKE 'pending_chain:%'")
        ids = [r["key"] for r in rows]
        with self._lock:
            self._conn.executemany("DELETE FROM meta WHERE key=?", [(k,) for k in ids])
            self._conn.commit()
        return [(int(k.split(":", 1)[1]), v) for k, v in
                ((r["key"], r["value"]) for r in rows)]
