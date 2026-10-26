"""Headless desktop controller: wraps the hunting engine (Settings + DB +
Scanner) behind a small API the Tkinter GUI can call. No tkinter imports
here, so this runs and tests anywhere.
"""
from __future__ import annotations

import queue
from typing import Any, Mapping

from app.config import DB_FILE, Settings
from app.db import DB
from app.scanner import Scanner
from app.telegram import TelegramNotifier

_ALLOWED_SORTS = ("popular", "benefit", "priceup", "pricedown", "rate")
_ALLOWED_MODES = ("auto", "live", "demo")


def _as_int(raw: Mapping[str, Any], key: str, label: str,
            lo: int | None = None, hi: int | None = None) -> int:
    try:
        v = int(str(raw[key]).strip())
    except Exception:
        raise ValueError(f'«{label}»: нужно целое число') from None
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _as_float(raw: Mapping[str, Any], key: str, label: str,
              lo: float | None = None, hi: float | None = None) -> float:
    try:
        v = float(str(raw[key]).strip().replace(",", "."))
    except Exception:
        raise ValueError(f'«{label}»: нужно число') from None
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _as_bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "нет")
    return bool(value)


def _transport(raw: Mapping[str, Any], key: str, label: str) -> str:
    transport = str(raw.get(key, "auto")).strip().lower()
    transport = {"curl_cffi": "browser", "curl": "browser",
                 "dom": "chrome", "cdp": "chrome", "edge": "chrome",
                 "chromium": "chrome"}.get(transport, transport)
    if transport not in ("auto", "httpx", "browser", "chrome"):
        raise ValueError(f"{label}: выбери auto, chrome, browser или httpx")
    return transport


def parse_settings_form(raw: Mapping[str, Any], existing: dict) -> dict:
    """Validate/normalize values coming from the settings form.

    Empty telegram_token means "keep the saved one" (same semantics as
    the web panel) so the form never needs to display the real secret.
    Raises ValueError with a Russian, user-facing message.
    """
    out: dict[str, Any] = {}

    mode = str(raw.get("mode", "auto")).strip().lower()
    if mode not in _ALLOWED_MODES:
        raise ValueError("Режим: выбери auto, live или demo")
    out["mode"] = mode

    q = raw.get("queries", "")
    if isinstance(q, str):
        queries = [x.strip() for x in q.splitlines() if x.strip()]
    else:
        queries = [str(x).strip() for x in q if str(x).strip()]
    if not queries:
        raise ValueError("Запросы: нужен хотя бы один поисковый запрос")
    out["queries"] = queries

    sorts = [s for s in raw.get("sorts", []) if s in _ALLOWED_SORTS]
    out["sorts"] = sorts or ["popular"]

    out["pages_per_query"] = _as_int(raw, "pages_per_query", "Страниц на запрос", 1, 10)
    out["interval_sec"] = _as_int(raw, "interval_sec", "Интервал, сек", 60, 86_400)
    out["min_discount_pct"] = _as_float(raw, "min_discount_pct", "Мин. скидка, %", 0, 99)
    out["min_drop_vs_history_pct"] = _as_float(
        raw, "min_drop_vs_history_pct", "Мин. падение vs истории, %", 0, 99)
    out["min_original_price_rub"] = _as_float(
        raw, "min_original_price_rub", "Мин. исходная цена, ₽", 0)
    out["min_saving_rub"] = _as_float(raw, "min_saving_rub", "Мин. выгода, ₽", 0)
    out["min_score"] = _as_int(raw, "min_score", "Мин. скор", 0, 100)
    out["alert_cooldown_hours"] = _as_float(
        raw, "alert_cooldown_hours", "Кулдаун алертов, ч", 0, 24 * 30)
    out["dest"] = _as_int(raw, "dest", "Регион WB (dest)")
    out["spp"] = _as_int(raw, "spp", "СПП, %", 0, 100)

    out["wb_enabled"] = _as_bool(raw, "wb_enabled", True)
    out["ozon_enabled"] = _as_bool(raw, "ozon_enabled", True)
    if not out["wb_enabled"] and not out["ozon_enabled"]:
        raise ValueError("Маркетплейсы: включи хотя бы WB или Ozon")

    out["wb_transport"] = _transport(raw, "wb_transport", "Транспорт WB")
    if raw.get("wb_request_delay_sec") not in (None, ""):
        out["wb_request_delay_sec"] = _as_float(
            raw, "wb_request_delay_sec", "Пауза WB, сек", 0.5, 10)
    out["ozon_transport"] = _transport(raw, "ozon_transport", "Транспорт Ozon")
    if raw.get("ozon_request_delay_sec") not in (None, ""):
        out["ozon_request_delay_sec"] = _as_float(
            raw, "ozon_request_delay_sec", "Пауза Ozon, сек", 1.0, 10)
    out["ozon_browser_fallback"] = _as_bool(
        raw, "ozon_browser_fallback", True)
    if raw.get("live_probe_interval_sec") not in (None, ""):
        out["live_probe_interval_sec"] = _as_int(
            raw, "live_probe_interval_sec", "Повторная проверка LIVE, сек",
            60, 3600)
    out["wb_browser_fallback"] = bool(raw.get("wb_browser_fallback", True))
    out["wb_browser_path"] = str(raw.get("wb_browser_path") or "").strip()[:500]
    out["wb_browser_headless"] = bool(raw.get("wb_browser_headless", False))
    if raw.get("wb_browser_port") not in (None, ""):
        out["wb_browser_port"] = _as_int(
            raw, "wb_browser_port", "Порт браузера", 1024, 65535)
    if raw.get("wb_browser_wait_sec") not in (None, ""):
        out["wb_browser_wait_sec"] = _as_int(
            raw, "wb_browser_wait_sec", "Ожидание браузера, сек", 5, 120)

    out["history_only"] = _as_bool(raw, "history_only", True)
    out["verify_with_price_history"] = bool(raw.get("verify_with_price_history", True))
    out["telegram_enabled"] = bool(raw.get("telegram_enabled", True))
    out["close_to_tray"] = bool(raw.get("close_to_tray", True))
    out["start_minimized"] = bool(raw.get("start_minimized", False))

    token = str(raw.get("telegram_token", "")).strip()
    out["telegram_token"] = token or (existing.get("telegram_token") or "")
    out["telegram_chat_id"] = str(raw.get("telegram_chat_id", "")).strip()
    if out["telegram_enabled"] and out["telegram_chat_id"] and not out["telegram_token"]:
        raise ValueError("Telegram: указан chat_id, но нет токена бота")
    return out


class DesktopController:
    def __init__(self, settings: Settings | None = None, db: DB | None = None):
        self.settings = settings or Settings()
        self.db = db or DB(DB_FILE)
        self.scanner = Scanner(self.db, self.settings)
        self._alert_q: queue.Queue[dict] = queue.Queue()
        self.scanner.on_alert(lambda a: self._alert_q.put(a))

    # --- scanner lifecycle ------------------------------------------- #
    def start_scanner(self) -> None:
        self.scanner.start()

    def stop_scanner(self) -> None:
        self.scanner.stop()

    def toggle_scanner(self) -> bool:
        if self.scanner.running:
            self.scanner.stop()
        else:
            self.scanner.start()
        return self.scanner.running

    def scan_now(self) -> None:
        self.scanner.trigger()

    def shutdown(self, close_browser: bool = True) -> None:
        self.scanner.stop()
        self.scanner.close(close_browser=close_browser)
        self.db.close()

    # --- data for the GUI -------------------------------------------- #
    def status(self) -> dict:
        return {**self.scanner.status(), "stats": self.db.stats(),
                "settings": self.settings.public()}

    def alerts(self, limit: int = 200,
               marketplace: str | None = None,
               only_history_confirmed: bool = False) -> list[dict]:
        return self.db.alerts(
            limit=limit, marketplace=marketplace,
            only_history_confirmed=only_history_confirmed)

    def runs(self, limit: int = 50) -> list[dict]:
        return self.db.runs(limit=limit)

    def dismiss(self, alert_id: int) -> None:
        self.db.dismiss_alert(alert_id)

    def product_history(self, nm: int, marketplace: str = "wb") -> list[dict]:
        return self.db.product_history(nm, marketplace=marketplace)

    def find_lowest_prices(self, query: str,
                           marketplaces: list[str] | None = None,
                           pages: int = 2,
                           strict_match: bool = False,
                           record: bool = True) -> dict:
        """Manual cheapest-current-offer search for the GUI tab."""
        return self.scanner.find_lowest_prices(
            query, marketplaces=marketplaces, pages=pages, limit=50,
            strict_match=strict_match, record=record)

    def drain_alerts(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self._alert_q.get_nowait())
            except queue.Empty:
                return out

    # --- settings / telegram ----------------------------------------- #
    def save_settings(self, raw: Mapping[str, Any]) -> dict:
        parsed = parse_settings_form(raw, self.settings.get())
        self.settings.update(parsed)
        # Mode/marketplaces/transports may have changed: re-probe on scan.
        self.scanner.reset_mode()
        return self.settings.public()

    def diagnose_marketplace(self, marketplace: str,
                             query: str | None = None) -> dict:
        """Run one marketplace connectivity check and return its verdict."""
        return self.scanner.diagnose(query, marketplace=marketplace)

    def diagnose_wb(self, query: str | None = None) -> dict:
        return self.diagnose_marketplace("wb", query)

    def diagnose_ozon(self, query: str | None = None) -> dict:
        return self.diagnose_marketplace("ozon", query)

    def diagnose_all(self, query: str | None = None) -> dict:
        return self.scanner.diagnose_all(query)

    def open_marketplace_browser(self, marketplace: str = "wb") -> dict:
        """Open the shared dedicated Chrome/Edge on a marketplace home so
        the user can finish any browser check there before diagnostics."""
        return self.scanner.open_browser(marketplace)

    def open_wb_browser(self) -> dict:
        return self.open_marketplace_browser("wb")

    def open_ozon_browser(self) -> dict:
        return self.open_marketplace_browser("ozon")

    def telegram_test(self, token: str | None = None,
                      chat_id: str | None = None) -> tuple[bool, str]:
        """Uses form values if given (even unsaved), else saved settings.
        Empty token falls back to the saved token."""
        cfg = self.settings.get()
        tok = (token or "").strip() or cfg.get("telegram_token", "")
        chat = (chat_id or "").strip() or cfg.get("telegram_chat_id", "")
        n = TelegramNotifier(tok, chat)
        if not n.configured:
            return False, "Укажи токен бота и chat_id"
        if n.send_test():
            return True, "Готово: сообщение отправлено — проверь Telegram"
        return False, ("Ошибка: Telegram не принял сообщение. Проверь токен, chat_id "
                       "и что ты нажал /start у бота")
