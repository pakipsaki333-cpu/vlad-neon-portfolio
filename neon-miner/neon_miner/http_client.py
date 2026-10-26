"""HTTP-клиент на стандартной библиотеке + демо-режим.

Запросы идут ТОЛЬКО на публичные read-only API пулов из pools.py — список
жёстко зашит, произвольные URL с фронтенда не принимаются (никакого SSRF).
Если сеть недоступна (песочница, офлайн), отдаём честно помеченные демо-данные.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .adapters import normalize_view
from .pools import POOLS, get

FIXTURES = Path(__file__).parent / "fixtures"
DEFAULT_TIMEOUT = 6.0
CACHE_TTL = 45.0
USER_AGENT = "NeonMiner/1.0 (local mining dashboard; +https://github.com/xmrig/xmrig)"

_CACHE: Dict[str, Tuple[float, Any]] = {}
_LOCK = threading.Lock()


def fetch_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> Tuple[Optional[Any], Optional[str]]:
    """Возвращает (данные, ошибка). url берётся только из каталога пулов."""
    allowed = any(url.startswith(pool.api_base) for pool in POOLS.values())
    if not allowed:
        return None, "URL не входит в белый список пулов"
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(url)
        if cached and now - cached[0] < CACHE_TTL:
            return cached[1], None

    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            payload = response.read(4_000_000)
            data = json.loads(payload.decode("utf-8", errors="replace"))
        with _LOCK:
            _CACHE[url] = (now, data)
        return data, None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            TimeoutError, ValueError, ssl.SSLError) as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:160]}"


def load_fixture(pool_id: str, kind: str) -> Any:
    path = FIXTURES / f"{pool_id}_{kind}.json"
    if not path.exists():
        return {} if kind != "payments" else []
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def demo_view(pool_id: str) -> dict:
    """Синтетические данные для демонстрации интерфейса. Всегда помечены demo=True."""
    view = normalize_view(
        pool_id,
        stats=load_fixture(pool_id, "stats"),
        payments=load_fixture(pool_id, "payments"),
        pool_stats=load_fixture(pool_id, "pool"),
        demo=True,
    )
    if not view.get("price_eur"):
        view["price_eur"] = 530.0
    if not view.get("price_usd"):
        view["price_usd"] = 594.0
    if not view.get("network_hashrate_hs"):
        view["network_hashrate_hs"] = 6.84e9
    if not view.get("block_reward_xmr"):
        view["block_reward_xmr"] = 0.6035
    view["demo_note"] = (
        "Демо-режим: цифры вымышлены. Вставь свой XMR-адрес — и дашборд "
        "подтянет реальную статистику напрямую из пула."
    )
    return view


def live_view(pool_id: str, wallet: str) -> Tuple[Optional[dict], list]:
    pool = get(pool_id)
    errors: list = []
    stats, err = fetch_json(pool.url_stats(wallet))
    if err:
        errors.append(f"stats: {err}")
    payments, err = fetch_json(pool.url_payments(wallet))
    if err:
        errors.append(f"payments: {err}")
    pool_stats, err = fetch_json(pool.url_pool())
    if err:
        errors.append(f"pool: {err}")

    if stats is None and pool_stats is None:
        return None, errors
    return normalize_view(pool_id, stats, payments, pool_stats, demo=False, errors=errors), errors


def enrich_with_market(view: dict) -> dict:
    """Цену XMR и сложность сети подтягиваем из HashVault (там есть EUR/RUB)."""
    market_pool = POOLS["hashvault"]
    data, err = fetch_json(market_pool.url_pool())
    if err or not isinstance(data, dict):
        return view
    market = data.get("market", {}) or {}
    network = data.get("network_statistics", {}) or {}
    view["price_eur"] = market.get("price_eur") or view.get("price_eur")
    view["price_usd"] = market.get("price_usd") or view.get("price_usd")
    view["price_rub"] = market.get("price_rub") or view.get("price_rub")
    if network.get("difficulty"):
        view["network_hashrate_hs"] = float(network["difficulty"]) / 120.0
    if network.get("value"):
        view["block_reward_xmr"] = float(network["value"]) / 1e12
    return view


def get_view(pool_id: str, wallet: str, *, force_demo: bool = False) -> dict:
    if force_demo or not wallet:
        return demo_view(pool_id)
    view, _ = live_view(pool_id, wallet)
    if view is None:
        base = demo_view(pool_id)
        base["demo_note"] = (
            "Нет связи с API пула (сеть/файрвол). Показаны демо-данные. "
            "Запусти клиент на своей машине или нажми «Обновить»."
        )
        return base
    return enrich_with_market(view)
