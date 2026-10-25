"""Telegram alerts via Bot API (no libraries, plain HTTPS).

Setup (2 minutes):
  1. In Telegram open @BotFather -> /newbot -> copy the token.
  2. Write any message to your new bot.
  3. Get your chat id: open @userinfobot, or call
     https://api.telegram.org/bot<TOKEN>/getUpdates and read message.chat.id
  4. Paste token + chat id in the web panel (Настройки) -> Тест.
"""
from __future__ import annotations

import html
import logging

import httpx

log = logging.getLogger("telegram")

_LEVEL_ICON = {"FIRE": "🔥🔥 ЦЕНОВАЯ ОШИБКА", "HOT": "🔥 РЕЗКОЕ ПАДЕНИЕ",
               "WATCH": "👀 Подозрительно дёшево"}

_MARKETPLACE = {
    "wb": ("🟣 Wildberries", "Артикул WB"),
    "ozon": ("🔵 Ozon", "SKU Ozon"),
}


def _marketplace_of(a: dict) -> tuple[str, str]:
    marketplace = str(a.get("marketplace") or "").lower()
    if marketplace not in _MARKETPLACE:
        marketplace = "ozon" if "ozon." in str(a.get("url") or "") else "wb"
    return _MARKETPLACE[marketplace]


def fmt_rub(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " ₽"


def alert_text(a: dict) -> str:
    marketplace_label, id_label = _marketplace_of(a)
    title = f"{html.escape(a['brand'])} — {html.escape(a['name'])}" \
        if a.get("brand") else html.escape(a["name"])
    lines = [
        f"<b>{_LEVEL_ICON.get(a['level'], a['level'])}</b>  ·  скор {a['score']}/100",
        f"🏪 <b>{marketplace_label}</b>",
        f"<b>{title}</b>",
        "",
    ]
    if a.get("drop_vs_median_pct") and a.get("hist_median"):
        lines += [
            f"💰 <b>{fmt_rub(a['sale_price_rub'])}</b> — на "
            f"<b>{a['drop_vs_median_pct']:.0f}% ниже обычной цены по истории</b>",
            f"📉 Медиана истории: <b>{fmt_rub(a['hist_median'])}</b>",
            f"💸 Выгода vs истории: <b>{fmt_rub(a['saving'])}</b>",
        ]
        if a.get("hist_min"):
            vs_min = a.get("vs_hist_min_pct")
            if vs_min is not None and vs_min > 0:
                lines.append(
                    f"🏆 <b>Новый минимум на площадке</b>: прежний минимум "
                    f"{fmt_rub(a['hist_min'])}, сейчас ниже на {vs_min:.0f}%")
            elif vs_min is not None and vs_min >= -3:
                lines.append(
                    f"📌 Цена у исторического минимума площадки: "
                    f"<b>{fmt_rub(a['hist_min'])}</b>")
            else:
                above = abs(vs_min) if vs_min is not None else 0
                lines.append(
                    f"📌 Минимум на площадке по истории: "
                    f"<b>{fmt_rub(a['hist_min'])}</b>"
                    + (f" (текущая выше на {above:.0f}%)" if above else ""))
        if a.get("price_rub"):
            lines.append(
                f"Для справки, цена в карточке: {fmt_rub(a['price_rub'])} "
                f"(скидка продавца −{a.get('discount_pct', 0):.0f}%; "
                f"она не была основанием алерта)")
    else:
        lines += [
            f"💰 <b>{fmt_rub(a['sale_price_rub'])}</b> вместо "
            f"<s>{fmt_rub(a['price_rub'])}</s>  (<b>−{a['discount_pct']:.0f}%</b>)",
            f"💸 Выгода: <b>{fmt_rub(a['saving'])}</b>",
        ]
    if a.get("reasons"):
        lines.append("")
        lines += [f"• {html.escape(r)}" for r in a["reasons"]]
    lines += [
        "",
        f"{id_label}: <code>{a.get('sku') or a['nm']}</code>",
        f"🛒 <a href=\"{a['url']}\">ОТКРЫТЬ И КУПИТЬ</a>",
        "⏱ Такие цены живут минуты. Действуй быстро, но проверь "
        "продавца и размер/комплектацию.",
    ]
    if a.get("source") == "demo":
        lines.append("\n<i>⚠ DEMO-режим: данные сгенерированы, товар вымышлен.</i>")
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _post(self, method: str, payload: dict) -> bool:
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{self.token}/{method}",
                json=payload, timeout=20)
            if r.status_code == 200 and r.json().get("ok"):
                return True
            log.warning("Telegram %s failed: %s %s", method, r.status_code, r.text[:300])
        except Exception as e:
            log.warning("Telegram %s error: %s", method, e)
        return False

    def send_alert(self, a: dict) -> bool:
        if not self.configured:
            return False
        text = alert_text(a)
        # Try with photo first; fall back to plain text (photo URL may 404).
        if a.get("photo") and self._post("sendPhoto", {
            "chat_id": self.chat_id, "photo": a["photo"], "caption": text[:1024],
            "parse_mode": "HTML",
        }):
            return True
        return self._post("sendMessage", {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })

    def send_test(self) -> bool:
        return self._post("sendMessage", {
            "chat_id": self.chat_id, "parse_mode": "HTML",
            "text": "✅ <b>Price Hunter на связи.</b>\n"
                    "Сюда будут падать ценовые ошибки Wildberries и Ozon. "
                    "Держи телефон под рукой — такие цены живут минуты.",
        })
