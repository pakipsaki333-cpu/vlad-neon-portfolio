"""Ozon storefront search client.

Ozon is materially stricter than WB for application-made HTTP requests:
plain clients commonly receive 403 or an HTTP 307 anti-bot redirect loop
while the storefront works in a browser.  This client therefore mirrors
the proven Price Hunter WB escalation ladder:

1. Ozon composer/entrypoint JSON via httpx (and curl_cffi in ``auto``);
2. the same composer JSON via ``fetch()`` inside the installed real
   Chrome/Edge on an ozon.ru tab;
3. as a last resort, the rendered search-result DOM in that browser.

The parser is deliberately tolerant.  Ozon's composer response is a
``widgetStates`` map whose values are JSON strings; current desktop
search tiles live in ``tileGridDesktop`` as typed ``mainState`` atoms.
Older/alternate widgets are handled by recursively looking for product
objects.  No account login, cookies export, Seller API key or payment
data is involved.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import quote_plus, urlencode, urlparse

import httpx

from .wb_dom import (OZON_HOME, BrowserSessionError, ChromeBrowserSession,
                     browser_info, chrome_available)

log = logging.getLogger("ozon")

API_ENDPOINTS = [
    "https://www.ozon.ru/api/composer-api.bx/page/json/v2",
    "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2",
    "https://api.ozon.ru/composer-api.bx/page/json/v2",
]

_SORT_MAP = {
    # Price Hunter/WB spellings
    "popular": "",
    "benefit": "discount",
    "priceup": "price",
    "pricedown": "price_desc",
    "rate": "rating",
    # Ozon spellings are accepted too
    "score": "",
    "price": "price",
    "price_desc": "price_desc",
    "rating": "rating",
    "new": "new",
    "discount": "discount",
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_OZON_SEARCH_EXTRACT_JS = r"""
(() => {
  const seen = new Map();
  const anchors = Array.from(document.querySelectorAll('a[href*="/product/"]'));
  for (const a of anchors) {
    const href = a.href || '';
    const match = href.match(/-(\d+)\/?(?:\?|#|$)/) || href.match(/\/product\/(\d+)\/?(?:\?|#|$)/);
    if (!match || seen.has(match[1])) continue;

    let card = a;
    let candidate = a;
    for (let i = 0; i < 7 && candidate; i++) {
      const txt = candidate.innerText || '';
      const productLinks = candidate.querySelectorAll
        ? candidate.querySelectorAll('a[href*="/product/"]').length : 0;
      if (txt.includes('₽') && txt.length < 1800 && productLinks <= 4) {
        card = candidate;
        break;
      }
      candidate = candidate.parentElement;
    }
    const text = (card && card.innerText ? card.innerText : (a.innerText || '')).trim();
    if (!text.includes('₽')) continue;

    const img = card ? card.querySelector('img') : null;
    const delText = card
      ? Array.from(card.querySelectorAll('del, s, strike'))
          .map(e => (e.innerText || '').trim()).join(' ')
      : '';
    seen.set(match[1], {
      id: Number(match[1]),
      href: href,
      title: (a.getAttribute('aria-label') || (img && img.alt) || '').trim(),
      text: text,
      delText: delText,
      photo: img ? (img.currentSrc || img.src || '') : ''
    });
  }
  return {
    url: location.href,
    title: document.title,
    bodyText: (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 2000),
    items: Array.from(seen.values())
  };
})()
"""

_MONEY_RE = re.compile(
    r"(\d[\d\s\u00a0\u202f]*(?:[,.]\d{1,2})?)\s*(?:₽|руб\.?)", re.I)
_RATING_RE = re.compile(r"(?<![\d,.])([0-5][,.]\d)(?![\d,.])")
_FEEDBACK_RE = re.compile(
    r"(?<![\d,.])(\d[\d\s\u00a0\u202f]*)\s*"
    r"(?:оценок|оценки|оценк[аиу]|отзывов|отзыва|отзыв)",
    re.I,
)
_NAME_STOP_PREFIXES = (
    "с ozon картой", "ozon карта", "без ozon карты", "с картой ozon",
    "распродажа", "оригинал", "хит", "новинка", "акция", "стало дешевле",
    "реклама", "доставка", "завтра", "сегодня", "осталось", "самовывоз",
    "есть в наличии", "нет в наличии", "в корзину", "купить", "премиум",
)
_BADGE_RE = re.compile(
    r"^(стало дешевле|оригинал|хит|новинка|акция|распродажа|выбор|"
    r"бестселлер|ozon|premium|премиум|скидка|самовывоз)", re.I)


class OzonUnavailable(RuntimeError):
    """Every Ozon endpoint/transport failed, with diagnostics."""

    def __init__(self, message: str, attempts: list[dict] | None = None,
                 outcome: str | None = None):
        super().__init__(message)
        self.attempts = attempts or []
        self.outcome = outcome or _dominant_outcome(self.attempts)


# Compatibility with the old phase-2 stub import path.
OzonNotImplemented = OzonUnavailable


def _dominant_outcome(attempts: list[dict]) -> str:
    for wanted in ("blocked", "rate_limited", "network_error",
                   "bad_shape", "bad_json", "http_error", "empty"):
        if any(a.get("outcome") == wanted for a in attempts):
            return wanted
    return "http_error"


def _summarize_attempts(attempts: list[dict], limit: int = 4) -> str:
    parts = []
    for a in attempts[-limit:]:
        host = urlparse(a.get("endpoint") or "").netloc or "ozon"
        status = a.get("status")
        parts.append(f"{host}/{a.get('transport', '?')}: "
                     f"{a.get('outcome', '?')}"
                     + (f" HTTP {status}" if status else ""))
    return "; ".join(parts) or "попыток не было"


def _clean_product_url(link: Any, sku: int | None = None) -> str:
    raw = str(link or "")
    if raw:
        parsed = urlparse(raw)
        path = parsed.path if parsed.netloc else raw.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/product/"):
            return "https://www.ozon.ru" + path
        if parsed.netloc.endswith("ozon.ru") and "/product/" in path:
            return f"https://www.ozon.ru{path}"
    return f"https://www.ozon.ru/product/{sku}/" if sku else "https://www.ozon.ru/"


def _sku_from_url(url: Any) -> int | None:
    text = str(url or "")
    m = re.search(r"-(\d+)/?(?:\?|#|$)", text) or re.search(r"/product/(\d+)/?", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _money_to_float(value: Any) -> float | None:
    """Normalize Ozon money strings/numbers to roubles.

    Composer tile prices are display strings in roubles.  A bare number
    is also treated as roubles: dividing every large integer by 100
    (a common parser shortcut) would turn a real 134 313 ₽ phone into
    1 343 ₽ and create exactly the false alerts this app must avoid.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("text", "price", "value", "amount", "cardPrice", "finalPrice"):
            parsed = _money_to_float(value.get(key))
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if result > 0 else None
    text = str(value).strip()
    m = _MONEY_RE.search(text)
    raw = m.group(1) if m else text
    raw = re.sub(r"[\s\u00a0\u202f]", "", raw).replace(",", ".")
    raw = re.sub(r"[^0-9.]", "", raw)
    if not raw:
        return None
    try:
        result = float(raw)
    except ValueError:
        return None
    return result if result > 0 else None


def _money_values(text: Any) -> list[float]:
    values: list[float] = []
    for line in str(text or "").splitlines() or [str(text or "")]:
        low = line.lower()
        if ("/мес" in low or "в месяц" in low or "рассроч" in low or
                "платеж" in low or "достав" in low or "балл" in low or
                "цена за" in low):
            continue
        for match in _MONEY_RE.finditer(line):
            # A per-unit amount (``50 ₽/шт``, ``120 ₽/100 г``) can share a
            # visual line with the product price; exclude only that match.
            suffix = line[match.end():match.end() + 16].lstrip().lower()
            if suffix.startswith(("/", "за шт", "за 1", "за 100")):
                continue
            value = _money_to_float(match.group(0))
            if value:
                values.append(value)
    return values


def _is_name_line(line: str) -> bool:
    low = line.lower().strip()
    if len(low) < 3 or "₽" in low:
        return False
    if re.fullmatch(r"[-−]?\d+\s*%", low) or re.match(r"^\d+[,.]\d\b", low):
        return False
    return not any(low.startswith(prefix) for prefix in _NAME_STOP_PREFIXES)


def _normalized(sku: int, name: str, brand: str, supplier: str,
                basic: float, sale: float, rating: Any, feedbacks: Any,
                photo: str, url: str, transport: str,
                price_kind: str = "card_or_regular_lowest") -> dict[str, Any]:
    try:
        rating_value = float(str(rating).replace(",", ".")) if rating else 0.0
    except (TypeError, ValueError):
        rating_value = 0.0
    try:
        feedback_value = int(float(str(feedbacks).replace(" ", ""))) if feedbacks else 0
    except (TypeError, ValueError):
        feedback_value = 0
    photo_url = str(photo or "")
    if photo_url.startswith("//"):
        photo_url = "https:" + photo_url
    return {
        "marketplace": "ozon",
        "nm": int(sku),          # legacy internal product-id key
        "sku": int(sku),
        "name": (name or f"Товар Ozon {sku}").strip(),
        "brand": (brand or "").strip(),
        "supplier": (supplier or "").strip(),
        "supplier_id": None,
        "price_rub": float(basic),
        "sale_price_rub": float(sale),
        "rating": rating_value,
        "feedbacks": feedback_value,
        "photo": photo_url if photo_url.startswith("http") else "",
        "url": _clean_product_url(url, int(sku)),
        "size": "",
        "total_quantity": None,
        "source_transport": transport,
        "price_kind": price_kind,
    }


def parse_ozon_dom_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Interpret rendered Ozon tile text in pure Python."""
    products: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in items or []:
        try:
            sku = int(item.get("id") or item.get("sku") or 0)
        except (TypeError, ValueError):
            sku = _sku_from_url(item.get("href")) or 0
        if not sku or sku in seen:
            continue
        text = str(item.get("text") or "")
        amounts = _money_values(text)
        if not amounts:
            continue
        seen.add(sku)
        sale = min(amounts)
        struck = _money_values(item.get("delText") or item.get("del") or "")
        basic = max(struck) if struck else max(amounts)
        if basic < sale:
            basic = sale

        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        candidate = str(item.get("title") or "").strip()
        if candidate.lower() in (
                "перейти к товару", "открыть товар", "подробнее",
                "смотреть товар", "товар"):
            candidate = ""
        if not candidate or "₽" in candidate:
            candidate = next((line for line in lines if _is_name_line(line)), "")

        rating_match = _RATING_RE.search(text)
        rating = rating_match.group(1) if rating_match else 0
        feedback_match = _FEEDBACK_RE.search(text)
        feedbacks = (re.sub(r"[\s\u00a0\u202f]", "", feedback_match.group(1))
                     if feedback_match else 0)
        products.append(_normalized(
            sku, candidate, "", "", basic, sale, rating, feedbacks,
            str(item.get("photo") or ""), str(item.get("href") or ""),
            "chrome-dom"))
    return products


def _iter_widget_states(payload: Any) -> list[tuple[str, Any]]:
    if not isinstance(payload, dict):
        return []
    states = payload.get("widgetStates")
    if not isinstance(states, dict):
        return []
    out: list[tuple[str, Any]] = []
    for key, raw in states.items():
        if isinstance(raw, dict):
            out.append((str(key), raw))
        elif isinstance(raw, str):
            try:
                out.append((str(key), json.loads(raw)))
            except json.JSONDecodeError:
                continue
    return out


def _widget_base(key: str) -> str:
    return str(key).split("-", 1)[0]


def _state_texts(state: dict[str, Any]) -> list[str]:
    label = state.get("labelListV2") if isinstance(state, dict) else None
    if not isinstance(label, dict):
        return []
    texts = []
    for item in label.get("items") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text_obj = item.get("text")
        text = text_obj.get("text") if isinstance(text_obj, dict) else text_obj
        if text:
            texts.append(str(text).strip())
    return texts


def _parse_tile_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    main_state = item.get("mainState")
    if not isinstance(main_state, list):
        return None

    price_block = None
    name = ""
    rating: Any = 0
    feedbacks: Any = 0
    brand = ""
    for state in main_state:
        if not isinstance(state, dict):
            continue
        if state.get("type") == "priceV2" and isinstance(state.get("priceV2"), dict):
            price_block = state["priceV2"]
        if state.get("id") == "name":
            text_ds = state.get("textDS")
            if isinstance(text_ds, dict):
                name = str(text_ds.get("text") or "")
        label = state.get("labelListV2")
        if isinstance(label, dict):
            encoded = json.dumps(label, ensure_ascii=False)
            texts = _state_texts(state)
            if "ic_s_star" in encoded:
                if texts:
                    rating = texts[0]
                if len(texts) > 1:
                    feedbacks = re.sub(r"[^\d]", "", texts[1])
            elif not brand:
                candidate = next((t for t in texts if t and not _BADGE_RE.search(t)), "")
                if candidate:
                    brand = candidate

    if not isinstance(price_block, dict):
        return None
    prices = price_block.get("price") or []
    sale_values: list[float] = []
    basic_values: list[float] = []
    if isinstance(prices, list):
        for entry in prices:
            if not isinstance(entry, dict):
                continue
            value = _money_to_float(entry.get("text"))
            if not value:
                continue
            style = str(entry.get("textStyle") or "")
            if style == "PRICE":
                sale_values.append(value)
            elif style == "ORIGINAL_PRICE":
                basic_values.append(value)
    # Ozon can expose both card and regular payable amounts as PRICE;
    # Price Hunter consistently tracks the lowest payable price.
    sale = min(sale_values) if sale_values else None
    basic = max(basic_values) if basic_values else None
    if sale is None:
        sale = _money_to_float(price_block.get("price"))
    if not sale:
        return None
    if not basic or basic < sale:
        basic = sale

    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    link = action.get("link") if isinstance(action, dict) else ""
    raw_sku = item.get("sku") or item.get("id") or _sku_from_url(link)
    try:
        sku = int(str(raw_sku))
    except (TypeError, ValueError):
        return None

    photo = ""
    tile_image = item.get("tileImage") if isinstance(item.get("tileImage"), dict) else {}
    if isinstance(tile_image, dict):
        photo = str(tile_image.get("coverImage") or "")
        if not photo:
            for image_item in tile_image.get("items") or []:
                if not isinstance(image_item, dict):
                    continue
                image = image_item.get("image") if isinstance(image_item.get("image"), dict) else {}
                if isinstance(image, dict) and image.get("link"):
                    photo = str(image["link"])
                    break
    return _normalized(sku, name, brand, "", basic, sale, rating, feedbacks,
                       photo, str(link or ""), "ozon-composer",
                       price_kind="composer_price")


def _find_tile_items(node: Any, acc: list[dict], depth: int = 0) -> None:
    if depth > 9:
        return
    if isinstance(node, dict):
        if isinstance(node.get("mainState"), list):
            acc.append(node)
        for value in node.values():
            _find_tile_items(value, acc, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _find_tile_items(value, acc, depth + 1)


def _find_product_objects(node: Any, acc: list[dict], depth: int = 0) -> None:
    if depth > 10:
        return
    if isinstance(node, dict):
        product = node.get("product")
        if isinstance(product, dict):
            acc.append(product)
        for value in node.values():
            _find_product_objects(value, acc, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _find_product_objects(value, acc, depth + 1)


def _parse_simple_product(raw: dict[str, Any]) -> dict[str, Any] | None:
    sku_raw = raw.get("id") or raw.get("sku") or raw.get("productId")
    try:
        sku = int(str(sku_raw))
    except (TypeError, ValueError):
        return None
    price_obj = raw.get("price") if isinstance(raw.get("price"), dict) else {}
    sale = None
    for candidate in (price_obj.get("cardPrice"), price_obj.get("price"),
                      price_obj.get("finalPrice"), price_obj.get("value"),
                      raw.get("cardPrice"), raw.get("finalPrice"), raw.get("price")):
        sale = _money_to_float(candidate)
        if sale:
            break
    if not sale:
        return None
    basic = None
    for candidate in (price_obj.get("oldPrice"), price_obj.get("originalPrice"),
                      raw.get("oldPrice"), raw.get("originalPrice"),
                      raw.get("old_price")):
        basic = _money_to_float(candidate)
        if basic:
            break
    if not basic or basic < sale:
        basic = sale
    photo = ""
    for key in ("image", "imageUrl", "photo", "coverImage"):
        if isinstance(raw.get(key), str) and raw[key].startswith(("http", "//")):
            photo = raw[key]
            break
    if not photo and isinstance(raw.get("images"), list):
        photo = next((str(x) for x in raw["images"]
                      if isinstance(x, str) and x.startswith(("http", "//"))), "")
    return _normalized(
        sku, str(raw.get("title") or raw.get("name") or ""),
        str(raw.get("brand") or ""), str(raw.get("seller") or raw.get("supplier") or ""),
        basic, sale, raw.get("rating") or 0,
        raw.get("reviews") or raw.get("feedbacks") or raw.get("reviewsCount") or 0,
        photo, str(raw.get("url") or raw.get("link") or ""), "ozon-composer",
        price_kind="composer_price")


def parse_composer_search(payload: Any) -> tuple[list[dict] | None, str, int | None]:
    """Return (products|None, shape, raw_count). None means drift."""
    widgets = _iter_widget_states(payload)
    if not widgets:
        return None, "unknown", None

    products: list[dict[str, Any]] = []
    seen: set[int] = set()
    raw_count = 0
    recognized = False

    for key, widget in widgets:
        if _widget_base(key) not in ("tileGridDesktop", "tileGridMobile"):
            continue
        recognized = True
        items: list[dict] = []
        if isinstance(widget, dict) and isinstance(widget.get("items"), list):
            items = [x for x in widget["items"] if isinstance(x, dict)]
        else:
            _find_tile_items(widget, items)
        raw_count += len(items)
        for item in items:
            product = _parse_tile_item(item)
            if product and product["nm"] not in seen:
                seen.add(product["nm"])
                products.append(product)
    if products:
        return products, "tileGridDesktop", raw_count
    if recognized and raw_count == 0:
        return [], "tileGridDesktop", 0

    for key, widget in widgets:
        if not (key.startswith("webSearchResults") or "searchResults" in key):
            continue
        recognized = True
        raw_products: list[dict] = []
        _find_product_objects(widget, raw_products)
        raw_count += len(raw_products)
        for raw in raw_products:
            product = _parse_simple_product(raw)
            if product and product["nm"] not in seen:
                seen.add(product["nm"])
                products.append(product)
    if products:
        return products, "webSearchResults.product", raw_count
    if recognized and raw_count == 0:
        return [], "search-widget-empty", 0
    if recognized:
        # A recognized widget contained raw items, but none yielded a SKU
        # and price.  That is shape drift (or only non-product tiles), not
        # proof of an empty search: caller must try rendered DOM.
        return None, "unparsed-search-widget", raw_count
    return None, "unknown", raw_count


@dataclass
class _Response:
    status_code: int
    text: str
    headers: dict[str, str]
    url: str
    transport: str
    elapsed_ms: int


def curl_cffi_available() -> bool:
    return importlib.util.find_spec("curl_cffi") is not None


def _is_blocked(resp: _Response | None) -> bool:
    if resp is None:
        return False
    if resp.status_code in (401, 403, 407, 498):
        return True
    location = (resp.headers.get("location") or "").lower()
    if resp.status_code in (301, 302, 303, 307, 308) and "__rr" in location:
        return True
    body = (resp.text or "")[:4000].lower()
    return any(marker in body for marker in (
        "antibot", "captcha", "доступ ограничен",
        "подтвердите, что вы не робот", "автоматические запросы"))


def _classify(resp: _Response | None, parse_failed: bool = False) -> str:
    if resp is None:
        return "network_error"
    if resp.status_code == 429:
        return "rate_limited"
    if _is_blocked(resp):
        return "blocked"
    if resp.status_code != 200:
        return "http_error"
    if parse_failed:
        return "bad_json"
    return "ok"


@dataclass
class OzonClient:
    timeout: float = 20.0
    transport: str = "auto"  # auto | httpx | browser(curl) | chrome(real)
    browser_fallback: bool = True
    browser_executable: str = ""
    browser_profile_dir: str = ""
    browser_port: int = 9229
    browser_headless: bool = False
    browser_wait_sec: float = 25
    _httpx_client: Any = field(default=None, repr=False)
    _curl_session: Any = field(default=None, repr=False)
    _browser_session: Any = field(default=None, repr=False)
    _browser_settled: bool = field(default=False, repr=False)
    _prefer_dom_search: bool = field(default=False, repr=False)
    last_attempts: list[dict] = field(default_factory=list, repr=False)
    last_endpoint: str | None = field(default=None, repr=False)
    last_transport: str | None = field(default=None, repr=False)

    def __post_init__(self):
        t = str(self.transport or "auto").lower()
        t = {"curl_cffi": "browser", "curl": "browser", "dom": "chrome",
             "cdp": "chrome", "edge": "chrome", "chromium": "chrome"}.get(t, t)
        self.transport = t if t in ("auto", "httpx", "browser", "chrome") else "auto"

    # -- browser -------------------------------------------------------- #
    def _real_browser_session(self) -> ChromeBrowserSession:
        if self._browser_session is None:
            self._browser_session = ChromeBrowserSession(
                executable=self.browser_executable,
                profile_dir=self.browser_profile_dir,
                port=self.browser_port,
                headless=self.browser_headless,
                wait_sec=self.browser_wait_sec,
                timeout=max(25.0, self.timeout),
                home_url=OZON_HOME,
            )
        return self._browser_session

    def _chrome_allowed(self) -> bool:
        return self.transport == "chrome" or (
            self.transport == "auto" and self.browser_fallback)

    def _dom_allowed(self) -> bool:
        return self.transport == "chrome" or (
            self.transport == "auto" and self.browser_fallback
            and chrome_available(self.browser_executable))

    def open_browser_session(self) -> dict:
        return self._real_browser_session().bring_to_front(OZON_HOME)

    def close_browser(self) -> None:
        if self._browser_session is not None:
            self._browser_session.terminate()
            self._browser_session = None
            self._browser_settled = False

    # -- HTTP plumbing --------------------------------------------------- #
    @staticmethod
    def _headers(browser: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://www.ozon.ru",
            "Referer": "https://www.ozon.ru/",
            "x-o3-app-name": "dweb_client",
        }
        if not browser:
            headers["User-Agent"] = _USER_AGENT
        return headers

    def _raw_get(self, transport: str, url: str,
                 params: dict | None) -> _Response:
        started = time.monotonic()
        if transport == "chrome":
            session = self._real_browser_session()
            if not self._browser_settled:
                # Ozon's Variti/Qrator-style check sets its cookies from
                # page JS after the home document becomes interactive.
                # Give a fresh dedicated profile one short settle window
                # before the first composer fetch; later calls reuse it.
                session.ensure_ready(OZON_HOME)
                time.sleep(6)
                self._browser_settled = True
            payload = session.fetch(url, params)
            return _Response(
                status_code=int(payload.get("status") or 0),
                text=str(payload.get("text") or ""),
                headers={str(k).lower(): str(v) for k, v in
                         dict(payload.get("headers") or {}).items()},
                url=str(payload.get("url") or url), transport=transport,
                elapsed_ms=int((time.monotonic() - started) * 1000))
        if transport == "browser":
            from curl_cffi import requests as curl_requests
            if self._curl_session is None:
                self._curl_session = curl_requests.Session(impersonate="chrome")
            response = self._curl_session.get(
                url, params=params, headers=self._headers(browser=True),
                timeout=self.timeout, allow_redirects=False)
        else:
            if self._httpx_client is None:
                self._httpx_client = httpx.Client(
                    timeout=self.timeout, follow_redirects=False)
            response = self._httpx_client.get(
                url, params=params, headers=self._headers(browser=False))
        return _Response(
            status_code=int(response.status_code), text=response.text or "",
            headers={str(k).lower(): str(v)
                     for k, v in dict(response.headers).items()},
            url=str(response.url), transport=transport,
            elapsed_ms=int((time.monotonic() - started) * 1000))

    def _transports(self) -> list[str]:
        if self.transport == "httpx":
            return ["httpx"]
        if self.transport == "browser":
            return ["browser", "httpx"]
        if self.transport == "chrome":
            return ["chrome", "httpx"]
        transports = ["httpx"]
        if curl_cffi_available():
            transports.append("browser")
        if self.browser_fallback and chrome_available(self.browser_executable):
            transports.append("chrome")
        return transports

    def _request_json(self, endpoint: str, site_path: str,
                      attempts: list[dict]):
        transports = self._transports()
        params = {"url": site_path}
        for idx, transport in enumerate(transports):
            started = time.monotonic()
            try:
                resp = self._raw_get(transport, endpoint, params)
            except Exception as e:
                outcome = getattr(e, "outcome", "network_error") \
                    if isinstance(e, BrowserSessionError) else "network_error"
                attempts.append({
                    "endpoint": endpoint, "transport": transport,
                    "status": None, "outcome": outcome,
                    "error": str(e)[:350],
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                })
                if transport in ("browser", "chrome") and idx < len(transports) - 1:
                    continue
                return None, None, None
            payload = None
            parse_failed = False
            if resp.status_code == 200:
                try:
                    payload = json.loads(resp.text)
                except Exception:
                    parse_failed = True
            outcome = _classify(resp, parse_failed)
            attempt = {
                "endpoint": endpoint, "transport": transport,
                "status": resp.status_code, "outcome": outcome,
                "server": resp.headers.get("server"),
                "content_type": resp.headers.get("content-type"),
                "location": resp.headers.get("location"),
                "elapsed_ms": resp.elapsed_ms,
            }
            if outcome in ("blocked", "bad_json") and idx < len(transports) - 1:
                attempts.append(attempt)
                continue
            return resp, payload, attempt
        return None, None, None

    # -- search ---------------------------------------------------------- #
    @staticmethod
    def _search_path(query: str, page: int = 1, sort: str = "popular") -> str:
        params: dict[str, Any] = {"text": query, "from_global": "true"}
        sorting = _SORT_MAP.get(str(sort or "popular").lower(), "")
        if sorting:
            params["sorting"] = sorting
        if int(page or 1) > 1:
            params["page"] = int(page)
        return "/search/?" + urlencode(params, quote_via=quote_plus)

    @classmethod
    def _search_url(cls, query: str, page: int = 1,
                    sort: str = "popular") -> str:
        return "https://www.ozon.ru" + cls._search_path(query, page, sort)

    def _search_dom(self, query: str, page: int, sort: str,
                    attempts: list[dict]) -> tuple[list[dict], bool]:
        started = time.monotonic()
        url = self._search_url(query, page, sort)
        try:
            payload = self._real_browser_session().rendered_page_payload(
                url, 'a[href*="/product/"]', _OZON_SEARCH_EXTRACT_JS,
                scrolls=4)
        except BrowserSessionError as e:
            attempts.append({
                "endpoint": url, "transport": "chrome-dom", "status": None,
                "outcome": e.outcome, "error": str(e)[:350],
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            })
            return [], False
        except Exception as e:
            attempts.append({
                "endpoint": url, "transport": "chrome-dom", "status": None,
                "outcome": "network_error", "error": str(e)[:350],
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            })
            return [], False
        items = payload.get("items") or []
        products = parse_ozon_dom_items(items)
        body = str(payload.get("bodyText") or "").lower()
        recognized_empty = not products and any(
            phrase in body for phrase in (
                "ничего не найдено", "ничего не нашлось",
                "по вашему запросу ничего", "товары не найдены"))
        attempts.append({
            "endpoint": str(payload.get("url") or url),
            "transport": "chrome-dom", "status": 200,
            "outcome": "ok" if products else (
                "empty" if recognized_empty else "bad_shape"),
            "shape": "rendered-dom", "raw_products": len(items),
            "products": len(products),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        })
        return products, recognized_empty

    def search(self, query: str, page: int = 1,
               sort: str = "popular") -> list[dict]:
        attempts: list[dict] = []
        if self._prefer_dom_search and self._dom_allowed():
            products, recognized_empty = self._search_dom(
                query, page, sort, attempts)
            if products:
                self.last_attempts = attempts
                self.last_endpoint = attempts[-1].get("endpoint")
                self.last_transport = "chrome-dom"
                return products
            if recognized_empty:
                self.last_attempts = attempts
                return []
            self._prefer_dom_search = False

        site_path = self._search_path(query, page, sort)
        valid_empty = False
        for endpoint in API_ENDPOINTS:
            resp, payload, attempt = self._request_json(
                endpoint, site_path, attempts)
            if resp is None or attempt is None:
                continue
            if attempt["outcome"] == "ok":
                products, shape, raw_count = parse_composer_search(payload)
                attempt.update(shape=shape, raw_products=raw_count,
                               products=len(products) if products is not None else None)
                if products:
                    for product in products:
                        product["source_transport"] = attempt["transport"]
                    attempts.append(attempt)
                    self.last_attempts = attempts
                    self.last_endpoint = endpoint
                    self.last_transport = attempt["transport"]
                    return products
                if products == []:
                    attempt["outcome"] = "empty"
                    valid_empty = True
                else:
                    attempt["outcome"] = "bad_shape"
            attempts.append(attempt)
            if attempt["outcome"] == "rate_limited":
                time.sleep(2)

        if not valid_empty and self._dom_allowed():
            products, recognized_empty = self._search_dom(
                query, page, sort, attempts)
            if products:
                self._prefer_dom_search = True
                self.last_attempts = attempts
                self.last_endpoint = attempts[-1].get("endpoint")
                self.last_transport = "chrome-dom"
                return products
            valid_empty = valid_empty or recognized_empty

        self.last_attempts = attempts
        if valid_empty:
            return []
        raise OzonUnavailable(
            f"Ozon не отдал товары по запросу {query!r}: "
            + _summarize_attempts(attempts), attempts=attempts)

    # -- diagnostics/compatibility --------------------------------------- #
    def history_median(self, _sku: int) -> float | None:
        # Ozon has no public equivalent of WB basket price-history here;
        # the scanner's own DB history is the reference.
        return None

    def probe(self, query: str = "наушники") -> dict:
        started = time.monotonic()
        info = browser_info(self.browser_executable)
        base: dict[str, Any] = {
            "ok": False, "outcome": "unknown", "marketplace": "ozon",
            "query": query, "transport_setting": self.transport,
            "browser_transport_available": curl_cffi_available(),
            "chrome_transport_available": info["available"],
            "chrome_executable_found": info["executable_found"],
            "chrome_executable": info["executable"],
            "chrome_websocket_available": info["websocket_available"],
            "chrome_fallback_enabled": self.browser_fallback,
            "chrome_port": self.browser_port,
            "products": 0, "sample": None, "card_ok": None,
            "attempts": [], "message": "", "hint": "",
        }
        try:
            products = self.search(query, page=1)
        except OzonUnavailable as e:
            base.update(outcome=e.outcome, attempts=e.attempts, message=str(e))
            base["hint"] = _hint_for(e.outcome, base)
            return base
        except Exception as e:
            base.update(outcome="network_error", message=f"Ошибка запроса к Ozon: {e}",
                        attempts=list(self.last_attempts))
            base["hint"] = _hint_for("network_error", base)
            return base

        base["attempts"] = list(self.last_attempts)
        if not products:
            base.update(outcome="empty",
                        message="Ozon отвечает, но по тестовому запросу товары не нашлись")
            base["hint"] = ("Открой тот же поиск на ozon.ru в отдельном окне "
                            "Price Hunter и проверь город/выдачу.")
            return base
        first = products[0]
        base.update(
            ok=True, outcome="ok", products=len(products),
            endpoint=self.last_endpoint, transport=self.last_transport,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            sample=f"{first.get('brand', '')} {first.get('name', '')}"[:80].strip(),
            message=(f"Ozon доступен: {len(products)} товаров по запросу "
                     f"«{query}» (транспорт: {self.last_transport})"))
        return base

    def close(self) -> None:
        for client in (self._httpx_client, self._curl_session):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        self._httpx_client = self._curl_session = None
        if self._browser_session is not None:
            self._browser_session.close()
            self._browser_session = None


def _hint_for(outcome: str, probe: dict) -> str:
    if outcome == "blocked":
        if not probe.get("chrome_transport_available"):
            if not probe.get("chrome_executable_found"):
                return ("Ozon блокирует запросы приложения, а Chrome/Edge на ПК не найден. "
                        "Установи браузер или укажи путь к нему в настройках, затем выбери "
                        "транспорт Ozon «chrome» и повтори проверку.")
            return ("Ozon блокирует транспорты приложения, а настоящий браузерный CDP "
                    "недоступен в этой сборке. Обнови приложение; из исходников нужен "
                    "websocket-client.")
        tried_chrome = any(str(a.get("transport", "")).startswith("chrome")
                           for a in probe.get("attempts") or [])
        if tried_chrome:
            return ("Ozon не пропустил даже настоящий браузер на этом проходе. Оставь "
                    "открытым отдельное окно Price Hunter, дождись/пройди проверку именно "
                    "в нём, при необходимости выбери город доставки и повтори диагностику. "
                    "Вход в аккаунт не требуется; не закрывай окно во время охоты.")
        return ("Ozon блокирует httpx/curl_cffi. В Настройках выбери транспорт Ozon "
                "«chrome», сохрани и проверь Ozon ещё раз: откроется отдельный профиль "
                "браузера, в котором можно пройти проверку вручную.")
    if outcome == "rate_limited":
        return ("Ozon ограничил частоту. Увеличь интервал и паузу Ozon, уменьши число "
                "страниц/сортировок.")
    if outcome == "network_error":
        return ("Проверь интернет, DNS/VPN и что ozon.ru открывается с этого ПК. "
                "Если сайт работает только в браузере, используй транспорт chrome.")
    if outcome in ("bad_shape", "bad_json"):
        return ("Ozon, похоже, изменил формат widgetStates/DOM. Сохрани диагностику "
                "с endpoint/HTTP/shape — по ней обновляется парсер.")
    return ""
