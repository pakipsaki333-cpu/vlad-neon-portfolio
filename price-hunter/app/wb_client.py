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
    transport: str = "auto"      # auto | httpx | browser (curl_cffi)
    _httpx_client: Any = field(default=None, repr=False)
    _curl_session: Any = field(default=None, repr=False)
    _search_idx: int = field(default=0, repr=False)
    _card_idx: int = field(default=0, repr=False)
    last_attempts: list[dict] = field(default_factory=list, repr=False)
    last_endpoint: str | None = field(default=None, repr=False)
    last_transport: str | None = field(default=None, repr=False)

    def __post_init__(self):
        t = str(self.transport or "auto").lower()
        self.transport = "browser" if t == "curl_cffi" else (
            t if t in ("auto", "httpx", "browser") else "auto")

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
        # auto: plain first; browser impersonation only for gated hosts
        # and only if available (a 403 there is deterministic, not luck).
        if gated and curl_cffi_available():
            return ["httpx", "browser"]
        return ["httpx"]

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
                attempts.append({
                    "endpoint": url, "transport": transport, "status": None,
                    "outcome": "network_error", "error": str(e)[:250],
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                })
                # A connection-level failure will not heal by switching
                # httpx -> browser on the same host (the TLS gate answers
                # with HTTP 403, handled below). Only an explicitly chosen
                # browser transport falls back to httpx here.
                if transport == "browser" and idx < len(transports) - 1:
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
        order = (SEARCH_ENDPOINTS[self._search_idx:]
                 + SEARCH_ENDPOINTS[: self._search_idx])
        valid_empty = False
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
        hist = self.price_history(nm)
        prices = [h["price_rub"] for h in hist if h["price_rub"] > 0]
        return statistics.median(prices) if len(prices) >= 3 else None

    # -- diagnostics --------------------------------------------------- #
    def probe(self, query: str = "наушники") -> dict:
        """One search (+ card check on the first hit) with a verdict the
        UI can show verbatim. Never raises."""
        started = time.monotonic()
        base: dict[str, Any] = {
            "ok": False, "outcome": "unknown", "query": query,
            "transport_setting": self.transport,
            "browser_transport_available": curl_cffi_available(),
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


def _hint_for(outcome: str, probe: dict) -> str:
    if outcome == "blocked":
        if not probe.get("browser_transport_available"):
            return ("WB режет обычный HTTP-клиент по TLS-отпечатку, а "
                    "браузерный транспорт (curl_cffi) в этой сборке недоступен. "
                    "Обнови приложение/сборку с curl_cffi или запускай из "
                    "исходников после pip install curl_cffi.")
        return ("WB показывает антибот даже браузерному транспорту. Отключи "
                "VPN/прокси, попробуй другую сеть (домашний интернет/мобильная "
                "раздача), открой wildberries.ru в обычном браузере и пройди "
                "проверку, если WB её покажет, затем повтори диагностику.")
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
