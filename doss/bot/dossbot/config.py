"""Конфигурация бота Doss (переменные окружения / .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Минимальный парсер .env — без внешних зависимостей."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    bot_token: str = ""
    owner_id: int = 0

    # тарифы
    trial_days: int = 3
    trial_gb: int = 10
    plan_days: int = 30
    plan_gb: int = 200
    stars_price: int = 150          # XTR (Telegram Stars), ≈ 119 ₽

    # инфраструктура
    db_path: str = "doss.sqlite3"
    provider: str = "demo"          # demo | local | ssh
    doss_bin: str = "doss"          # для provider=local
    ssh_host: str = ""
    ssh_user: str = "root"
    ssh_key: str = "/etc/doss/bot_id_ed25519"
    sub_base: str = ""              # напр. http://203.0.113.10 — если включены подписки

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        return cls(
            bot_token=os.environ.get("BOT_TOKEN", ""),
            owner_id=_int("OWNER_ID", 0),
            trial_days=_int("TRIAL_DAYS", 3),
            trial_gb=_int("TRIAL_GB", 10),
            plan_days=_int("PLAN_DAYS", 30),
            plan_gb=_int("PLAN_GB", 200),
            stars_price=_int("STARS_PRICE", 150),
            db_path=os.environ.get("DB_PATH", "doss.sqlite3"),
            provider=os.environ.get("PROVIDER", "demo"),
            doss_bin=os.environ.get("DOSS_BIN", "doss"),
            ssh_host=os.environ.get("SSH_HOST", ""),
            ssh_user=os.environ.get("SSH_USER", "root"),
            ssh_key=os.environ.get("SSH_KEY", "/etc/doss/bot_id_ed25519"),
            sub_base=os.environ.get("SUB_BASE", ""),
        )
