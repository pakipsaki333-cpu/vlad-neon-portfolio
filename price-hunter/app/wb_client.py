"""Wildberries storefront JSON client.

WB rotates endpoint versions and response envelopes without notice, so
this client is deliberately defensive:

* current search endpoints first (v18 on u-search / search /
  www.__internal), older known versions as fallbacks;
* every response shape seen in the wild is accepted: top-level
  ``products``, ``data.products``, a bare list, ...  (Reading only
  ``data.products`` was the "WB works but finds nothing" bug: v4/v18
  answer with top-level ``products``.)
* card detail uses ``cards/v4/detail`` (v1/v2 are 404 now);
* each attempt is classified (ok / blocked / rate_limited / empty /
  bad_shape / http_error / network_error) and kept in ``last_attempts``
  so the UI can tell the user *why* WB is not answering instead of
  silently pretending there are simply no products;
* transport: plain ``httpx`` first. Some networks see WB refuse the
  Python TLS fingerprint with 403/498 while a browser from the same IP
  works; only then (transport="auto") does it retry through
  ``curl_cffi`` with Chrome impersonation, if that package is installed.
  If *that* is still refused, it can use the installed real Chrome/Edge
  over CDP (see :mod:`app.wb_dom`): first ``fetch()`` from inside a live
  WB tab, then the already rendered search DOM as a last resort.

No account, token or payment data is involved. The client only READS
public storefront prices and stays polite (see request delay in the
scanner).
"""
from __future__ import annotations

import importlib.util
import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from .basket import photo_url, price_history_urls, product_url
from .wb_dom import (BrowserSessionError, ChromeBrowserSession, browser_info,
                     chrome_available, parse_dom_search_items)

log = logging.getLogger("wb")

SEARCH_ENDPOINTS = [
    "https://u-search.wb.ru/exactmatch/ru/common/v18/search",
    "https://search.wb.ru/exactmatch/ru/common/v18/search",
    "https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search",
    "https://www.wildberries.ru/__internal/search/exactmatch/ru/common/v18/search",
    "https://search.wb.ru/exactmatch/ru/common/v9/search",
    "https://search.wb.ru/exactmatch/ru/common/v5/search",
    "https://search.wb.ru/exactmatch/ru/common/v4/search",
]
CARD_ENDPOINTS = [
    "https://card.wb.ru/cards/v4/detail",
    "https://www.wildberries.ru/__internal/u-card/cards/v4/detail",
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Hosts known to sit behind WB's TLS/antibot gate.
_GATED_SUFFIXES = ("search.wb.ru", "card.wb.ru", "catalog.wb.ru",
                   "wildberries.ru")

_OUTCOME_TEXT = {
    "blocked": "WB заблокировал запрос (антибот 403/498)",
    "rate_limited": "WB ограничил частоту запросов (429)",
    "empty": "WB ответил, но список товаров пустой",
    "bad_shape": "WB ответил в неожиданном формате",
    "bad_json": "WB вернул не JSON (похоже на антибот-страницу)",
    "http_error": "WB вернул HTTP-ошибку",
    "network_error": "нет сетевого доступа к WB",
}


class WBUnavailable(RuntimeError):
    """Every endpoint/transport failed. Carries per-attempt diagnostics."""

    def __init__(self, message: str, attempts: list[dict] | None = None,
                 outcome: str | None = None):
        super().__init__(message)
        self.attempts = attempts or []
        self.outcome = outcome or _dominant_outcome(self.attempts)


def _dominant_outcome(attempts: list[dict]) -> str:
    for wanted in ("blocked", "rate_limited", "network_error",
                   "bad_shape", "bad_json", "http_error", "empty"):
        if any(a.get("outcome") == wanted for a in attempts):
            return wanted
    return "http_error"


def _summarize_attempts(attempts: list[dict], limit: int = 4) -> str:
    parts = []
    for a in attempts[-limit:]:
        host = urlparse(a.get("endpoint") or "").netloc or "wb"
        status = a.get("status")
        parts.append(f"{host}/{a.get('transport', '?')}: "
                     f"{a.get('outcome', '?')}"
                     + (f" HTTP {status}" if status else ""))
    return "; ".join(parts) or "попыток не было"


# --------------------------------------------------------------------------- #
# Response shapes / normalization
# --------------------------------------------------------------------------- #
def extract_products(payload: Any) -> tuple[list[dict] | None, str, int | None]:
    """Return (raw_products | None, shape_name, total | None).

    ``None`` products means "shape not recognized" — very different from
    a recognized-but-empty result list.
    """
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)], "list", len(payload)
    if not isinstance(payload, dict):
        return None, "unknown", None

    def _total(container: dict) -> int | None:
        for key in ("total", "totalCount", "count"):
            v = container.get(key)
            if isinstance(v, int):
                return v
        meta = container.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("total"), int):
            return meta["total"]
        return None

    if isinstance(payload.get("products"), list):
        return ([p for p in payload["products"] if isinstance(p, dict)],
                "products", _total(payload))
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("products"), list):
        return ([p for p in data["products"] if isinstance(p, dict)],
                "data.products", _total(data) or _total(payload))
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)], "data[]", len(data)
    if isinstance(payload.get("items"), list):
        return ([p for p in payload["items"] if isinstance(p, dict)],
                "items", _total(payload))
    return None, "unknown", _total(payload)


def _to_int_price(value: Any) -> int | None:
    try:
        if value is None:
            return None
        v = int(float(value))
        return v if v > 0 else None
    except Exception:
        return None


def _first_price(container: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        v = _to_int_price(container.get(key))
        if v:
            return v
    return None


def _extract_price(raw: dict[str, Any]) -> tuple[int, int, str] | None:
    """(basic_kopecks, sale_kopecks, size_label) — cheapest in-stock size."""
    sale = _first_price(raw, ("salePriceU", "clientSalePriceU", "sale_price",
                              "priceProduct", "finalPrice"))
    basic = _first_price(raw, ("priceU", "clientPriceU", "basicPrice", "price"))
    if sale:
        return (basic or sale), sale, ""

    candidates: list[tuple[int, int, str, int | None]] = []
    for size in raw.get("sizes") or []:
        if not isinstance(size, dict):
            continue
        price = size.get("price") if isinstance(size.get("price"), dict) else {}
        s_sale = (_first_price(price, ("product", "total", "client", "sale",
                                       "final"))
                  or _first_price(size, ("salePriceU", "clientSalePriceU")))
        if not s_sale:
            continue
        s_basic = (_first_price(price, ("basic", "price", "base"))
                   or _first_price(size, ("priceU", "clientPriceU")) or s_sale)
        stock: int | None = None
        stocks = size.get("stocks")
        if isinstance(stocks, list) and stocks:
            stock = 0
            for st in stocks:
                if isinstance(st, dict):
                    for k in ("qty", "quantity", "count"):
                        v = _to_int_price(st.get(k))
                        if v:
                            stock += v
                            break
        label = str(size.get("name") or size.get("origName")
                    or size.get("techSize") or "").strip()
        candidates.append((s_basic, s_sale, label, stock))
    if not candidates:
        return None
    in_stock = [c for c in candidates if (c[3] or 0) > 0]
    pool = in_stock or candidates
    basic_c, sale_c, label, _stock = min(pool, key=lambda c: c[1])
    return basic_c, sale_c, label


def normalize_product(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a raw WB product into our internal schema. Prices in RUB."""
    nm = raw.get("id") or raw.get("nmID") or raw.get("nmId") or raw.get("nm_id")
    if not nm:
        return None
    extracted = _extract_price(raw)
    if not extracted:
        return None
    basic_u, sale_u, size_label = extracted
    return {
        "nm": int(nm),
        "name": (raw.get("name") or "").strip(),
        "brand": (raw.get("brand") or "").strip(),
        "supplier": (raw.get("supplier") or "").strip(),
        "supplier_id": raw.get("supplierId"),
        "price_rub": round(basic_u / 100.0, 2),     # price before discount
        "sale_price_rub": round(sale_u / 100.0, 2),  # final price to pay
        "rating": (raw.get("reviewRating") or raw.get("nmReviewRating")
                   or raw.get("rating") or 0),
        "feedbacks": (raw.get("feedbacks") or raw.get("nmFeedbacks")
                      or raw.get("feedbacksCount") or 0),
        "size": size_label,
        "total_quantity": raw.get("totalQuantity"),
        "photo": photo_url(int(nm)),
        "url": product_url(int(nm)),
    }


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
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


def _is_blocked_body(resp: _Response) -> bool:
    server = (resp.headers.get("server") or "").lower()
    if "wbaas" in server:
        return True
    body = (resp.text or "")[:4000].lower()
    return any(marker in body for marker in (
        "wbaas", "antibot", "подозрительная активность",
        "x_wbaas_token", "captcha"))


def _classify(resp: _Response | None, payload: Any = None,
              parse_failed: bool = False) -> str:
    if resp is None:
        return "network_error"
    if resp.status_code == 429:
        return "rate_limited"
    if resp.status_code in (401, 403, 407, 498) or _is_blocked_body(resp):
        return "blocked"
    if resp.status_code != 200:
        return "http_error"
    if parse_failed:
        return "bad_json"
    return "ok"


@dataclass
class WBClient:
    dest: int = -1257786
    spp: int = 30
    timeout: float = 15.0
    # auto | httpx | browser (curl_cffi impersonation) |
    # chrome (installed real Chrome/Edge via CDP, incl. rendered-DOM reads)
    transport: str = "auto"
    browser_fallback: bool = True        # auto may escalate to real Chrome
    browser_executable: str = ""         # optional explicit Chrome/Edge path
    browser_profile_dir: str = ""        # dedicated local browser profile
    browser_port: int = 9229
    browser_headless: bool = False
    browser_wait_sec: float = 25
    _httpx_client: Any = field(default=None, repr=False)
    _curl_session: Any = field(default=None, repr=False)
    _browser_session: Any = field(default=None, repr=False)
    _prefer_dom_search: bool = field(default=False, repr=False)
    _dom_empty_streak: int = field(default=0, repr=False)
    _history_cache: dict = field(default_factory=dict, repr=False)
    _search_idx: int = field(default=0, repr=False)
    _card_idx: int = field(default=0, repr=False)
    last_attempts: list[dict] = field(default_factory=list, repr=False)
    last_endpoint: str | None = field(default=None, repr=False)
    last_transport: str | None = field(default=None, repr=False)

    def __post_init__(self):
        t = str(self.transport or "auto").lower()
        aliases = {"curl_cffi": "browser", "curl": "browser",
                   "dom": "chrome", "cdp": "chrome", "edge": "chrome",
                   "real_browser": "chrome", "chromium": "chrome"}
        t = aliases.get(t, t)
        self.transport = t if t in ("auto", "httpx", "browser", "chrome") else "auto"

    # -- real browser session ------------------------------------------ #
    def _real_browser_session(self) -> ChromeBrowserSession:
        if self._browser_session is None:
            self._browser_session = ChromeBrowserSession(
                executable=self.browser_executable,
                profile_dir=self.browser_profile_dir,
                port=self.browser_port,
                headless=self.browser_headless,
                wait_sec=self.browser_wait_sec,
                timeout=max(20.0, self.timeout),
            )
        return self._browser_session

    def _chrome_allowed(self) -> bool:
        return self.transport == "chrome" or (
            self.transport == "auto" and self.browser_fallback)

    def open_browser_session(self) -> dict:
        """Open/warm WB's own tab in the dedicated Chrome/Edge window."""
        return self._real_browser_session().bring_to_front()

    def close_browser(self) -> None:
        """Terminate the dedicated browser if this app launched it."""
        if self._browser_session is not None:
            self._browser_session.terminate()
            self._browser_session = None

    # -- low level ----------------------------------------------------- #
    def _headers(self, browser: bool = False) -> dict[str, str]:
        h = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://www.wildberries.ru",
            "Referer": "https://www.wildberries.ru/",
        }
        if not browser:
            # curl_cffi impersonation supplies its own consistent UA.
            h["User-Agent"] = _USER_AGENT
            h["sec-ch-ua"] = '"Chromium";v="131", "Google Chrome";v="131"'
            h["sec-ch-ua-mobile"] = "?0"
            h["sec-ch-ua-platform"] = '"Windows"'
        return h

    def _raw_get(self, transport: str, url: str,
                 params: dict | None) -> _Response:
        started = time.monotonic()
        if transport == "chrome":
            # The request is made by the real browser itself, from inside a
            # wildberries.ru tab, so WB sees that browser's actual session;
            # nothing is copied out of it.
            payload = self._real_browser_session().fetch(url, params)
            headers = {str(k).lower(): str(v)
                       for k, v in dict(payload.get("headers") or {}).items()}
            return _Response(
                status_code=int(payload.get("status") or 0),
                text=str(payload.get("text") or ""), headers=headers,
                url=str(payload.get("url") or url), transport=transport,
                elapsed_ms=int((time.monotonic() - started) * 1000))
        if transport == "browser":
            from curl_cffi import requests as curl_requests
            if self._curl_session is None:
                self._curl_session = curl_requests.Session(
                    impersonate="chrome")
            r = self._curl_session.get(
                url, params=params, headers=self._headers(browser=True),
                timeout=self.timeout)
        else:
            if self._httpx_client is None:
                self._httpx_client = httpx.Client(
                    timeout=self.timeout, follow_redirects=True)
            r = self._httpx_client.get(
                url, params=params, headers=self._headers(browser=False))
        return _Response(
            status_code=int(r.status_code), text=r.text or "",
            headers={str(k).lower(): str(v) for k, v in dict(r.headers).items()},
            url=str(r.url), transport=transport,
            elapsed_ms=int((time.monotonic() - started) * 1000))

    def _transports_for(self, url: str) -> list[str]:
        host = urlparse(url).netloc.lower()
        gated = any(host == s or host.endswith("." + s) or host.endswith(s)
                    for s in _GATED_SUFFIXES)
        if self.transport == "httpx":
            return ["httpx"]
        if self.transport == "browser":
            return ["browser", "httpx"]
        if self.transport == "chrome":
            # Basket price-history is not behind the same gate and should
            # not force a browser launch; gated WB APIs go through the tab.
            return ["chrome", "httpx"] if gated else ["httpx"]
        # auto: plain first; impersonation next; the installed real browser
        # is the last resort for networks where both app transports get 403.
        transports = ["httpx"]
        if gated and curl_cffi_available():
            transports.append("browser")
        if gated and self.browser_fallback and chrome_available(
                self.browser_executable):
            transports.append("chrome")
        return transports

    def _request_json(self, url: str, params: dict | None,
                      attempts: list[dict]):
        """GET JSON trying transports. Returns (resp, payload, attempt).

        Non-final transport attempts are appended to ``attempts`` here;
        the caller enriches and appends the final attempt.
        """
        transports = self._transports_for(url)
        for idx, transport in enumerate(transports):
            started = time.monotonic()
            try:
                resp = self._raw_get(transport, url, params)
            except Exception as e:
                outcome = getattr(e, "outcome", "network_error") \
                    if isinstance(e, BrowserSessionError) else "network_error"
                attempts.append({
                    "endpoint": url, "transport": transport, "status": None,
                    "outcome": outcome, "error": str(e)[:350],
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                })
                # A connection-level failure will not heal by switching
                # httpx -> browser on the same host (the TLS gate answers
                # with HTTP 403, handled below). Browser-first settings may
                # still fall back to httpx if the browser itself is absent.
                if transport in ("browser", "chrome") and idx < len(transports) - 1:
                    continue
                return None, None, None
            payload, parse_failed = None, False
            if resp.status_code == 200:
                try:
                    payload = json.loads(resp.text)
                except Exception:
                    parse_failed = True
            outcome = _classify(resp, payload, parse_failed)
            attempt = {
                "endpoint": url, "transport": transport,
                "status": resp.status_code, "outcome": outcome,
                "server": resp.headers.get("server"),
                "content_type": resp.headers.get("content-type"),
                "elapsed_ms": resp.elapsed_ms,
            }
            if outcome in ("blocked", "bad_json") and idx < len(transports) - 1:
                attempts.append(attempt)
                continue
            return resp, payload, attempt
        return None, None, None

    def _dom_allowed(self) -> bool:
        return self.transport == "chrome" or (
            self.transport == "auto" and self.browser_fallback
            and chrome_available(self.browser_executable))

    def _search_dom(self, query: str, page: int, sort: str,
                    attempts: list[dict]) -> tuple[list[dict], bool]:
        """Last resort: read the search page WB rendered for the browser.

        Returns ``(products, recognized_empty)``.  This matters because on
        some networks WB refuses even API ``fetch()`` calls made from a tab,
        while still rendering the normal search grid for that same browser.
        """
        started = time.monotonic()
        endpoint = "https://www.wildberries.ru/catalog/0/search.aspx"
        try:
            payload = self._real_browser_session().search_dom_payload(
                query, page=page, sort=sort)
        except BrowserSessionError as e:
            attempts.append({
                "endpoint": endpoint, "transport": "chrome-dom",
                "status": None, "outcome": e.outcome,
                "error": str(e)[:350],
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            })
            return [], False
        except Exception as e:  # browser vanished mid-read etc.
            attempts.append({
                "endpoint": endpoint, "transport": "chrome-dom",
                "status": None, "outcome": "network_error",
                "error": str(e)[:350],
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            })
            return [], False

        items = payload.get("items") or []
        products = parse_dom_search_items(items)
        body = str(payload.get("bodyText") or "").lower()
        recognized_empty = not products and any(
            phrase in body for phrase in (
                "ничего не нашлось", "ничего не найдено",
                "по вашему запросу ничего", "товары не найдены"))
        attempts.append({
            "endpoint": str(payload.get("url") or endpoint),
            "transport": "chrome-dom", "status": 200,
            "outcome": "ok" if products else (
                "empty" if recognized_empty else "bad_shape"),
            "shape": "rendered-dom", "raw_products": len(items),
            "products": len(products),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        })
        return products, recognized_empty

    # -- search -------------------------------------------------------- #
    def search(self, query: str, page: int = 1,
               sort: str = "popular") -> list[dict]:
        params = {
            "ab_testing": "false", "appType": 1, "curr": "rub",
            "dest": self.dest, "lang": "ru", "locale": "ru", "page": page,
            "query": query, "resultset": "catalog", "sort": sort,
            "spp": self.spp, "suppressSpellcheck": "false",
        }
        attempts: list[dict] = []
        valid_empty = False
        if self._prefer_dom_search and self._dom_allowed():
            # A previous request on this client proved the API path is
            # refused while rendered pages work.  Do not pay the full
            # endpoint/transport matrix again for every query in a scan;
            # if DOM has stopped working, fall through and retest APIs.
            dom_products, dom_empty = self._search_dom(
                query, page, sort, attempts)
            if dom_products:
                self._dom_empty_streak = 0
                self.last_attempts = attempts
                self.last_endpoint = attempts[-1].get("endpoint")
                self.last_transport = "chrome-dom"
                return dom_products
            if dom_empty:
                self._dom_empty_streak = 0
                self.last_attempts = attempts
                return []
            # One flaky empty render (e.g. the WB tab was in the
            # background and its grid rendered slowly) must not throw
            # every following query back into the full endpoint/
            # transport matrix plus a second DOM wait — that multiplied
            # a single search to over a minute and made whole scans
            # look frozen.  Fail this query fast and only retest the
            # API matrix after several consecutive empty DOM reads.
            self._dom_empty_streak += 1
            if self._dom_empty_streak < 3:
                self.last_attempts = attempts
                raise WBUnavailable(
                    f"WB не отдал товары по запросу {query!r}: "
                    "отрисованная выдача пуста (возможно, вкладка WB в "
                    "фоне не успела отрисоваться): "
                    + _summarize_attempts(attempts), attempts=attempts)
            self._dom_empty_streak = 0
            self._prefer_dom_search = False
        order = (SEARCH_ENDPOINTS[self._search_idx:]
                 + SEARCH_ENDPOINTS[: self._search_idx])
        dead_hosts: set[str] = set()

        def mark_dead(endpoint_url: str) -> None:
            outcomes = [a.get("outcome") for a in attempts
                        if a.get("endpoint") == endpoint_url]
            if outcomes and all(o in ("blocked", "network_error")
                                for o in outcomes):
                dead_hosts.add(urlparse(endpoint_url).netloc.lower())

        for url in order:
            if urlparse(url).netloc.lower() in dead_hosts:
                continue  # other versions on a refused host won't heal it
            resp, payload, attempt = self._request_json(url, params, attempts)
            if resp is None or attempt is None:
                mark_dead(url)
                continue
            if attempt["outcome"] == "ok":
                raw_products, shape, total = extract_products(payload)
                attempt.update(shape=shape, total=total,
                               raw_products=(len(raw_products)
                                             if raw_products is not None else None))
                if raw_products is None:
                    attempt["outcome"] = "bad_shape"
                else:
                    products = [p for p in
                                (normalize_product(x) for x in raw_products) if p]
                    attempt["products"] = len(products)
                    if products:
                        attempts.append(attempt)
                        self._dom_empty_streak = 0
                        self._search_idx = SEARCH_ENDPOINTS.index(url)
                        self.last_attempts, self.last_endpoint = attempts, url
                        self.last_transport = attempt["transport"]
                        return products
                    if raw_products:
                        attempt["outcome"] = "bad_shape"  # unparsable prices
                    else:
                        attempt["outcome"] = "empty"
                        valid_empty = True
            attempts.append(attempt)
            mark_dead(url)
            if attempt["outcome"] == "rate_limited":
                time.sleep(2)
        if not valid_empty and self._dom_allowed():
            dom_products, dom_empty = self._search_dom(
                query, page, sort, attempts)
            if dom_products:
                self._prefer_dom_search = True
                self._dom_empty_streak = 0
                self.last_attempts = attempts
                self.last_endpoint = attempts[-1].get("endpoint")
                self.last_transport = "chrome-dom"
                return dom_products
            valid_empty = valid_empty or dom_empty
        self.last_attempts = attempts
        if valid_empty:
            return []  # WB answered properly; this query has no results.
        raise WBUnavailable(
            f"WB не отдал товары по запросу {query!r}: "
            + _summarize_attempts(attempts), attempts=attempts)

    # -- card detail --------------------------------------------------- #
    def card_detail(self, nms: list[int]) -> list[dict]:
        if not nms:
            return []
        params = {
            "appType": 1, "curr": "rub", "dest": self.dest, "lang": "ru",
            "locale": "ru", "spp": self.spp,
            "nm": ";".join(str(n) for n in nms),
        }
        attempts: list[dict] = []
        order = (CARD_ENDPOINTS[self._card_idx:]
                 + CARD_ENDPOINTS[: self._card_idx])
        for url in order:
            resp, payload, attempt = self._request_json(url, params, attempts)
            if resp is None or attempt is None:
                continue
            if attempt["outcome"] == "ok":
                raw_products, shape, _total = extract_products(payload)
                attempt.update(shape=shape)
                if raw_products:
                    products = [p for p in
                                (normalize_product(x) for x in raw_products) if p]
                    attempt["products"] = len(products)
                    attempts.append(attempt)
                    self._card_idx = CARD_ENDPOINTS.index(url)
                    self.last_attempts = attempts
                    return products
                attempt["outcome"] = "empty" if raw_products == [] else "bad_shape"
            attempts.append(attempt)
        self.last_attempts = attempts
        log.debug("card_detail failed: %s", _summarize_attempts(attempts))
        return []

    # -- price history ------------------------------------------------- #
    def price_history(self, nm: int) -> list[dict]:
        """Return [{'ts': int, 'price_rub': float}, ...] (may be empty)."""
        for url in price_history_urls(nm):
            attempts: list[dict] = []
            try:
                resp, payload, attempt = self._request_json(url, None, attempts)
            except Exception as e:
                log.debug("price_history %s failed: %s", nm, e)
                continue
            if resp is None or attempt is None or attempt["outcome"] != "ok":
                continue
            if not isinstance(payload, list):
                continue
            out = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                rub = (item.get("price") or {}).get("RUB") \
                    if isinstance(item.get("price"), dict) else None
                v = _to_int_price(rub)
                if v:
                    out.append({"ts": item.get("dt", 0), "price_rub": v / 100.0})
            if out:
                return out
        return []

    def history_median(self, nm: int) -> float | None:
        # The scanner asks this for every "likely" product on every
        # cycle; with WB's inflated sticker discounts that is hundreds
        # of identical basket fetches per cycle, which used to add
        # minutes before the next marketplace even started.  External
        # price history changes slowly — cache the median for a while.
        key = int(nm)
        now = time.time()
        cached = self._history_cache.get(key)
        if cached is not None and now - cached[0] < 6 * 3600:
            return cached[1]
        hist = self.price_history(nm)
        prices = [h["price_rub"] for h in hist if h["price_rub"] > 0]
        result = statistics.median(prices) if len(prices) >= 3 else None
        if len(self._history_cache) > 5000:
            self._history_cache.clear()
        self._history_cache[key] = (now, result)
        return result

    # -- diagnostics --------------------------------------------------- #
    def probe(self, query: str = "наушники") -> dict:
        """One search (+ card check on the first hit) with a verdict the
        UI can show verbatim. Never raises."""
        started = time.monotonic()
        real_browser = browser_info(self.browser_executable)
        base: dict[str, Any] = {
            "ok": False, "outcome": "unknown", "query": query,
            "transport_setting": self.transport,
            "browser_transport_available": curl_cffi_available(),
            "chrome_transport_available": real_browser["available"],
            "chrome_executable_found": real_browser["executable_found"],
            "chrome_executable": real_browser["executable"],
            "chrome_websocket_available": real_browser["websocket_available"],
            "chrome_fallback_enabled": self.browser_fallback,
            "chrome_port": self.browser_port,
            "dest": self.dest, "spp": self.spp, "products": 0,
            "sample": None, "card_ok": None, "attempts": [],
            "message": "", "hint": "",
        }
        try:
            products = self.search(query, page=1)
        except WBUnavailable as e:
            base.update(outcome=e.outcome, attempts=e.attempts,
                        message=str(e))
            base["hint"] = _hint_for(e.outcome, base)
            return base
        except Exception as e:
            base.update(outcome="network_error",
                        message=f"Ошибка запроса к WB: {e}",
                        attempts=list(self.last_attempts))
            base["hint"] = _hint_for("network_error", base)
            return base

        base["attempts"] = list(self.last_attempts)
        if not products:
            base.update(outcome="empty",
                        message="WB отвечает, но по тестовому запросу "
                                "товары не нашлись")
            base["hint"] = ("Проверь регион (dest) и запросы; открой тот же "
                            "поиск на wildberries.ru в браузере — есть ли там выдача.")
            return base

        p0 = products[0]
        base.update(
            ok=True, outcome="ok", products=len(products),
            endpoint=self.last_endpoint, transport=self.last_transport,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            sample=f"{p0.get('brand', '')} {p0.get('name', '')}"[:70].strip(),
            message=(f"WB доступен: {len(products)} товаров по запросу "
                     f"«{query}» (транспорт: {self.last_transport})"))
        details = self.card_detail([int(p0["nm"])])
        base["card_ok"] = bool(details)
        return base

    def ping(self) -> bool:
        return bool(self.probe()["ok"])

    def close(self) -> None:
        for client in (self._httpx_client, self._curl_session):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        self._httpx_client = self._curl_session = None
        if self._browser_session is not None:
            # Closing a temporary diagnostic client must not kill the
            # visible browser the user may still need for WB's check.
            self._browser_session.close()
            self._browser_session = None


def _hint_for(outcome: str, probe: dict) -> str:
    if outcome == "blocked":
        if not probe.get("chrome_transport_available"):
            if not probe.get("chrome_executable_found"):
                return ("WB режет оба транспортных клиента приложения, а Chrome/Edge "
                        "на этом ПК не найден. Установи Chrome или Edge либо укажи "
                        "полный путь к браузеру в настройках, затем выбери транспорт "
                        "WB «chrome» и повтори проверку.")
            if not probe.get("browser_transport_available"):
                return ("WB режет обычный HTTP-клиент по TLS-отпечатку, а "
                        "браузерный транспорт (curl_cffi) в этой сборке недоступен. "
                        "Обнови приложение/сборку или запускай из исходников после "
                        "pip install curl_cffi websocket-client.")
            return ("Настоящий браузер найден, но CDP-транспорт недоступен в сборке. "
                    "Обнови приложение; из исходников нужен websocket-client.")
        tried_dom = any(a.get("transport") == "chrome-dom"
                        for a in probe.get("attempts") or [])
        tried_chrome = any(str(a.get("transport", "")).startswith("chrome")
                           for a in probe.get("attempts") or [])
        if tried_chrome or tried_dom:
            return ("WB не пропустил даже настоящий браузер на этом проходе. "
                    "Оставь ОТКРЫТЫМ отдельное окно Chrome/Edge, которое открыл "
                    "Price Hunter, дождись/пройди проверку WB в нём, затем нажми "
                    "диагностику ещё раз. Это отдельный профиль Price Hunter: "
                    "проверка в твоём обычном окне Chrome его не прогревает. "
                    "Окно нельзя закрывать во время охоты; куки никуда не отправляются.")
        return ("WB режет httpx и curl_cffi, но на ПК найден настоящий Chrome/Edge. "
                "В Настройках выбери «Транспорт WB → chrome», сохрани и снова нажми "
                "«Проверить WB»: приложение откроет отдельный браузерный профиль. "
                "Если WB покажет проверку — пройди её в этом окне и повтори.")
    if outcome == "rate_limited":
        return ("Слишком частые запросы. Увеличь интервал сканирования и "
                "паузу между запросами, уменьши число запросов/страниц.")
    if outcome == "network_error":
        return ("Проверь интернет, DNS, VPN/прокси и что wildberries.ru "
                "открывается в браузере с этого же ПК.")
    if outcome in ("bad_shape", "bad_json"):
        return ("WB, похоже, снова поменял формат ответа. Сохрани результат "
                "диагностики (endpoint/HTTP/shape) — по нему чинится клиент.")
    return _OUTCOME_TEXT.get(outcome, "")
