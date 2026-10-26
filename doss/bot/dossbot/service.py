"""Бизнес-логика подписок: триал, оплата, продление, истечение.

Здесь нет aiogram — модуль тестируется отдельно (tests/selftest.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .db import Database
from .providers import Provider, ProviderError


class NotAllowed(RuntimeError):
    pass


@dataclass
class Access:
    name: str
    link: str
    expires: str | None
    traffic_gb: int
    status: str


def resolve_owner(cfg, db) -> int:
    """OWNER_ID из env, иначе — сохранённый в БД (первый /start)."""
    if getattr(cfg, "owner_id", 0):
        return int(cfg.owner_id)
    try:
        return int(db.get_meta("owner_id") or 0)
    except (TypeError, ValueError):
        return 0


def claim_owner(cfg, db, user_id: int) -> bool:
    """Первый /start становится владельцем. True — если назначили только что."""
    if resolve_owner(cfg, db):
        return False
    db.set_meta("owner_id", str(user_id))
    return True


def today() -> date:
    return datetime.utcnow().date()


def _iso(d: date) -> str:
    return d.isoformat()


class SubscriptionService:
    def __init__(self, db: Database, provider: Provider, cfg) -> None:
        self.db = db
        self.provider = provider
        self.cfg = cfg

    # ---------------------------------------------------------------- helpers
    def _name(self, tg_id: int) -> str:
        """Имя на сервере. Ничего не пишет в БД — только читает/вычисляет."""
        row = self.db.get(tg_id)
        return row["doss_name"] if row is not None else f"u{tg_id}"

    def _extend_from(self, row) -> date:
        """Продление от текущей даты подписки, если она ещё не кончилась."""
        base = today()
        if row is not None and row["expires_at"]:
            try:
                current = date.fromisoformat(row["expires_at"])
                if current > base:
                    base = current
            except ValueError:
                pass
        return base

    # ------------------------------------------------------------------ триал
    async def start_trial(self, tg_id: int, username: str = "") -> Access:
        row = self.db.get(tg_id)
        if row is not None and row["trial_used"]:
            raise NotAllowed("триал уже использован")
        if row is not None and row["status"] in ("owner", "friend"):
            raise NotAllowed("у тебя и так безлимитный доступ")

        name = self._name(tg_id)
        expires = _iso(today() + timedelta(days=self.cfg.trial_days))
        try:
            link = await self.provider.add_user(
                name, expires=expires, traffic_gb=self.cfg.trial_gb, note=f"tg:{tg_id}"
            )
        except ProviderError as e:
            raise NotAllowed(f"сервер не выдал доступ: {e}") from e

        self.db.upsert(
            tg_id, username=username, trial_used=1, status="trial",
            expires_at=expires, traffic_gb=self.cfg.trial_gb,
        )
        return Access(name, link, expires, self.cfg.trial_gb, "trial")

    # ----------------------------------------------------------------- оплата
    async def apply_payment(self, tg_id: int, username: str, charge_id: str,
                            amount: int, currency: str) -> Access:
        """Продлевает подписку на plan_days. Идемпотентно по charge_id."""
        fresh = self.db.add_payment(charge_id, tg_id, amount, currency, self.cfg.plan_days)
        row = self.db.get(tg_id)
        if row is None:
            self.db.upsert(tg_id, username=username, doss_name=f"u{tg_id}")
            row = self.db.get(tg_id)
        if not fresh:
            # повторное событие: просто отдаём актуальный доступ
            return await self.get_access(tg_id)

        base = self._extend_from(row)
        expires = _iso(base + timedelta(days=self.cfg.plan_days))
        name = row["doss_name"]

        if row["status"] in ("trial", "paid", "expired"):
            try:
                await self.provider.update_user(
                    name, expires=expires, traffic_gb=self.cfg.plan_gb, enabled=True
                )
            except ProviderError:
                # аккаунта на сервере нет (например, отозван) — создаём заново
                try:
                    await self.provider.remove_user(name)
                except ProviderError:
                    pass
                await self.provider.add_user(
                    name, expires=expires, traffic_gb=self.cfg.plan_gb, note=f"tg:{tg_id}"
                )
        else:  # new
            await self.provider.add_user(
                name, expires=expires, traffic_gb=self.cfg.plan_gb, note=f"tg:{tg_id}"
            )

        self.db.upsert(
            tg_id, username=username, status="paid", expires_at=expires,
            traffic_gb=self.cfg.plan_gb,
        )
        return await self.get_access(tg_id)

    # --------------------------------------------------------------- доступ
    async def get_access(self, tg_id: int) -> Access:
        row = self.db.get(tg_id)
        if row is None or row["status"] == "new":
            raise NotAllowed("подписки пока нет")
        link = await self.provider.link(row["doss_name"])
        return Access(
            row["doss_name"], link, row["expires_at"],
            row["traffic_gb"] or 0, row["status"],
        )

    async def grant(self, tg_id: int, days: int | None, unlimited: bool) -> Access:
        """Ручная выдача (владелец/друзья)."""
        self.db.upsert(tg_id, doss_name=f"u{tg_id}")
        row = self.db.get(tg_id)
        expires = None if unlimited or not days else _iso(
            self._extend_from(row) + timedelta(days=days)
        )
        status = "friend" if unlimited else "paid"

        if row["status"] in ("new",):
            await self.provider.add_user(
                row["doss_name"], expires=expires, traffic_gb=self.cfg.plan_gb,
                unlimited=unlimited, note=f"tg:{tg_id}",
            )
        else:
            await self.provider.update_user(
                row["doss_name"], expires=expires, traffic_gb=self.cfg.plan_gb,
                unlimited=unlimited, enabled=True,
            )
        self.db.upsert(tg_id, status=status, expires_at=expires,
                       traffic_gb=None if unlimited else self.cfg.plan_gb)
        return await self.get_access(tg_id)

    # ------------------------------------------------------------- обслуживание
    async def expire_outdated(self) -> list[str]:
        """Отключает подписки с истёкшей датой. Вызывать раз в сутки."""
        disabled: list[str] = []
        for row in self.db.expired(_iso(today())):
            try:
                await self.provider.update_user(row["doss_name"], enabled=False)
            except ProviderError:
                pass
            self.db.upsert(row["tg_id"], status="expired")
            disabled.append(row["doss_name"])
        return disabled
