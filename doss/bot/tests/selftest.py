"""Офлайн-проверка бизнес-логики бота: без токена, без сети, без aiogram.

Запуск:  python3 doss/bot/tests/selftest.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dossbot.config import Config  # noqa: E402
from dossbot.db import Database  # noqa: E402
from dossbot.providers import (DemoProvider, LinkProvider,  # noqa: E402
                               ProviderError)
from dossbot.service import (NotAllowed, SubscriptionService,  # noqa: E402
                             claim_owner, resolve_owner)

PASS = FAIL = 0


def ok(name: str) -> None:
    global PASS
    PASS += 1
    print(f"  \033[32m✓\033[0m {name}")


def bad(name: str, err: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"  \033[31m✗\033[0m {name} {err}")


def check(name: str, cond: bool, err: str = "") -> None:
    ok(name) if cond else bad(name, err)


async def main() -> int:
    tmp = tempfile.mkdtemp()
    cfg = Config(db_path=f"{tmp}/t.sqlite3", provider="demo",
                 trial_days=3, trial_gb=10, plan_days=30, plan_gb=200)
    db = Database(cfg.db_path)
    provider = DemoProvider()
    svc = SubscriptionService(db, provider, cfg)

    print("== триал")
    acc = await svc.start_trial(111, "anna")
    check("выдана ссылка", acc.link.startswith("vless://"))
    check("срок = сегодня+3", acc.expires == (date.today() + timedelta(days=3)).isoformat())
    check("лимит 10 ГБ", acc.traffic_gb == 10)
    check("на сервере создан пользователь", "u111" in provider.users)
    check("trial_used выставлен", db.get(111)["trial_used"] == 1)
    check("повторный триал запрещён", await _raises(svc.start_trial, 111))

    print("== оплата")
    acc = await svc.apply_payment(111, "anna", "charge_1", 150, "XTR")
    check("статус paid", db.get(111)["status"] == "paid")
    check("срок продлён от триала (3+30)", acc.expires == (date.today() + timedelta(days=33)).isoformat(),
          f"got {acc.expires}")
    check("лимит 200 ГБ", acc.traffic_gb == 200)
    check("на сервере лимит обновлён", provider.users["u111"]["traffic_gb"] == 200)
    check("тот же charge_id не продлевает дважды",
          (await svc.apply_payment(111, "anna", "charge_1", 150, "XTR")).expires == acc.expires)
    acc2 = await svc.apply_payment(111, "anna", "charge_2", 150, "XTR")
    check("второй платёж продлевает ещё на 30",
          acc2.expires == (date.today() + timedelta(days=63)).isoformat(), f"got {acc2.expires}")

    print("== новый пользователь сразу с оплатой")
    acc = await svc.apply_payment(222, "boris", "charge_3", 150, "XTR")
    check("создан и оплачен", db.get(222)["status"] == "paid" and "u222" in provider.users)
    check("срок 30 дней", acc.expires == (date.today() + timedelta(days=30)).isoformat())

    print("== ручная выдача")
    acc = await svc.grant(333, None, True)
    check("безлимит без срока", acc.expires is None and db.get(333)["status"] == "friend")
    check("на сервере unlimited", provider.users["u333"]["unlimited"] is True)
    acc = await svc.grant(444, 365, False)
    check("год доступа", acc.expires == (date.today() + timedelta(days=365)).isoformat())

    print("== истечение")
    db.upsert(555, doss_name="u555", status="paid", trial_used=1,
              expires_at=(date.today() - timedelta(days=1)).isoformat(), traffic_gb=200)
    await provider.add_user("u555", expires="2020-01-01", traffic_gb=200)
    disabled = await svc.expire_outdated()
    check("истёкший отключён", "u555" in disabled)
    check("статус expired", db.get(555)["status"] == "expired")
    check("на сервере enabled=false", provider.users["u555"]["enabled"] is False)
    check("бессрочный (friend) не тронут", db.get(333)["status"] == "friend")

    print("== обработка ошибок сервера")
    class Broken(DemoProvider):
        async def add_user(self, *a, **kw):
            raise ProviderError("ssh: connection refused")

    svc_broken = SubscriptionService(db, Broken(), cfg)
    check("ошибка провайдера -> понятный NotAllowed",
          await _raises(svc_broken.start_trial, 999))

    print("== владелец без OWNER_ID (первый /start)")
    cfg0 = Config(db_path=cfg.db_path, provider="demo", owner_id=0)
    check("до первого /start владельца нет", resolve_owner(cfg0, db) == 0)
    check("первый /start назначает владельца", claim_owner(cfg0, db, 777) is True)
    check("владелец запомнен", resolve_owner(cfg0, db) == 777)
    check("второй /start владельца не перебивает", claim_owner(cfg0, db, 888) is False)
    check("после перезапуска владелец тот же", resolve_owner(cfg0, db) == 777)
    cfg_forced = Config(db_path=cfg.db_path, provider="demo", owner_id=12345)
    check("OWNER_ID в env важнее", resolve_owner(cfg_forced, db) == 12345)
    check("claim с готовым владельцем ничего не меняет", claim_owner(cfg_forced, db, 999) is False)

    print("== режим одной ссылки (бесплатный PaaS)")
    os.environ["DOSS_LINK"] = "vless://demo-uuid@doss.onrender.com:443?type=ws&security=tls#Doss"
    lp = LinkProvider(os.environ["DOSS_LINK"])
    cfg_link = Config(db_path=cfg.db_path, provider="link", owner_id=0)
    svc_link = SubscriptionService(db, lp, cfg_link)
    acc = await svc_link.start_trial(1001, "link_user")
    check("ссылка из DOSS_LINK", acc.link.startswith("vless://demo-uuid@doss.onrender.com"))
    acc2 = await svc_link.apply_payment(1001, "link_user", "ch_link", 150, "XTR")
    check("оплата не ломается в режиме link", acc2.link == acc.link)
    await svc_link.grant(1002, None, True)
    check("grant тоже работает", (await svc_link.get_access(1002)).link == acc.link)
    try:
        LinkProvider("https://not-a-vless-link")
        check("мусорная ссылка отклоняется", False)
    except ProviderError:
        check("мусорная ссылка отклоняется", True)

    print("== статистика")
    s = db.stats()
    check("считает пользователей", s["total"] == 7, f"total={s['total']}")
    check("неудачная выдача не создаёт мусорных строк", db.get(999) is None)
    check("считает платежи", s["payments_count"] == 4, f"n={s['payments_count']}")
    check("сумма платежей", s["payments_sum"] == 600, f"sum={s['payments_sum']}")

    db.close()
    print(f"\nитог: {PASS} ок, {FAIL} провал(ов)")
    return 0 if FAIL == 0 else 1


async def _raises(fn, *args) -> bool:
    try:
        await fn(*args)
        return False
    except NotAllowed:
        return True


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
