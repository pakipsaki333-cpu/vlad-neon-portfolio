"""Telegram-бот Doss: триал, подписка за Stars, выдача конфига."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, LabeledPrice, Message,
                           PreCheckoutQuery)

from .config import Config
from .db import Database
from .providers import ProviderError, build_provider
from .service import NotAllowed, SubscriptionService

log = logging.getLogger("doss.bot")
router = Router()

APPS_HELP = (
    "<b>Как подключиться</b>\n\n"
    "1. Поставь клиент:\n"
    "   • <a href='https://github.com/hiddify/hiddify-app/releases'>Hiddify</a> "
    "(рекомендую, умеет всё)\n"
    "   • <a href='https://github.com/2dust/v2rayNG/releases'>v2rayNG</a>\n"
    "2. Нажми «Моя подписка» и скопируй ссылку.\n"
    "3. В клиенте: «+» → «Импорт из буфера обмена» → подключиться.\n\n"
    "Если не подключается — напиши /support, поменяем протокол."
)


def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Попробовать 3 дня бесплатно", callback_data="trial")],
        [InlineKeyboardButton(text="⭐ Подписка на 30 дней", callback_data="buy")],
        [InlineKeyboardButton(text="🔑 Моя подписка", callback_data="me")],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="help")],
    ])


def _days_left(expires: str | None) -> str:
    if not expires:
        return "бессрочно"
    try:
        d = date.fromisoformat(expires)
    except ValueError:
        return expires
    left = (d - datetime.utcnow().date()).days
    if left < 0:
        return f"истекла {expires}"
    return f"до {expires} (осталось {left} дн.)"


def access_text(acc) -> str:
    return (
        "🔑 <b>Твой доступ Doss</b>\n\n"
        f"Срок: {_days_left(acc.expires)}\n"
        f"Лимит: {'∞' if acc.traffic_gb == 0 else str(acc.traffic_gb) + ' ГБ'}\n\n"
        "<b>Ссылка для клиента</b> (нажми, чтобы скопировать):\n"
        f"<code>{html.escape(acc.link)}</code>\n\n"
        "Дальше: Hiddify → «+» → «Импорт из буфера»."
    )


# ------------------------------------------------------------------ команды
@router.message(CommandStart())
async def cmd_start(message: Message, cfg: Config, svc: SubscriptionService) -> None:
    name = html.escape(message.from_user.first_name or "друг")
    text = (
        f"Привет, {name}! Это <b>Doss</b> — личный VPN без рекламы и логов.\n\n"
        f"🎁 Первые {cfg.trial_days} дней бесплатно ({cfg.trial_gb} ГБ) — без карты.\n"
        f"⭐ Дальше {cfg.plan_days} дней за {cfg.stars_price} Stars "
        f"({cfg.plan_gb} ГБ, оплата прямо в Telegram).\n\n"
        "Работает на Android/iOS/Windows через бесплатные клиенты — "
        "ссылка подойдёт к любому.\n\n"
        "Выбирай:"
    )
    await message.answer(text, reply_markup=main_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(APPS_HELP)


@router.message(Command("me"))
async def cmd_me(message: Message, svc: SubscriptionService) -> None:
    try:
        acc = await svc.get_access(message.from_user.id)
    except NotAllowed:
        await message.answer("Подписки пока нет. Возьми триал 🎁", reply_markup=main_kb())
        return
    await message.answer(access_text(acc))


@router.message(Command("trial"))
async def cmd_trial(message: Message, cfg: Config, svc: SubscriptionService) -> None:
    await _start_trial(message, cfg, svc)


@router.message(Command("buy"))
async def cmd_buy(message: Message, cfg: Config) -> None:
    await _send_invoice(message, cfg)


@router.message(Command("support"))
async def cmd_support(message: Message, cfg: Config) -> None:
    owner = f"@{cfg.owner_id}" if not cfg.owner_id else f"tg://user?id={cfg.owner_id}"
    await message.answer(f"Напиши владельцу: {owner}\nОпиши сеть (оператор) и клиент — так быстрее.")


# --------------------------------------------------------------- callbacks
@router.callback_query(F.data.in_({"trial", "buy", "me", "help"}))
async def on_menu(cb: CallbackQuery, cfg: Config, svc: SubscriptionService) -> None:
    await cb.answer()
    data = cb.data
    if data == "trial":
        await _start_trial(cb.message, cfg, svc, user=cb.from_user)
    elif data == "buy":
        await _send_invoice(cb.message, cfg)
    elif data == "me":
        try:
            acc = await svc.get_access(cb.from_user.id)
        except NotAllowed:
            await cb.message.answer("Подписки пока нет. Возьми триал 🎁", reply_markup=main_kb())
            return
        await cb.message.answer(access_text(acc))
    else:
        await cb.message.answer(APPS_HELP)


async def _start_trial(message: Message, cfg: Config, svc: SubscriptionService,
                       user=None) -> None:
    u = user or message.from_user
    try:
        acc = await svc.start_trial(u.id, username=u.username or "")
    except NotAllowed as e:
        await message.answer(f"Триал недоступен: {html.escape(str(e))}.\n\n"
                             "Можно оформить подписку:", reply_markup=main_kb())
        return
    await message.answer("Готово! Держи доступ 🎉\n\n" + access_text(acc))


async def _send_invoice(message: Message, cfg: Config) -> None:
    await message.answer_invoice(
        title=f"Doss VPN — {cfg.plan_days} дней",
        description=(
            f"Безлимитный доступ к Doss на {cfg.plan_days} дней "
            f"(лимит {cfg.plan_gb} ГБ). Без логов, без рекламы."
        ),
        payload=f"sub:{cfg.plan_days}",
        provider_token="",              # для Stars всегда пустой
        currency="XTR",
        prices=[LabeledPrice(label=f"{cfg.plan_days} дней", amount=cfg.stars_price)],
        start_parameter="doss-subscription",
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def on_paid(message: Message, cfg: Config, svc: SubscriptionService) -> None:
    sp = message.successful_payment
    if sp is None:
        return
    try:
        acc = await svc.apply_payment(
            message.from_user.id,
            username=message.from_user.username or "",
            charge_id=sp.telegram_payment_charge_id,
            amount=sp.total_amount,
            currency=sp.currency,
        )
    except (NotAllowed, ProviderError) as e:
        log.exception("платёж не применён")
        await message.answer(
            "Оплата получена, но доступ не выдался автоматически. "
            "Напиши /support — выдам вручную в течение часа."
        )
        if cfg.owner_id:
            await message.bot.send_message(
                cfg.owner_id,
                f"⚠️ Платёж без выдачи: tg={message.from_user.id} "
                f"{sp.total_amount} {sp.currency}\n{e}",
            )
        return

    await message.answer("Оплата получена, спасибо! 💚\n\n" + access_text(acc))


# ------------------------------------------------------------------ админ
def _is_owner(cfg: Config, user_id: int) -> bool:
    return cfg.owner_id != 0 and user_id == cfg.owner_id


@router.message(Command("stats"))
async def cmd_stats(message: Message, cfg: Config, db: Database) -> None:
    if not _is_owner(cfg, message.from_user.id):
        return
    s = db.stats()
    by = ", ".join(f"{k}: {v}" for k, v in sorted(s["by_status"].items())) or "—"
    await message.answer(
        f"<b>Doss stats</b>\nВсего: {s['total']}\nСтатусы: {by}\n"
        f"Платежей: {s['payments_count']} на {s['payments_sum']} ⭐"
    )


@router.message(Command("grant"))
async def cmd_grant(message: Message, cfg: Config, svc: SubscriptionService) -> None:
    """ /grant <tg_id> <дней|inf> """
    if not _is_owner(cfg, message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Формат: /grant <tg_id> <дней|inf>")
        return
    try:
        tg_id = int(parts[1])
    except ValueError:
        await message.answer("tg_id должен быть числом")
        return
    unlimited = parts[2].lower() in {"inf", "unlimited", "∞"}
    days = None if unlimited else int(parts[2])
    acc = await svc.grant(tg_id, days, unlimited)
    await message.answer(f"Выдано {tg_id}: {acc.name}, {_days_left(acc.expires)}")
    try:
        await message.bot.send_message(tg_id, "Тебе выдали доступ Doss 💚\n\n" + access_text(acc))
    except Exception:  # пользователь мог не начинать диалог с ботом
        pass


# ---------------------------------------------------------- обслуживание
async def maintenance_loop(svc: SubscriptionService, interval: int = 3600) -> None:
    while True:
        try:
            disabled = await svc.expire_outdated()
            if disabled:
                log.info("отключены истёкшие: %s", ", ".join(disabled))
        except Exception:  # noqa: BLE001 — цикл не должен падать
            log.exception("ошибка обслуживания")
        await asyncio.sleep(interval)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")

    db = Database(cfg.db_path)
    provider = build_provider(cfg)
    svc = SubscriptionService(db, provider, cfg)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp["cfg"] = cfg
    dp["db"] = db
    dp["svc"] = svc
    dp.include_router(router)

    task = asyncio.create_task(maintenance_loop(svc))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        db.close()
        await bot.session.close()
