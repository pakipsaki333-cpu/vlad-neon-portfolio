"""Wildberries public API client.

Uses the same unauthenticated endpoints the WB website itself calls:
  * search.wb.ru  — catalog search (several API versions as fallbacks,
    WB rotates them regularly)
  * card.wb.ru    — product card detail by article (nm)
  * basket-*.wbbasket.ru — price history JSON

No account, token or payment data is involved. The client only READS
public prices. Respect rate limits: keep intervals sane (>= 60s per
full scan) so WB does not throttle your IP.
"""
from __future__ import annotations

import logging
import random
import statistics
import time
from typing import Any

import httpx

from .basket import photo_url, price_history_url, product_url

log = logging.getLogger("wb")

SEARCH_ENDPOINTS = [
    "https://search.wb.ru/exactmatch/ru/common/v9/search",
    "https://search.wb.ru/exactmatch/ru/common/v5/search",
    "https://search.wb.ru/exactmatch/ru/common/v4/search",
]
CARD_ENDPOINTS = [
    "https://card.wb.ru/cards/v2/detail",
    "https://card.wb.ru/cards/v1/detail",
]

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) "
    "Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]


class WBUnavailable(RuntimeError):
    """Raised when every WB endpoint failed (network blocked / throttled)."""


def normalize_product(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a raw WB product into our internal schema. Prices in RUB."""
    nm = raw.get("id") or raw.get("nmID") or raw.get("nmId")
    if not nm:
        return None
    # v9 nests sizes with per-size price; older versions have flat fields.
    price_u = raw.get("priceU")
    sale_u = raw.get("salePriceU")
    if sale_u is None and raw.get("sizes"):
        for size in raw["sizes"]:
            p = size.get("price") or {}
            if p.get("product"):
                sale_u = p["product"]
                price_u = p.get("basic", price_u)
                break
    if not sale_u:
        return None
    price_rub = (price_u or sale_u) / 100.0
    sale_rub = sale_u / 100.0
    return {
        "nm": int(nm),
        "name": (raw.get("name") or "").strip(),
        "brand": (raw.get("brand") or "").strip(),
        "supplier": (raw.get("supplier") or "").strip(),
        "supplier_id": raw.get("supplierId"),
        "price_rub": round(price_rub, 2),          # price before discount
        "sale_price_rub": round(sale_rub, 2),      # final price to pay
        "rating": raw.get("reviewRating") or raw.get("rating") or 0,
        "feedbacks": raw.get("feedbacks") or raw.get("nmFeedbacks") or 0,
        "photo": photo_url(int(nm)),
        "url": product_url(int(nm)),
    }


class WBClient:
    def __init__(self, dest: int = -1257786, spp: int = 30, timeout: float = 15.0):
        self.dest = dest
        self.spp = spp
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)
        self._search_idx = 0  # remember which search version last worked

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://www.wildberries.ru",
            "Referer": "https://www.wildberries.ru/",
        }

    # ------------------------------------------------------------------ #
    def search(self, query: str, page: int = 1, sort: str = "popular") -> list[dict]:
        params = {
            "appType": 1, "curr": "rub", "dest": self.dest, "lang": "ru",
            "page": page, "query": query, "resultset": "catalog",
            "sort": sort, "spp": self.spp, "suppressSpellcheck": "false",
        }
        last_err: Exception | None = None
        order = SEARCH_ENDPOINTS[self._search_idx:] + SEARCH_ENDPOINTS[: self._search_idx]
        for url in order:
            try:
                r = self._client.get(url, params=params, headers=self._headers())
                if r.status_code == 200:
                    self._search_idx = SEARCH_ENDPOINTS.index(url)
                    products = (r.json().get("data") or {}).get("products") or []
                    return [p for p in (normalize_product(x) for x in products) if p]
                last_err = WBUnavailable(f"{url} -> HTTP {r.status_code}")
                if r.status_code == 429:
                    time.sleep(3)
            except Exception as e:  # network / json errors -> try next version
                last_err = e
        raise WBUnavailable(f"WB search unavailable for {query!r}: {last_err}")

    # ------------------------------------------------------------------ #
    def card_detail(self, nms: list[int]) -> list[dict]:
        params = {
            "appType": 1, "curr": "rub", "dest": self.dest, "spp": self.spp,
            "nm": ";".join(str(n) for n in nms),
        }
        for url in CARD_ENDPOINTS:
            try:
                r = self._client.get(url, params=params, headers=self._headers())
                if r.status_code == 200:
                    products = (r.json().get("data") or {}).get("products") or []
                    return [p for p in (normalize_product(x) for x in products) if p]
            except Exception as e:
                log.debug("card_detail %s failed: %s", url, e)
        return []

    # ------------------------------------------------------------------ #
    def price_history(self, nm: int) -> list[dict]:
        """Return [{'ts': int, 'price_rub': float}, ...] (may be empty)."""
        try:
            r = self._client.get(price_history_url(nm), headers=self._headers())
            if r.status_code != 200:
                return []
            out = []
            for item in r.json() or []:
                rub = (item.get("price") or {}).get("RUB")
                if rub:
                    out.append({"ts": item.get("dt", 0), "price_rub": rub / 100.0})
            return out
        except Exception as e:
            log.debug("price_history %s failed: %s", nm, e)
            return []

    def history_median(self, nm: int) -> float | None:
        hist = self.price_history(nm)
        prices = [h["price_rub"] for h in hist if h["price_rub"] > 0]
        return statistics.median(prices) if len(prices) >= 3 else None

    # ------------------------------------------------------------------ #
    def ping(self) -> bool:
        try:
            return len(self.search("наушники", page=1)) > 0
        except Exception:
            return False
