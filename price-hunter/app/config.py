"""Settings: defaults + persisted JSON (data/settings.json) + .env overrides."""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# Desktop (.exe) build sets PRICE_HUNTER_DATA_DIR to %APPDATA%\PriceHunter
# BEFORE importing this module: a frozen one-file app unpacks code into a
# temp dir, so settings/DB must live in a stable per-user location.
DATA_DIR = Path(os.environ.get("PRICE_HUNTER_DATA_DIR") or (BASE_DIR / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "price_hunter.db"

DEFAULT_QUERIES = [
    "смартфон iphone", "наушники sony", "наушники airpods", "ноутбук игровой",
    "playstation 5", "видеокарта", "фен dyson", "пылесос робот",
    "lego конструктор", "парфюмерная вода", "кроссовки nike", "умные часы",
    "телевизор 4k", "электросамокат", "кофемашина", "игровая приставка",
]

DEFAULTS: dict = {
    # auto = try live WB, fall back to demo if unreachable (e.g. this sandbox)
    "mode": "auto",
    "queries": DEFAULT_QUERIES,
    "pages_per_query": 2,
    "sorts": ["popular", "benefit"],   # benefit = biggest discount first
    "interval_sec": 180,
    "min_discount_pct": 65,
    "min_drop_vs_history_pct": 35,
    "min_original_price_rub": 1500,
    "min_saving_rub": 1000,
    "min_score": 35,
    "alert_cooldown_hours": 6,
    "verify_with_price_history": True,
    "dest": -1257786,                  # WB geo: Moscow. Change to your region.
    "spp": 30,
    "telegram_enabled": True,
    "telegram_token": "",
    "telegram_chat_id": "",
}


def _load_env_file() -> None:
    env = BASE_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class Settings:
    def __init__(self):
        _load_env_file()
        self._data = dict(DEFAULTS)
        if SETTINGS_FILE.exists():
            try:
                self._data.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
            except Exception:
                pass
        # Env wins over file (handy on servers / Docker).
        if os.getenv("TELEGRAM_TOKEN"):
            self._data["telegram_token"] = os.environ["TELEGRAM_TOKEN"]
        if os.getenv("TELEGRAM_CHAT_ID"):
            self._data["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
        if os.getenv("HUNTER_MODE"):
            self._data["mode"] = os.environ["HUNTER_MODE"]

    def get(self) -> dict:
        return dict(self._data)

    def __getitem__(self, key):
        return self._data[key]

    def update(self, patch: dict) -> dict:
        allowed = set(DEFAULTS) | {"mode"}
        for k, v in patch.items():
            if k in allowed and v is not None:
                self._data[k] = v
        # Clamp dangerous values (protect user's IP from WB throttling).
        self._data["interval_sec"] = max(60, int(self._data["interval_sec"]))
        self._data["pages_per_query"] = max(1, min(10, int(self._data["pages_per_query"])))
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.get()

    def public(self) -> dict:
        """Settings safe for the UI (token masked)."""
        d = self.get()
        tok = d.get("telegram_token") or ""
        d["telegram_token_masked"] = (tok[:6] + "…" + tok[-3:]) if len(tok) > 12 else ("•••" if tok else "")
        d["telegram_configured"] = bool(tok and d.get("telegram_chat_id"))
        return d
