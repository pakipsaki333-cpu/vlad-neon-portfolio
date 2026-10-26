"""Интеграционный тест: бот ↔ реальный CLI `doss` (локальный провайдер).

Проверяет, что триал/оплата/истечение из бота действительно меняют
состояние сервера, а не только строки в SQLite.

Запуск:  python3 doss/bot/tests/test_provider_cli.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # .../doss
CLI = ROOT / "server" / "doss"

sys.path.insert(0, str(ROOT / "bot"))

from dossbot.config import Config  # noqa: E402
from dossbot.db import Database  # noqa: E402
from dossbot.providers import CommandProvider  # noqa: E402
from dossbot.service import SubscriptionService  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        print(f"  \033[31m✗\033[0m {name} {extra}")


def state(path: Path) -> dict:
    return json.loads(path.read_text())


async def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    etc = tmp / "etc"
    etc.mkdir()
    os.environ.update({
        "DOSS_ETC": str(etc),
        "SUB_DIR": str(tmp / "sub"),
        "XRAY_BIN": str(tmp / "no-xray"),
        "XRAY_CONFIG": str(tmp / "xray" / "config.json"),
        "DOSS_NO_SERVICE": "1",
    })

    # тот же state.json, что создаёт install.sh
    (etc / "state.json").write_text(json.dumps({
        "version": 1, "created": "2026-01-01T00:00:00Z",
        "server": {"host": "203.0.113.10", "port": 443, "dest_domain": "www.microsoft.com",
                   "private_key": "PRIV", "public_key": "PUB", "short_ids": [""],
                   "xhttp": {"enabled": False, "port": 8443, "path": "/doss-xh"},
                   "awg": {"enabled": False}},
        "users": {},
    }))
    subprocess.run([str(CLI), "build-base"], check=True, capture_output=True)

    cfg = Config(db_path=str(tmp / "bot.sqlite3"), provider="local", doss_bin=str(CLI),
                 trial_days=3, trial_gb=10, plan_days=30, plan_gb=200)
    db = Database(cfg.db_path)
    svc = SubscriptionService(db, CommandProvider([cfg.doss_bin]), cfg)

    print("== триал через CLI")
    acc = await svc.start_trial(111, "anna")
    st = state(etc / "state.json")
    check("пользователь появился на сервере", "u111" in st["users"])
    check("срок записан на сервере",
          st["users"]["u111"]["expires"] == (date.today() + timedelta(days=3)).isoformat())
    check("лимит 10 ГБ на сервере", st["users"]["u111"]["traffic_gb"] == 10)
    check("ссылка с маской и pbk", "sni=www.microsoft.com" in acc.link and "pbk=PUB" in acc.link)

    print("== оплата через CLI")
    acc = await svc.apply_payment(111, "anna", "charge_x", 150, "XTR")
    st = state(etc / "state.json")
    check("срок продлён на сервере",
          st["users"]["u111"]["expires"] == (date.today() + timedelta(days=33)).isoformat())
    check("лимит стал 200 ГБ", st["users"]["u111"]["traffic_gb"] == 200)
    check("срок в БД совпадает с сервером",
          db.get(111)["expires_at"] == st["users"]["u111"]["expires"])

    print("== истечение через CLI")
    db.upsert(222, doss_name="u222", status="paid", trial_used=1,
              expires_at=(date.today() - timedelta(days=1)).isoformat(), traffic_gb=200)
    await CommandProvider([cfg.doss_bin]).add_user(
        "u222", expires="2020-01-01", traffic_gb=200)
    disabled = await svc.expire_outdated()
    st = state(etc / "state.json")
    check("истёкший отключён на сервере", st["users"]["u222"]["enabled"] is False)
    check("и вернулся в списке", "u222" in disabled)

    print("== оборванная связь")
    broken = SubscriptionService(db, CommandProvider([str(tmp / "no-such-doss")]), cfg)
    try:
        await broken.start_trial(333)
        check("ошибка пробрасывается", False)
    except Exception as e:  # noqa: BLE001
        check("ошибка пробрасывается как NotAllowed", type(e).__name__ == "NotAllowed", type(e).__name__)

    db.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nитог: {PASS} ок, {FAIL} провал(ов)")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
