"""SQLite-хранилище: минимум данных, никаких логов трафика и IP."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id       INTEGER PRIMARY KEY,
    username    TEXT,
    doss_name   TEXT UNIQUE NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',   -- new|trial|paid|friend|owner|expired
    trial_used  INTEGER NOT NULL DEFAULT 0,
    expires_at  TEXT,                          -- YYYY-MM-DD, NULL = бессрочно
    traffic_gb  INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS payments (
    id          TEXT PRIMARY KEY,              -- telegram_payment_charge_id
    tg_id       INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    days        INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- users -------------------------------------------------------
    def get(self, tg_id: int) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return cur.fetchone()

    def upsert(self, tg_id: int, **fields) -> None:
        now = fields.pop("now", None) or _utcnow()
        fields.setdefault("updated_at", now)
        row = self.get(tg_id)
        if row is None:
            fields.setdefault("created_at", now)
            fields.setdefault("doss_name", f"u{tg_id}")
            cols = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            self.conn.execute(
                f"INSERT INTO users (tg_id, {cols}) VALUES (?, {marks})",
                (tg_id, *fields.values()),
            )
        else:
            if not fields:
                return
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(
                f"UPDATE users SET {sets} WHERE tg_id = ?",
                (*fields.values(), tg_id),
            )
        self.conn.commit()

    def all_users(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM users ORDER BY created_at"))

    def expired(self, today: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM users WHERE expires_at IS NOT NULL AND expires_at < ? "
            "AND status NOT IN ('owner','friend','expired')",
            (today,),
        ))

    def stats(self) -> dict:
        q = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM users GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in q}
        paid = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s, COUNT(*) AS n FROM payments"
        ).fetchone()
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "payments_count": paid["n"],
            "payments_sum": paid["s"],
        }

    # --- payments ----------------------------------------------------
    def add_payment(self, charge_id: str, tg_id: int, amount: int,
                    currency: str, days: int) -> bool:
        """False, если платёж уже обработан (идемпотентность вебхуков)."""
        try:
            self.conn.execute(
                "INSERT INTO payments (id, tg_id, amount, currency, days, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (charge_id, tg_id, amount, currency, days, _utcnow()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    # --- служебные значения (например, кто владелец бота) --------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
