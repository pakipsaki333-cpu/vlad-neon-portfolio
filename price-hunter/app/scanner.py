"""Scanner: poll WB and Ozon search by queries, record prices, detect
anomalies and fire Telegram alerts.

The two marketplaces are independent live sources sharing one detector,
database, notifier and dedicated browser profile.  A failed WB pass never
discards Ozon products, and vice versa; DEMO fallback in auto mode happens
only when every enabled live source is unavailable.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Iterable

from . import demo, detector
from .config import DATA_DIR, Settings
from .db import DB
from .ozon_client import OzonClient
from .telegram import TelegramNotifier
from .wb_client import WBClient, WBUnavailable, curl_cffi_available  # noqa: F401
from .wb_dom import browser_info

log = logging.getLogger("scanner")

_MARKETPLACES = ("wb", "ozon")
_LABELS = {"wb": "WB", "ozon": "Ozon"}


def _normalize_marketplace(value: Any, product: dict | None = None) -> str:
    marketplace = str(value or "").lower()
    if marketplace in _MARKETPLACES:
        return marketplace
    if product and "ozon." in str(product.get("url") or ""):
        return "ozon"
    return "wb"


def _blank_marketplace_state() -> dict[str, Any]:
    return {
        "available": None,
        "probe": None,
        "last_probe_ts": None,
        "next_probe_ts": None,
        "failures": 0,
        "last_error": None,
        "last_scan_ts": None,
        "last_products": 0,
        "last_new": 0,
        "last_alerts": 0,
        "request_failures": 0,
    }


_LOWEST_STOPWORDS = {
    "для", "и", "в", "на", "с", "по", "от", "до", "из", "у", "the", "a", "an",
}
_LOWEST_UNIT_ALIASES = {
    "гб": "gb", "gb": "gb", "гигабайт": "gb", "гигабайта": "gb",
    "тб": "tb", "tb": "tb", "мл": "ml", "ml": "ml", "мм": "mm", "mm": "mm",
    "см": "cm", "cm": "cm", "кг": "kg", "kg": "kg", "л": "l",
}
_LOWEST_ACCESSORY_STEMS = (
    "чехол", "бампер", "защитн", "стекл", "пленк", "плёнк", "кабел",
    "шнур", "адаптер", "переходник", "зарядн", "ремешок", "подставк",
    "держател", "креплен", "наклейк", "стикер", "запчаст", "комплектующ",
    "сменн", "расходн", "фильтр", "щетк", "щётк", "мешок", "картридж",
)
_LOWEST_VARIANT_MARKERS = {
    "pro", "max", "ultra", "plus", "mini", "lite", "fe", "se", "air",
}


def _lowest_tokens(text: Any) -> list[str]:
    """Small language-tolerant tokenizer for manual cheapest-offer search."""
    normalized = str(text or "").lower().replace("ё", "е")
    raw_tokens = re.findall(r"[a-zа-я0-9]+(?:[.,][0-9]+)?", normalized)
    tokens: list[str] = []
    for token in raw_tokens:
        token = token.strip(".,")
        if not token:
            continue
        attached_unit = re.fullmatch(
            r"(\d+(?:[.,]\d+)?)(гб|gb|тб|tb|мл|ml|мм|mm|см|cm|кг|kg|л|l)",
            token)
        if attached_unit:
            tokens.append(attached_unit.group(1).replace(",", "."))
            tokens.append(_LOWEST_UNIT_ALIASES[attached_unit.group(2)])
            continue
        token = _LOWEST_UNIT_ALIASES.get(token, token)
        tokens.append(token.replace(",", "."))
    return tokens


def _lowest_common_prefix(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    idx = 0
    while idx < limit and a[idx] == b[idx]:
        idx += 1
    return idx


def _lowest_token_matches(query_token: str, title_tokens: Iterable[str]) -> bool:
    token_set = set(title_tokens)
    if query_token in token_set:
        return True
    if any(ch.isdigit() for ch in query_token):
        return False
    if len(query_token) < 5:
        return False
    for token in token_set:
        if len(token) < 4:
            continue
        common = _lowest_common_prefix(query_token, token)
        if common >= 4 and common / min(len(query_token), len(token)) >= 0.75:
            return True
    return False


def _lowest_is_accessory(title_tokens: list[str],
                         query_tokens: list[str]) -> bool:
    """Detect cases/glass/cables etc. without punishing a real product
    whose title merely mentions a charger or a case in the bundle."""
    query_stems = {
        stem for stem in _LOWEST_ACCESSORY_STEMS
        if any(token.startswith(stem.replace("ё", "е"))
               for token in query_tokens)
    }
    for stem in _LOWEST_ACCESSORY_STEMS:
        clean_stem = stem.replace("ё", "е")
        if stem in query_stems or clean_stem in query_stems:
            continue
        positions = [idx for idx, token in enumerate(title_tokens)
                     if token.startswith(clean_stem)]
        if not positions:
            continue
        if min(positions) <= 2:
            return True
        # «чехол для iPhone», «стекло для смартфона» and similar phrases
        # are accessories wherever the phrase starts in a long title.
        if any(pos + 1 < len(title_tokens) and title_tokens[pos + 1] == "для"
               for pos in positions):
            return True
    return False


def _lowest_match_score(product: dict[str, Any], query: str,
                        query_tokens: list[str]) -> tuple[float, bool]:
    title = f"{product.get('brand') or ''} {product.get('name') or ''}"
    title_tokens = _lowest_tokens(title)
    accessory = _lowest_is_accessory(title_tokens, query_tokens)
    clean_query = str(query or "").strip()
    try:
        if clean_query.isdigit() and int(product.get("nm") or 0) == int(clean_query):
            return 1.0, accessory
    except (TypeError, ValueError):
        pass
    if not query_tokens:
        return 0.0, accessory
    matched = sum(
        1 for token in query_tokens
        if _lowest_token_matches(token, title_tokens))
    score = matched / len(query_tokens)
    extra_variants = (_LOWEST_VARIANT_MARKERS & set(title_tokens)) - set(query_tokens)
    if extra_variants and score >= 0.999:
        # iPhone 15 Pro contains every token of «iPhone 15», but it is a
        # different variant; keep it as a close, not exact, match.
        score = 0.89
    return score, accessory


class Scanner:
    def __init__(self, db: DB, settings: Settings):
        self.db = db
        self.settings = settings
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._scan_now = threading.Event()
        self.running = False
        self.scanning = False
        # Live per-request progress of the current scan (marketplace,
        # query position, found so far) so the GUI can show movement
        # instead of an opaque "scanning…" for minutes.
        self._progress: dict[str, Any] | None = None
        self.effective_mode = "—"          # 'live' | 'demo' after first probe
        self.mode_reason: str | None = None
        self.last_probe: dict | None = None
        self.last_probe_ts: int | None = None
        self.next_live_probe_ts: int | None = None
        self.live_failures = 0
        self.last_scan_ts: int | None = None
        self.next_scan_ts: int | None = None
        self.last_error: str | None = None
        self._listeners: list[Callable[[dict], None]] = []
        # Legacy single-client attributes remain the WB aliases used by
        # older tests/integrations.  Generic caches hold both clients.
        self._client_cache: Any = None
        self._client_sig: tuple | None = None
        self._client_caches: dict[str, Any] = {}
        self._client_sigs: dict[str, tuple] = {}
        self._marketplace_states = {
            m: _blank_marketplace_state() for m in _MARKETPLACES}
        self._probe_lock = threading.Lock()
        self._client_lock = threading.RLock()
        self._lowest_search_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def on_alert(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def _emit(self, alert: dict) -> None:
        for cb in self._listeners:
            try:
                cb(alert)
            except Exception:
                pass

    def _state(self, marketplace: str) -> dict[str, Any]:
        return self._marketplace_states[_normalize_marketplace(marketplace)]

    def _enabled_marketplaces(self, cfg: dict | None = None) -> list[str]:
        cfg = cfg or self.settings.get()
        enabled = [m for m in _MARKETPLACES
                   if bool(cfg.get(f"{m}_enabled", True))]
        return enabled or ["wb"]

    # -- clients -------------------------------------------------------- #
    def _new_client_for(self, marketplace: str):
        cfg = self.settings.get()
        profile_dir = str(DATA_DIR / "browser-profile")
        executable = str(cfg.get("wb_browser_path") or "")
        port = int(cfg.get("wb_browser_port", 9229) or 9229)
        headless = bool(cfg.get("wb_browser_headless", False))
        wait_sec = float(cfg.get("wb_browser_wait_sec", 25) or 25)
        if _normalize_marketplace(marketplace) == "ozon":
            return OzonClient(
                transport=cfg.get("ozon_transport", "auto"),
                browser_fallback=bool(cfg.get(
                    "ozon_browser_fallback",
                    cfg.get("wb_browser_fallback", True))),
                browser_executable=executable,
                browser_profile_dir=profile_dir,
                browser_port=port,
                browser_headless=headless,
                browser_wait_sec=wait_sec,
            )
        return WBClient(
            dest=cfg["dest"], spp=cfg["spp"],
            transport=cfg.get("wb_transport", "auto"),
            browser_fallback=bool(cfg.get("wb_browser_fallback", True)),
            browser_executable=executable,
            browser_profile_dir=profile_dir,
            browser_port=port,
            browser_headless=headless,
            browser_wait_sec=wait_sec,
        )

    def _new_client(self) -> WBClient:
        return self._new_client_for("wb")  # type: ignore[return-value]

    def _new_ozon_client(self) -> OzonClient:
        return self._new_client_for("ozon")  # type: ignore[return-value]

    @staticmethod
    def _client_signature_for(cfg: dict, marketplace: str) -> tuple:
        shared_browser = (
            str(cfg.get("wb_browser_path") or ""),
            int(cfg.get("wb_browser_port", 9229) or 9229),
            bool(cfg.get("wb_browser_headless", False)),
            float(cfg.get("wb_browser_wait_sec", 25) or 25),
        )
        if _normalize_marketplace(marketplace) == "ozon":
            return (cfg.get("ozon_transport", "auto"),
                    bool(cfg.get("ozon_browser_fallback",
                                 cfg.get("wb_browser_fallback", True))),
                    *shared_browser)
        return (cfg.get("dest"), cfg.get("spp"),
                cfg.get("wb_transport", "auto"),
                bool(cfg.get("wb_browser_fallback", True)),
                *shared_browser)

    @staticmethod
    def _client_signature(cfg: dict) -> tuple:
        return Scanner._client_signature_for(cfg, "wb")

    def _client_for(self, marketplace: str):
        """Cached per-marketplace client (cookies/endpoint/browser state)."""
        marketplace = _normalize_marketplace(marketplace)
        with self._client_lock:
            if marketplace == "wb" and self._client_cache is not None \
                    and "wb" not in self._client_caches:
                self._client_caches["wb"] = self._client_cache
                if self._client_sig is not None:
                    self._client_sigs["wb"] = self._client_sig

            cfg = self.settings.get()
            sig = self._client_signature_for(cfg, marketplace)
            client = self._client_caches.get(marketplace)
            if client is None or self._client_sigs.get(marketplace) != sig:
                if client is not None:
                    self._close_client(client, close_browser=False)
                client = self._new_client_for(marketplace)
                self._client_caches[marketplace] = client
                self._client_sigs[marketplace] = sig
            if marketplace == "wb":
                self._client_cache = client
                self._client_sig = sig
            return client

    def _client(self) -> WBClient:
        return self._client_for("wb")  # type: ignore[return-value]

    @staticmethod
    def _close_client(client: Any, close_browser: bool) -> None:
        if client is None:
            return
        if close_browser and hasattr(client, "close_browser"):
            try:
                client.close_browser()
            except Exception:
                pass
        if hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass

    def open_browser(self, marketplace: str = "wb") -> dict:
        """Open the shared dedicated Chrome/Edge on a marketplace home."""
        marketplace = _normalize_marketplace(marketplace)
        result = self._client_for(marketplace).open_browser_session()
        return {**result, "marketplace": marketplace}

    def open_wb_browser(self) -> dict:
        return self.open_browser("wb")

    def open_ozon_browser(self) -> dict:
        return self.open_browser("ozon")

    def close(self, close_browser: bool = True) -> None:
        """Release cached clients; optionally terminate their browser.

        CLI diagnostics pass ``close_browser=False`` so the user can keep
        the opened marketplace window, finish its check there, and rerun.
        WB and Ozon intentionally use the same profile/port, so closing a
        launched browser through one client closes the shared window.
        """
        with self._client_lock:
            clients = list(self._client_caches.values())
            if self._client_cache is not None and all(
                    c is not self._client_cache for c in clients):
                clients.append(self._client_cache)
            self._client_caches.clear()
            self._client_sigs.clear()
            self._client_cache = None
            self._client_sig = None
        for client in clients:
            self._close_client(client, close_browser=close_browser)

    # -- mode/probes ---------------------------------------------------- #
    def reset_mode(self) -> None:
        """Forget live/demo verdicts so the next scan re-probes sources."""
        self.effective_mode = "—"
        self.mode_reason = None
        self.next_live_probe_ts = None
        self.last_probe = None
        self.last_probe_ts = None
        self.live_failures = 0
        for state in self._marketplace_states.values():
            state.update({
                "available": None, "probe": None, "last_probe_ts": None,
                "next_probe_ts": None, "failures": 0, "last_error": None,
            })

    def _probe_query(self) -> str:
        queries = self.settings.get().get("queries") or []
        return str(queries[0]) if queries else "наушники"

    def _refresh_mode_state(self) -> str:
        cfg = self.settings.get()
        requested = cfg.get("mode", "auto")
        enabled = self._enabled_marketplaces(cfg)
        parts = []
        available = []
        probed_all = True
        for marketplace in enabled:
            state = self._state(marketplace)
            label = _LABELS[marketplace]
            if state["available"] is True:
                available.append(marketplace)
                parts.append(f"{label} ✓ доступен")
            elif state["available"] is False:
                reason = str(state.get("last_error") or "нет ответа")[:180]
                parts.append(f"{label} ✗ недоступен ({reason})")
            else:
                probed_all = False
                parts.append(f"{label} — ещё не проверен")
        if not available and not probed_all:
            # A source known down plus one never tried is still unresolved,
            # but if we already fell back keep that visible verdict.
            probed_all = False

        joined = "; ".join(parts)
        if requested == "demo":
            self.effective_mode = "demo"
            self.mode_reason = (
                "Выбран DEMO вручную: настоящие товары не ищутся, находки "
                f"сгенерированы. Проверка источников: {joined}.")
        elif available:
            self.effective_mode = "live"
            self.mode_reason = f"Живой поиск: {joined}"
        elif requested == "live":
            self.effective_mode = "live"
            self.mode_reason = f"LIVE: {joined}"
        elif probed_all:
            self.effective_mode = "demo"
            self.mode_reason = (
                "Живые источники недоступны — временный DEMO, настоящие "
                f"товары не ищутся. {joined}")
        else:
            if self.effective_mode not in ("live", "demo"):
                self.effective_mode = "—"
            self.mode_reason = joined or self.mode_reason

        # Legacy aggregate probe fields: prefer WB when it is enabled,
        # otherwise expose the first available enabled source.
        chosen_probe = None
        chosen_ts = None
        for marketplace in enabled:
            state = self._state(marketplace)
            if state.get("probe") is not None:
                chosen_probe = state["probe"]
                chosen_ts = state.get("last_probe_ts")
                break
        if chosen_probe is not None:
            self.last_probe = chosen_probe
            self.last_probe_ts = chosen_ts
        self.live_failures = sum(
            int(self._state(m).get("failures") or 0) for m in enabled)
        next_values = [self._state(m).get("next_probe_ts") for m in enabled]
        next_values = [int(v) for v in next_values if v]
        self.next_live_probe_ts = None if available or requested == "live" \
            else (min(next_values) if next_values else self.next_live_probe_ts)
        state_errors = [str(self._state(m).get("last_error"))
                        for m in enabled if self._state(m).get("last_error")]
        self.last_error = " | ".join(state_errors)[:500] if state_errors else None
        return self.effective_mode

    def _apply_probe(self, probe: dict,
                     marketplace: str | None = None) -> str:
        marketplace = _normalize_marketplace(
            marketplace or (probe or {}).get("marketplace"))
        probe = dict(probe or {})
        probe.setdefault("marketplace", marketplace)
        probe["marketplace"] = marketplace
        cfg = self.settings.get()
        now = int(time.time())
        state = self._state(marketplace)
        state["probe"] = probe
        state["last_probe_ts"] = now
        if probe.get("ok"):
            state.update(available=True, failures=0,
                         next_probe_ts=None, last_error=None)
        else:
            state["available"] = False
            state["failures"] = int(state.get("failures") or 0) + 1
            state["next_probe_ts"] = now + int(
                cfg.get("live_probe_interval_sec", 300))
            state["last_error"] = str(
                probe.get("message") or f"{_LABELS[marketplace]} недоступен")[:500]
        return self._refresh_mode_state()

    def _probe_marketplace(self, marketplace: str,
                           query: str | None = None) -> dict:
        marketplace = _normalize_marketplace(marketplace)
        cfg = self.settings.get()
        transport = str(cfg.get(f"{marketplace}_transport", "auto")
                        or "auto").lower()
        # Non-browser probes use a fresh client so a stale scan endpoint
        # cannot hide recovery.  Browser transports must reuse the cache:
        # two clients must not compete for one profile/port.
        use_cached = transport in (
            "auto", "chrome", "dom", "cdp", "edge", "chromium")
        with self._probe_lock:
            before = self.effective_mode
            client = self._client_for(marketplace) if use_cached \
                else self._new_client_for(marketplace)
            try:
                probe = client.probe(query or self._probe_query())
            finally:
                if not use_cached:
                    self._close_client(client, close_browser=False)
            if not isinstance(probe, dict):
                probe = {"ok": False, "outcome": "bad_shape",
                         "message": "Клиент вернул неожиданный результат проверки"}
            effective = self._apply_probe(probe, marketplace)
        if probe.get("ok") and before != "live" and effective == "live" \
                and self.running:
            self._scan_now.set()
        return {**probe,
                "marketplace": marketplace,
                "enabled": marketplace in self._enabled_marketplaces(cfg),
                "requested_mode": cfg.get("mode", "auto"),
                "effective_mode": self.effective_mode,
                "mode_reason": self.mode_reason,
                "marketplaces": self._marketplace_summary()}

    def diagnose(self, query: str | None = None,
                 marketplace: str = "wb") -> dict:
        """User-triggered marketplace check (WB by default, legacy API)."""
        return self._probe_marketplace(marketplace, query)

    def diagnose_wb(self, query: str | None = None) -> dict:
        return self._probe_marketplace("wb", query)

    def diagnose_ozon(self, query: str | None = None) -> dict:
        return self._probe_marketplace("ozon", query)

    def diagnose_all(self, query: str | None = None,
                     include_disabled: bool = False) -> dict:
        marketplaces = list(_MARKETPLACES) if include_disabled \
            else self._enabled_marketplaces()
        results = {m: self._probe_marketplace(m, query)
                   for m in marketplaces}
        enabled_results = [results[m] for m in self._enabled_marketplaces()
                           if m in results]
        return {
            "ok": any(r.get("ok") for r in enabled_results),
            "outcome": "ok" if any(r.get("ok") for r in enabled_results)
                       else "unavailable",
            "query": query or self._probe_query(),
            "results": results,
            "marketplaces": self._marketplace_summary(),
            "requested_mode": self.settings.get().get("mode", "auto"),
            "effective_mode": self.effective_mode,
            "mode_reason": self.mode_reason,
            "message": self.mode_reason or "",
        }

    def resolve_mode(self, force: bool = False) -> str:
        cfg = self.settings.get()
        requested = cfg.get("mode", "auto")
        enabled = self._enabled_marketplaces(cfg)
        if requested == "demo":
            self._refresh_mode_state()
            return "demo"
        if requested == "live":
            # Legacy tests/control paths can mark probing as done through
            # last_probe_ts.  Otherwise probe sources not checked yet.
            if force or (self.last_probe_ts is None and any(
                    self._state(m).get("probe") is None for m in enabled)):
                for marketplace in enabled:
                    if force or self._state(marketplace).get("probe") is None:
                        self._probe_marketplace(marketplace)
            self.effective_mode = "live"
            return self._refresh_mode_state()

        # auto
        now = int(time.time())
        due: list[str] = []
        unknown: list[str] = []
        legacy_due = bool(self.next_live_probe_ts
                          and now >= int(self.next_live_probe_ts))
        for marketplace in enabled:
            state = self._state(marketplace)
            if state["available"] is None:
                unknown.append(marketplace)
            elif state["available"] is False and (
                    force or legacy_due or not state.get("next_probe_ts")
                    or now >= int(state["next_probe_ts"])):
                due.append(marketplace)

        any_available = any(self._state(m)["available"] is True
                            for m in enabled)
        if not force and self.effective_mode == "live" and any_available:
            # Keep hunting on the healthy source, but periodically try to
            # recover the other one instead of forgetting it forever.
            for marketplace in due:
                self._probe_marketplace(marketplace)
            return self._refresh_mode_state()
        if not force and self.effective_mode == "demo" and not unknown \
                and not due:
            return "demo"

        targets = enabled if force else list(dict.fromkeys(unknown + due))
        if not targets and self.effective_mode not in ("live", "demo"):
            targets = enabled
        for marketplace in targets:
            self._probe_marketplace(marketplace)
        effective = self._refresh_mode_state()
        log.info("auto mode -> %s (%s)", effective, self.mode_reason)
        return effective

    # -- manual lowest-current-price search ------------------------------ #
    def find_lowest_prices(
            self, query: str,
            marketplaces: Iterable[str] | str | None = None,
            pages: int | None = None,
            limit: int = 50,
            strict_match: bool = False,
            record: bool = True) -> dict[str, Any]:
        """Find the cheapest current offers for one user-entered product.

        This is intentionally separate from the anomaly scanner: it does
        not create alerts and does not treat a seller's crossed-out price
        as proof of a bargain.  It asks each selected marketplace for the
        same query (cheapest-first plus popular ordering), de-duplicates
        cards, drops obvious accessories, scores title relevance, and
        returns current offers sorted by the payable/sale price.

        When ``record`` is true in LIVE mode, only the displayed offers
        are also upserted into the product DB.  That grows Price Hunter's
        own price history without sending Telegram alerts from a manual
        search.
        """
        clean_query = " ".join(str(query or "").split()).strip()
        if len(clean_query) < 2:
            raise ValueError("Введи название или модель товара")
        if len(clean_query) > 160:
            raise ValueError("Запрос слишком длинный (максимум 160 символов)")

        query_tokens: list[str] = []
        for token in _lowest_tokens(clean_query):
            if token in _LOWEST_STOPWORDS or token in query_tokens:
                continue
            query_tokens.append(token)
        if not query_tokens:
            raise ValueError("Запрос не содержит названия или модели товара")

        cfg = self.settings.get()
        if marketplaces is None:
            requested = self._enabled_marketplaces(cfg)
        elif isinstance(marketplaces, str):
            requested = [marketplaces]
        else:
            requested = list(marketplaces)
        selected: list[str] = []
        for value in requested:
            marketplace = str(value or "").strip().lower()
            if marketplace not in _MARKETPLACES:
                raise ValueError("Площадка должна быть wb или ozon")
            if marketplace not in selected:
                selected.append(marketplace)
        if not selected:
            raise ValueError("Выбери хотя бы WB или Ozon")

        try:
            page_count = int(pages if pages is not None
                             else cfg.get("pages_per_query", 1) or 1)
        except Exception:
            page_count = 1
        page_count = max(1, min(3, page_count))
        try:
            result_limit = int(limit)
        except Exception:
            result_limit = 50
        result_limit = max(1, min(200, result_limit))

        with self._lowest_search_lock:
            mode = self.resolve_mode()
            raw_by_market: dict[str, list[dict[str, Any]]] = {
                marketplace: [] for marketplace in selected}
            errors_by_market: dict[str, list[str]] = {
                marketplace: [] for marketplace in selected}

            if mode == "demo":
                if "wb" in selected:
                    raw_by_market["wb"] = [
                        dict(product, marketplace="wb", source="demo")
                        for product in demo.generate_scan([clean_query])
                    ]
                if "ozon" in selected:
                    errors_by_market["ozon"].append(
                        "DEMO-каталог эмулирует только WB; для Ozon нужен LIVE/auto")
            else:
                sorts = ("priceup", "popular")
                for marketplace in selected:
                    deduped: dict[int, dict[str, Any]] = {}
                    delay_key = ("wb_request_delay_sec" if marketplace == "wb"
                                 else "ozon_request_delay_sec")
                    try:
                        delay = float(cfg.get(
                            delay_key, 1.2 if marketplace == "wb" else 3.0) or 0)
                    except Exception:
                        delay = 0.0
                    try:
                        client = self._client_for(marketplace)
                    except Exception as e:
                        errors_by_market[marketplace].append(
                            f"{_LABELS[marketplace]}: не удалось создать клиент: {e}")
                        continue
                    for sort_idx, sort in enumerate(sorts):
                        first_id_page1: int | None = None
                        for page in range(1, page_count + 1):
                            try:
                                batch = client.search(
                                    clean_query, page=page, sort=sort) or []
                            except Exception as e:
                                errors_by_market[marketplace].append(
                                    f"{_LABELS[marketplace]} {clean_query!r}/"
                                    f"{sort}/стр.{page}: {str(e)[:260]}")
                                break
                            if not batch:
                                break
                            try:
                                first_id = int(batch[0].get("nm") or 0) or None
                            except Exception:
                                first_id = None
                            if page == 1:
                                first_id_page1 = first_id
                            elif first_id is not None and first_id == first_id_page1:
                                break
                            for raw in batch:
                                if not isinstance(raw, dict):
                                    continue
                                try:
                                    nm = int(raw.get("nm") or raw.get("sku") or 0)
                                    sale = float(raw.get("sale_price_rub") or 0)
                                except Exception:
                                    continue
                                if not nm or sale <= 0:
                                    continue
                                product = dict(raw)
                                product["marketplace"] = marketplace
                                product["nm"] = nm
                                if marketplace == "ozon":
                                    product["sku"] = int(product.get("sku") or nm)
                                current = deduped.get(nm)
                                if current is None or sale < float(
                                        current.get("sale_price_rub") or sale):
                                    if current is not None:
                                        for key in ("photo", "supplier", "brand"):
                                            if not product.get(key) and current.get(key):
                                                product[key] = current[key]
                                    deduped[nm] = product
                            if delay > 0 and (sort_idx < len(sorts) - 1
                                              or page < page_count):
                                time.sleep(delay)
                    raw_by_market[marketplace] = list(deduped.values())

            excluded_accessories = 0
            all_offers: list[dict[str, Any]] = []
            matched_by_market: dict[str, int] = {}
            for marketplace in selected:
                annotated: list[dict[str, Any]] = []
                for raw in raw_by_market.get(marketplace, []):
                    try:
                        sale = float(raw.get("sale_price_rub") or 0)
                        basic = float(raw.get("price_rub") or sale)
                    except Exception:
                        continue
                    if sale <= 0:
                        continue
                    score, accessory = _lowest_match_score(
                        raw, clean_query, query_tokens)
                    if accessory:
                        excluded_accessories += 1
                        continue
                    product = dict(raw)
                    product["marketplace"] = marketplace
                    product.setdefault("brand", "")
                    product.setdefault("supplier", "")
                    product.setdefault("photo", "")
                    product.setdefault("rating", 0)
                    product.setdefault("feedbacks", 0)
                    product["price_rub"] = basic
                    product["sale_price_rub"] = sale
                    product["discount_pct"] = round(
                        (basic - sale) / basic * 100, 1) if basic > sale else 0.0
                    product["match_score"] = round(score, 3)
                    annotated.append(product)

                exact = [p for p in annotated if p["match_score"] >= 0.999]
                close = [p for p in annotated if p["match_score"] >= 0.60]
                similar = [p for p in annotated if p["match_score"] >= 0.34]
                if strict_match:
                    pool = exact
                elif exact:
                    pool = exact
                elif close:
                    pool = close
                else:
                    pool = similar
                for product in pool:
                    product["match_label"] = (
                        "точное" if product["match_score"] >= 0.999
                        else "близкое" if product["match_score"] >= 0.60
                        else "похожее")
                matched_by_market[marketplace] = len(pool)
                all_offers.extend(pool)

            def offer_sort_key(product: dict[str, Any]) -> tuple:
                try:
                    feedbacks = int(product.get("feedbacks") or 0)
                except Exception:
                    feedbacks = 0
                return (
                    float(product.get("sale_price_rub") or 0),
                    -float(product.get("match_score") or 0),
                    -feedbacks,
                    str(product.get("name") or ""),
                )

            all_offers.sort(key=offer_sort_key)
            offers = all_offers[:result_limit]

            recorded = 0
            for product in offers:
                marketplace = _normalize_marketplace(
                    product.get("marketplace"), product)
                own = self.db.history_stats(
                    int(product["nm"]), marketplace=marketplace)
                product["own_history_count"] = own.get("count", 0)
                product["own_history_median"] = own.get("median")
                product["own_history_min"] = own.get("min")
                if record and mode == "live":
                    try:
                        self.db.upsert_product(product)
                        recorded += 1
                    except Exception:
                        pass

            cheapest = offers[0] if offers else None
            cheapest_price = float(cheapest["sale_price_rub"]) if cheapest else None
            for idx, product in enumerate(offers, 1):
                product["rank"] = idx
                product["is_cheapest"] = idx == 1
                if cheapest_price:
                    product["above_cheapest_rub"] = round(
                        float(product["sale_price_rub"]) - cheapest_price, 2)
                    product["above_cheapest_pct"] = round(
                        (float(product["sale_price_rub"]) / cheapest_price - 1) * 100, 1)
                else:
                    product["above_cheapest_rub"] = None
                    product["above_cheapest_pct"] = None

            by_marketplace: dict[str, dict[str, Any]] = {}
            for marketplace in selected:
                marketplace_offers = [
                    p for p in all_offers
                    if _normalize_marketplace(p.get("marketplace"), p) == marketplace]
                by_marketplace[marketplace] = {
                    "label": _LABELS[marketplace],
                    "raw_count": len(raw_by_market.get(marketplace, [])),
                    "matched_count": matched_by_market.get(marketplace, 0),
                    "cheapest": marketplace_offers[0] if marketplace_offers else None,
                    "errors": errors_by_market.get(marketplace, []),
                }

            flat_errors = [error for marketplace in selected
                           for error in errors_by_market.get(marketplace, [])]
            if cheapest:
                message = (
                    f"Самая низкая текущая цена: "
                    f"{_LABELS[_normalize_marketplace(cheapest.get('marketplace'), cheapest)]} — "
                    f"{float(cheapest['sale_price_rub']):,.0f} ₽. "
                    f"Подходящих предложений: {len(all_offers)}.")
            elif flat_errors:
                message = ("Не удалось получить подходящие предложения. "
                           "Проверь доступность площадок и попробуй точнее указать модель.")
            else:
                message = ("Подходящих предложений не найдено. Уточни бренд, "
                           "модель и вариант (например, объём памяти).")

            return {
                "ok": bool(offers),
                "query": clean_query,
                "searched_at": int(time.time()),
                "mode": mode,
                "effective_mode": self.effective_mode,
                "requested_mode": cfg.get("mode", "auto"),
                "marketplaces": selected,
                "pages": page_count,
                "strict_match": bool(strict_match),
                "offers": offers,
                "cheapest": cheapest,
                "by_marketplace": by_marketplace,
                "total_raw": sum(len(v) for v in raw_by_market.values()),
                "total_matched": len(all_offers),
                "excluded_accessories": excluded_accessories,
                "recorded": recorded,
                "errors": flat_errors,
                "message": message,
            }

    # -- marketplace fetching ------------------------------------------- #
    def _fetch_marketplace(self, marketplace: str, cfg: dict,
                           client: Any, *,
                           queries: list[str] | None = None,
                           seen: set[int] | None = None,
                           fetch_state: dict[str, Any] | None = None,
                           query_start: int = 1,
                           query_total: int | None = None,
                           ) -> tuple[list[dict], list[str]]:
        """Fetch products for one marketplace over the given queries.

        ``seen``/``fetch_state`` can be shared across calls so a scan
        cycle may interleave queries between marketplaces (WB q1,
        Ozon q1, WB q2, …) while de-duplication, progress counters
        and the consecutive-failure cut-off stay per-source cumulative.
        """
        products: list[dict[str, Any]] = []
        failures: list[str] = []
        if seen is None:
            seen = set()
        if fetch_state is None:
            fetch_state = {}
        fetch_state.setdefault("consecutive_failures", 0)
        fetch_state.setdefault("stop", False)
        fetch_state.setdefault("any_products", False)
        fetch_state.setdefault("found", 0)
        fetch_state.setdefault("failures", 0)
        delay_key = "wb_request_delay_sec" if marketplace == "wb" \
            else "ozon_request_delay_sec"
        delay = float(cfg.get(delay_key, 1.2 if marketplace == "wb" else 3.0))
        label = _LABELS[marketplace]
        query_list = list(queries) if queries is not None \
            else list(cfg.get("queries") or [])
        total = query_total or len(query_list)
        for offset, query in enumerate(query_list):
            q_idx = query_start + offset
            if self._stop.is_set() or fetch_state["stop"]:
                break
            for sort in cfg.get("sorts", ["popular"]):
                if fetch_state["stop"] or self._stop.is_set():
                    break
                first_id_page1: int | None = None
                for page in range(1, int(cfg.get("pages_per_query", 1)) + 1):
                    if self._stop.is_set():
                        break
                    self._progress = {
                        "marketplace": marketplace, "label": label,
                        "query": query, "query_idx": q_idx,
                        "query_total": total, "sort": sort,
                        "page": page, "found": fetch_state["found"],
                        "failures": fetch_state["failures"],
                    }
                    try:
                        batch = client.search(query, page=page, sort=sort) or []
                    except Exception as e:
                        msg = (f"{label} {query!r}/{sort}/стр.{page}: "
                               f"{str(e)[:240]}")
                        failures.append(msg)
                        fetch_state["failures"] += 1
                        log.warning("%s search failed (%s)", label, msg)
                        fetch_state["consecutive_failures"] += 1
                        if (fetch_state["consecutive_failures"] >= 3
                                and not fetch_state["any_products"]) \
                                or fetch_state["consecutive_failures"] >= 5:
                            fetch_state["stop"] = True
                        break
                    fetch_state["consecutive_failures"] = 0
                    if not batch:
                        break
                    fetch_state["any_products"] = True
                    if page == 1:
                        first_id_page1 = batch[0]["nm"]
                    elif batch[0]["nm"] == first_id_page1:
                        break  # marketplace wrapped around to page 1
                    for product in batch:
                        product = dict(product)
                        product["marketplace"] = marketplace
                        if product["nm"] not in seen:
                            seen.add(product["nm"])
                            products.append(product)
                            fetch_state["found"] += 1
                    time.sleep(max(0.0, delay))
        return products, failures

    def _apply_scan_result(self, marketplace: str, products: list[dict],
                           failures: list[str],
                           reset_counts: bool = True) -> None:
        cfg = self.settings.get()
        now = int(time.time())
        state = self._state(marketplace)
        state.update(last_scan_ts=now, last_products=len(products),
                     request_failures=len(failures))
        if reset_counts:
            state.update(last_new=0, last_alerts=0)
        if products:
            state.update(available=True, failures=0, next_probe_ts=None)
            state["last_error"] = (
                f"Частично: {_LABELS[marketplace]} не ответил на "
                f"{len(failures)} запрос(ов)" if failures else None)
        elif failures:
            state["available"] = False
            state["failures"] = int(state.get("failures") or 0) + 1
            state["next_probe_ts"] = now + int(
                cfg.get("live_probe_interval_sec", 300))
            state["last_error"] = failures[-1][:500]
        else:
            # Search calls completed with recognized empty result sets.
            state.update(available=True, failures=0, next_probe_ts=None,
                         last_error=None)

    def _process_products(
            self, products: list[dict[str, Any]], cfg: dict, mode: str,
            live_clients: dict[str, Any],
            source_results: dict[str, dict[str, int]]) -> tuple[int, int, int]:
        """Persist/detect/alert one source's products immediately.

        Processing is intentionally interleaved with fetching: WB finds
        must reach Telegram before a slow Ozon DOM pass (and vice versa),
        instead of waiting for every marketplace to finish.
        """
        scanned = new_products = alerts_made = 0
        notifier = TelegramNotifier(
            cfg["telegram_token"], cfg["telegram_chat_id"])
        for product in products:
            if self._stop.is_set():
                break  # paused mid-processing; next scan re-sees them
            marketplace = _normalize_marketplace(
                product.get("marketplace"), product)
            product["marketplace"] = marketplace
            scanned += 1
            if self.db.upsert_product(product):
                new_products += 1
                self._state(marketplace)["last_new"] += 1
                if marketplace in source_results:
                    source_results[marketplace]["new"] += 1
            own = self.db.history_stats(
                product["nm"], marketplace=marketplace)
            hist_median = None
            hist_min = None
            external_hist_stats = None
            discount = 0.0
            if product["price_rub"] > product["sale_price_rub"]:
                discount = ((product["price_rub"] - product["sale_price_rub"])
                            / product["price_rub"] * 100)
            own_ready = own.get("count", 0) >= 3 and bool(own.get("median"))
            if cfg.get("history_only", True):
                # The sticker discount must not decide whether we look for
                # a normal-price baseline: in history-only mode, external
                # WB/demo history is needed precisely while our own
                # snapshot history is still too short.
                needs_external_history = not own_ready
            else:
                # Legacy mixed mode keeps its cheaper pre-filter.
                needs_external_history = (
                    discount >= cfg["min_discount_pct"] or (
                        own.get("median")
                        and product["sale_price_rub"] < own["median"] * 0.7))
            if needs_external_history and cfg.get("verify_with_price_history"):
                if mode == "live":
                    client = live_clients.get(marketplace)
                    stats_fn = getattr(client, "history_stats", None)
                    history_fn = getattr(client, "history_median", None)
                    try:
                        if callable(stats_fn):
                            external_hist_stats = stats_fn(product["nm"]) or {}
                            hist_median = external_hist_stats.get("median")
                            hist_min = external_hist_stats.get("min")
                        elif callable(history_fn):
                            hist_median = history_fn(product["nm"])
                    except Exception:
                        hist_median = hist_min = None
                        external_hist_stats = None
                elif mode == "demo":
                    external_hist_stats = demo.demo_history_stats(product["nm"])
                    hist_median = external_hist_stats.get("median")
                    hist_min = external_hist_stats.get("min")

            alert = detector.evaluate(
                product, own, hist_median, cfg, wb_hist_min=hist_min)
            if not alert:
                continue
            if self.db.recent_alert(
                    product["nm"], product["sale_price_rub"],
                    cfg["alert_cooldown_hours"], marketplace=marketplace):
                continue
            alert["marketplace"] = marketplace
            if marketplace == "ozon":
                alert["sku"] = product.get("sku") or product["nm"]
            alert["source"] = mode
            alert["photo"] = product.get("photo") or ""
            alert["raw"] = {
                **product, "detected_at": int(time.time()),
                "own_history": own,
                "external_hist_median": hist_median,
                "external_hist_min": hist_min,
                "external_hist_stats": external_hist_stats,
                "wb_hist_median": hist_median if marketplace == "wb" else None,
                "wb_hist_min": hist_min if marketplace == "wb" else None,
                "hist_min": alert.get("hist_min"),
                "vs_hist_min_pct": alert.get("vs_hist_min_pct"),
                "history_source": alert.get("history_source"),
                "history_only": alert.get("history_only"),
            }
            alert_id = self.db.add_alert(alert)
            alert["id"] = alert_id
            alert["ts"] = int(time.time())
            alerts_made += 1
            self._state(marketplace)["last_alerts"] += 1
            if marketplace in source_results:
                source_results[marketplace]["alerts"] += 1
            log.info(
                "ALERT %s id=%s %s₽ history -%.0f%% (sticker -%.0f%%) "
                "score=%s (%s)", marketplace, product["nm"],
                product["sale_price_rub"],
                alert.get("drop_vs_median_pct") or 0,
                alert["discount_pct"], alert["score"], mode)
            if cfg.get("telegram_enabled") and notifier.configured:
                if notifier.send_alert(alert):
                    self.db.mark_telegram_sent(alert_id)
                    alert["telegram_sent"] = 1
            self._emit(alert)
        return scanned, new_products, alerts_made

    # ------------------------------------------------------------------ #
    def scan_once(self) -> dict:
        cfg = self.settings.get()
        mode = self.resolve_mode()
        enabled = self._enabled_marketplaces(cfg)
        run_mode = mode if mode != "live" else "live:" + "+".join(enabled)
        run_id = self.db.start_run(run_mode, len(cfg.get("queries") or []))
        scanned = new_products = alerts_made = 0
        error = None
        request_failures: list[str] = []
        source_results: dict[str, dict[str, int]] = {}
        live_clients: dict[str, Any] = {}
        self.scanning = True
        try:
            total_found = 0
            if mode == "live":
                requested = cfg.get("mode", "auto")
                active: list[str] = []
                clients: dict[str, Any] = {}
                accumulated: dict[str, dict[str, Any]] = {}
                for marketplace in enabled:
                    state = self._state(marketplace)
                    next_probe = state.get("next_probe_ts")
                    if (requested == "auto" and state["available"] is False
                            and next_probe and int(time.time()) < int(next_probe)):
                        # The healthy source keeps this scan LIVE.  Do not
                        # hammer the known-down source on every short scan;
                        # resolve_mode() will probe it again when due (or a
                        # manual diagnosis can revive it immediately).
                        source_results[marketplace] = {
                            "products": 0, "new": 0, "alerts": 0,
                            "request_failures": 0, "skipped": 1,
                        }
                        continue
                    client = self._client_for(marketplace)
                    clients[marketplace] = client
                    live_clients[marketplace] = client
                    active.append(marketplace)
                    # Chunk processing below increments these as alerts
                    # happen, so reset them up front (the end-of-cycle
                    # _apply_scan_result must not wipe them again).
                    state.update(last_new=0, last_alerts=0)
                    source_results[marketplace] = {
                        "products": 0, "new": 0, "alerts": 0,
                        "request_failures": 0,
                    }
                    accumulated[marketplace] = {
                        "products": [], "failures": [], "seen": set(),
                        "fetch_state": {},
                    }
                # Interleave marketplaces by query (WB q1 → Ozon q1 →
                # WB q2 …) and process/alert each chunk immediately.
                # Fetching one source completely before the other let a
                # long WB DOM pass (plus price-history processing of
                # hundreds of items) starve Ozon for a whole cycle —
                # after app restarts Ozon was effectively never reached
                # and its DB stayed empty while its diagnostics passed.
                queries = list(cfg.get("queries") or [])
                for q_idx, query in enumerate(queries, 1):
                    if self._stop.is_set():
                        break
                    for marketplace in active:
                        if self._stop.is_set():
                            break
                        acc = accumulated[marketplace]
                        found, failures = self._fetch_marketplace(
                            marketplace, cfg, clients[marketplace],
                            queries=[query], seen=acc["seen"],
                            fetch_state=acc["fetch_state"],
                            query_start=q_idx, query_total=len(queries))
                        acc["products"].extend(found)
                        acc["failures"].extend(failures)
                        source_results[marketplace]["products"] = len(
                            acc["products"])
                        source_results[marketplace]["request_failures"] = len(
                            acc["failures"])
                        total_found += len(found)
                        request_failures.extend(failures)
                        done, fresh, made = self._process_products(
                            found, cfg, mode, live_clients, source_results)
                        scanned += done
                        new_products += fresh
                        alerts_made += made
                for marketplace in active:
                    acc = accumulated[marketplace]
                    self._apply_scan_result(
                        marketplace, acc["products"], acc["failures"],
                        reset_counts=False)
                if not total_found and request_failures:
                    labels = ", ".join(_LABELS[m] for m in enabled)
                    raise RuntimeError(
                        f"{labels} не отдал(и) ни одного товара. "
                        + " | ".join(request_failures[:3]))
                if request_failures:
                    counts = ", ".join(
                        f"{_LABELS[m]} — {source_results[m]['request_failures']}"
                        for m in enabled
                        if source_results.get(m, {}).get("request_failures"))
                    error = ("Частично: запросы без ответа: " + counts + ": "
                             + " | ".join(request_failures[:2]))
                    self.last_error = error[:500]
            else:
                products = [dict(p, marketplace="wb")
                            for p in demo.generate_scan(cfg.get("queries") or [])]
                source_results["wb"] = {
                    "products": len(products), "new": 0, "alerts": 0,
                    "request_failures": 0,
                }
                scanned, new_products, alerts_made = self._process_products(
                    products, cfg, mode, live_clients, source_results)

            if mode == "live" and scanned > 0:
                self._refresh_mode_state()
                if error:
                    self.last_error = error[:500]
        except Exception as e:
            error = str(e)[:500]
            log.warning("scan failed: %s", e)
            if mode == "live":
                self._refresh_mode_state()
                any_available = any(
                    self._state(m)["available"] is True for m in enabled)
                if self.settings.get().get("mode") == "auto" \
                        and not any_available:
                    self.effective_mode = "demo"
                    self.mode_reason = (
                        "Живые источники недоступны — временный DEMO, "
                        "настоящие товары не ищутся. Причина: "
                        f"{error[:220]}")
                    if not self.next_live_probe_ts:
                        self.next_live_probe_ts = int(time.time()) + int(
                            cfg.get("live_probe_interval_sec", 300))
                    log.warning("falling back to DEMO until %s",
                                self.next_live_probe_ts)
                elif any_available:
                    self.effective_mode = "live"
                    self.mode_reason = (
                        f"{self.mode_reason or 'Живой поиск'}; последний "
                        f"скан завершился с ошибкой: {error[:180]}")
                else:
                    self.effective_mode = "live"
                    self.mode_reason = f"LIVE: последний скан упал: {error[:220]}"
            self.last_error = error
        finally:
            self.scanning = False
            self._progress = None
            self.last_scan_ts = int(time.time())
            self.db.finish_run(run_id, scanned, new_products, alerts_made, error)
        return {
            "scanned": scanned, "new": new_products, "alerts": alerts_made,
            "error": error, "mode": mode,
            "effective_mode": self.effective_mode,
            "mode_reason": self.mode_reason,
            "request_failures": len(request_failures),
            "marketplaces": source_results,
            "source_results": source_results,
            "scanned_wb": source_results.get("wb", {}).get("products", 0),
            "scanned_ozon": source_results.get("ozon", {}).get("products", 0),
            "alerts_wb": source_results.get("wb", {}).get("alerts", 0),
            "alerts_ozon": source_results.get("ozon", {}).get("alerts", 0),
        }

    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        self.resolve_mode()
        while not self._stop.is_set():
            self.scan_once()
            cfg = self.settings.get()
            interval = int(cfg["interval_sec"])
            if self.effective_mode == "demo":
                if cfg.get("mode") == "demo":
                    interval = min(interval, 45)
                else:
                    interval = min(
                        interval, int(cfg.get("live_probe_interval_sec", 300)))
            self.next_scan_ts = int(time.time()) + interval
            self._scan_now.wait(timeout=interval)
            self._scan_now.clear()
        self.running = False
        self.next_scan_ts = None

    def start(self) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        if not self.running:
            return False
        self._stop.set()
        self._scan_now.set()
        return True

    def trigger(self) -> None:
        if self.running:
            self._scan_now.set()
        else:
            threading.Thread(target=self.scan_once, daemon=True).start()

    def _marketplace_summary(self) -> dict[str, dict[str, Any]]:
        cfg = self.settings.get()
        enabled = set(self._enabled_marketplaces(cfg))
        summary: dict[str, dict[str, Any]] = {}
        for marketplace in _MARKETPLACES:
            state = self._state(marketplace)
            summary[marketplace] = {
                "label": _LABELS[marketplace],
                "enabled": marketplace in enabled,
                "available": state.get("available"),
                "probe": state.get("probe"),
                "last_probe": state.get("probe"),
                "last_probe_ts": state.get("last_probe_ts"),
                "next_probe_ts": state.get("next_probe_ts"),
                "failures": state.get("failures", 0),
                "last_error": state.get("last_error"),
                "last_scan_ts": state.get("last_scan_ts"),
                "last_products": state.get("last_products", 0),
                "last_new": state.get("last_new", 0),
                "last_alerts": state.get("last_alerts", 0),
                "request_failures": state.get("request_failures", 0),
                "transport": cfg.get(f"{marketplace}_transport", "auto"),
            }
        return summary

    def status(self) -> dict:
        cfg = self.settings.get()
        requested = cfg.get("mode", "auto")
        marketplaces = self._marketplace_summary()
        return {
            "running": self.running,
            "scanning": self.scanning,
            "stopping": bool(self.running and self._stop.is_set()),
            "progress": self._progress if self.scanning else None,
            "mode": requested,
            "requested_mode": requested,
            "effective_mode": self.effective_mode,
            "mode_reason": self.mode_reason,
            "last_probe": self.last_probe,
            "last_probe_wb": marketplaces["wb"]["last_probe"],
            "last_probe_ozon": marketplaces["ozon"]["last_probe"],
            "last_probe_ts": self.last_probe_ts,
            "next_live_probe_ts": self.next_live_probe_ts,
            "live_failures": self.live_failures,
            "marketplaces": marketplaces,
            "wb_enabled": marketplaces["wb"]["enabled"],
            "ozon_enabled": marketplaces["ozon"]["enabled"],
            "wb_transport": cfg.get("wb_transport", "auto"),
            "ozon_transport": cfg.get("ozon_transport", "auto"),
            "browser_transport_available": curl_cffi_available(),
            "chrome_transport": browser_info(str(cfg.get("wb_browser_path") or "")),
            "last_scan_ts": self.last_scan_ts,
            "next_scan_ts": self.next_scan_ts if self.running else None,
            "last_error": self.last_error,
        }
