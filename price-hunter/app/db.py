"""SQLite storage: products, price snapshots, alerts, scan runs.

Price history we collect ourselves is the core asset: WB inflates the
'before discount' price all the time, so a 90% sticker discount alone
means little. A drop against the product's own recorded history is what
separates a real pricing error / self-buyout dump from marketing noise.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    nm INTEGER PRIMARY KEY,
    name TEXT, brand TEXT, supplier TEXT, photo TEXT, url TEXT,
    rating REAL DEFAULT 0, feedbacks INTEGER DEFAULT 0,
    first_seen INTEGER, last_seen INTEGER,
    last_price REAL, last_sale_price REAL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nm INTEGER NOT NULL, ts INTEGER NOT NULL,
    price REAL NOT NULL, sale_price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_nm_ts ON snapshots(nm, ts);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nm INTEGER NOT NULL, ts INTEGER NOT NULL,
    name TEXT, brand TEXT, photo TEXT, url TEXT,
    price REAL, sale_price REAL, discount_pct REAL,
    drop_vs_median_pct REAL, hist_median REAL,
    saving REAL, score INTEGER, level TEXT,
    reasons TEXT, source TEXT,           -- 'live' | 'demo'
    telegram_sent INTEGER DEFAULT 0,
    dismissed INTEGER DEFAULT 0,
    raw TEXT                              -- JSON evidence snapshot
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, finished INTEGER,
    source TEXT, queries INTEGER, scanned INTEGER,
    new_products INTEGER, alerts INTEGER, error TEXT
);
"""


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ #
    def upsert_product(self, p: dict[str, Any]) -> bool:
        """Insert/update product + append snapshot. Returns True if new."""
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT nm FROM products WHERE nm=?", (p["nm"],)
            ).fetchone()
            is_new = row is None
            self._conn.execute(
                """INSERT INTO products (nm,name,brand,supplier,photo,url,rating,
                       feedbacks,first_seen,last_seen,last_price,last_sale_price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(nm) DO UPDATE SET name=excluded.name,
                       brand=excluded.brand, supplier=excluded.supplier,
                       rating=excluded.rating, feedbacks=excluded.feedbacks,
                       last_seen=excluded.last_seen,
                       last_price=excluded.last_price,
                       last_sale_price=excluded.last_sale_price""",
                (p["nm"], p["name"], p["brand"], p.get("supplier", ""), p["photo"],
                 p["url"], p.get("rating", 0), p.get("feedbacks", 0),
                 now, now, p["price_rub"], p["sale_price_rub"]),
            )
            # Avoid snapshot spam: only record when price moved or >10 min passed.
            last = self._conn.execute(
                "SELECT ts, sale_price FROM snapshots WHERE nm=? ORDER BY ts DESC LIMIT 1",
                (p["nm"],),
            ).fetchone()
            if not last or abs(last["sale_price"] - p["sale_price_rub"]) > 0.01 \
                    or now - last["ts"] > 600:
                self._conn.execute(
                    "INSERT INTO snapshots (nm,ts,price,sale_price) VALUES (?,?,?,?)",
                    (p["nm"], now, p["price_rub"], p["sale_price_rub"]),
                )
            self._conn.commit()
            return is_new

    def history_stats(self, nm: int, exclude_latest_minutes: int = 5) -> dict:
        """Stats over our own snapshots, ignoring the freshest few minutes
        so the current (possibly erroneous) price doesn't skew the median."""
        cutoff = int(time.time()) - exclude_latest_minutes * 60
        with self._lock:
            rows = self._conn.execute(
                "SELECT sale_price FROM snapshots WHERE nm=? AND ts<=? ORDER BY ts",
                (nm, cutoff),
            ).fetchall()
        by_time = [r[0] for r in rows]
        if not by_time:
            return {"count": 0, "median": None, "min": None, "prev": None}
        prices = sorted(by_time)
        mid = prices[len(prices) // 2] if len(prices) % 2 else \
            (prices[len(prices) // 2 - 1] + prices[len(prices) // 2]) / 2
        return {"count": len(prices), "median": mid, "min": prices[0],
                "prev": by_time[-1]}  # most recent price before the cutoff

    def product_history(self, nm: int, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, price, sale_price FROM snapshots WHERE nm=? "
                "ORDER BY ts DESC LIMIT ?", (nm, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ------------------------------------------------------------------ #
    def recent_alert(self, nm: int, sale_price: float, cooldown_h: float) -> bool:
        """True if we already alerted this product at ~this price recently."""
        since = int(time.time()) - int(cooldown_h * 3600)
        with self._lock:
            row = self._conn.execute(
                "SELECT sale_price FROM alerts WHERE nm=? AND ts>=? "
                "ORDER BY ts DESC LIMIT 1", (nm, since)).fetchone()
        if not row:
            return False
        # Re-alert only if price fell another >=10% since the last alert.
        return sale_price >= row[0] * 0.9

    def add_alert(self, a: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO alerts (nm,ts,name,brand,photo,url,price,sale_price,
                       discount_pct,drop_vs_median_pct,hist_median,saving,score,
                       level,reasons,source,raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (a["nm"], int(time.time()), a["name"], a["brand"], a["photo"],
                 a["url"], a["price_rub"], a["sale_price_rub"], a["discount_pct"],
                 a.get("drop_vs_median_pct"), a.get("hist_median"), a["saving"],
                 a["score"], a["level"], json.dumps(a["reasons"], ensure_ascii=False),
                 a["source"], json.dumps(a.get("raw", {}), ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def mark_telegram_sent(self, alert_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE alerts SET telegram_sent=1 WHERE id=?",
                               (alert_id,))
            self._conn.commit()

    def alerts(self, limit: int = 100, include_dismissed: bool = False) -> list[dict]:
        q = "SELECT * FROM alerts"
        if not include_dismissed:
            q += " WHERE dismissed=0"
        q += " ORDER BY ts DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(q, (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d.get("reasons") or "[]")
            d.pop("raw", None)
            out.append(d)
        return out

    def dismiss_alert(self, alert_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE alerts SET dismissed=1 WHERE id=?",
                               (alert_id,))
            self._conn.commit()

    # ------------------------------------------------------------------ #
    def start_run(self, source: str, queries: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (ts,source,queries,scanned,new_products,alerts) "
                "VALUES (?,?,?,0,0,0)", (int(time.time()), source, queries))
            self._conn.commit()
            return cur.lastrowid

    def finish_run(self, run_id: int, scanned: int, new_products: int,
                   alerts: int, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished=?,scanned=?,new_products=?,alerts=?,error=? "
                "WHERE id=?",
                (int(time.time()), scanned, new_products, alerts, error, run_id))
            self._conn.commit()

    def runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        day_ago = int(time.time()) - 86400
        with self._lock:
            def one(sql, *args):
                return self._conn.execute(sql, args).fetchone()[0]
            return {
                "products_seen": one("SELECT COUNT(*) FROM products"),
                "snapshots": one("SELECT COUNT(*) FROM snapshots"),
                "alerts_total": one("SELECT COUNT(*) FROM alerts WHERE dismissed=0"),
                "alerts_today": one(
                    "SELECT COUNT(*) FROM alerts WHERE dismissed=0 AND ts>=?", day_ago),
                "best_discount_today": one(
                    "SELECT COALESCE(MAX(discount_pct),0) FROM alerts WHERE ts>=?",
                    day_ago),
                "potential_saving_today": one(
                    "SELECT COALESCE(SUM(saving),0) FROM alerts WHERE ts>=?", day_ago),
                "scans_total": one("SELECT COUNT(*) FROM runs WHERE finished IS NOT NULL"),
            }

    def products(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM products ORDER BY last_seen DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
