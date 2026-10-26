"""Приведение разных API пулов к одному виду для дашборда.

Поддерживаются два формата (см. pools.py). Всё считается в piconero внутри,
наружу отдаём XMR (float) — удобно для UI, но для денежных расчётов в пуле
используются целые числа, тут мы ничего не округляем «в свою пользу».
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .pools import PICONERO, get

BLOCK_TIME_SECONDS = 120  # Monero: цель — 2 минуты на блок


def _num(value: Any, default: float = 0.0) -> float:
    """API пулов иногда возвращает false вместо 0."""
    if value is None or value is False or value is True:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _xmr(value: Any) -> float:
    return _num(value) / PICONERO


def normalize_payments(pool_id: str, raw: Any) -> List[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        txid = item.get("txnHash") or item.get("txn_hash") or item.get("hash") or ""
        out.append({
            "ts": int(_num(item.get("ts") or item.get("timestamp"))),
            "amount_xmr": _xmr(item.get("amount")),
            "txid": str(txid),
        })
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:25]


def normalize_view(
    pool_id: str,
    stats: Optional[dict] = None,
    payments: Optional[Any] = None,
    pool_stats: Optional[dict] = None,
    *,
    demo: bool = False,
    errors: Optional[List[str]] = None,
) -> dict:
    """Собирает единый объект статистики.

    `stats`, `payments`, `pool_stats` — сырые JSON-ответы пула. Отсутствующие
    части просто дают нули, дашборд при этом не падает.
    """
    pool = get(pool_id)
    stats = stats if isinstance(stats, dict) else {}
    pool_stats = pool_stats if isinstance(pool_stats, dict) else {}

    view: Dict[str, Any] = {
        "pool": pool_id,
        "pool_name": pool.name,
        "fee_pct": pool.fee_pct,
        # nodejs_pool не отдаёт персональный порог в API — берём минимально
        # доступный как «до первой капли». Автопорог показываем отдельно.
        "threshold_xmr": pool.min_payout_xmr,
        "auto_threshold_xmr": pool.default_payout_xmr,
        "min_payout_xmr": pool.min_payout_xmr,
        "hashrate_hs": 0.0,
        "hashrate_24h": None,
        "balance_xmr": 0.0,
        "unconfirmed_xmr": 0.0,
        "paid_xmr": 0.0,
        "valid_shares": 0,
        "invalid_shares": 0,
        "total_hashes": 0.0,
        "last_share_ts": None,
        "workers": None,
        "payments": normalize_payments(pool_id, payments),
        "pool_hashrate_hs": None,
        "network_hashrate_hs": None,
        "block_reward_xmr": None,
        "pool_miners": None,
        "price_usd": None,
        "price_eur": None,
        "price_rub": None,
        "updated_at": int(time.time()),
        "demo": demo,
        "errors": list(errors or []),
    }

    if pool.api_kind == "nodejs_pool":
        view.update({
            "hashrate_hs": _num(stats.get("hash")),
            "balance_xmr": _xmr(stats.get("amtDue")),
            "paid_xmr": _xmr(stats.get("amtPaid")),
            "valid_shares": int(_num(stats.get("validShares"))),
            "invalid_shares": int(_num(stats.get("invalidShares"))),
            "total_hashes": _num(stats.get("totalHashes")),
            "last_share_ts": int(_num(stats.get("lastHash"))) or None,
            "txn_count": int(_num(stats.get("txnCount"))),
        })
        ps = pool_stats.get("pool_statistics", {}) if isinstance(pool_stats.get("pool_statistics"), dict) else {}
        view["pool_hashrate_hs"] = _num(ps.get("hashRate")) or None
        view["pool_miners"] = int(_num(ps.get("miners"))) or None
        price = pool_stats.get("price")
        if isinstance(price, dict):
            view["price_usd"] = _num(price.get("usd")) or None
            view["price_eur"] = _num(price.get("eur")) or None
    else:  # hashvault
        collective = stats.get("collective", {}) if isinstance(stats.get("collective"), dict) else {}
        revenue = stats.get("revenue", {}) if isinstance(stats.get("revenue"), dict) else {}
        view.update({
            "hashrate_hs": _num(collective.get("hashRate")),
            "hashrate_24h": _num(collective.get("avg24hashRate")) or None,
            "balance_xmr": _xmr(revenue.get("confirmedBalance")),
            "paid_xmr": _xmr(revenue.get("totalPaid")),
            "valid_shares": int(_num(collective.get("validShares"))),
            "invalid_shares": int(_num(collective.get("invalidShares"))),
            "total_hashes": _num(collective.get("totalHashes")),
            "workers": None,  # v3 wallet/stats не отдаёт список воркеров
            "txn_count": int(_num(revenue.get("totalPaymentsSent"))),
        })
        unconfirmed = revenue.get("unconfirmedBalance", {})
        if isinstance(unconfirmed, dict):
            view["unconfirmed_xmr"] = sum(
                _xmr((part or {}).get("total"))
                for part in unconfirmed.values()
                if isinstance(part, dict)
            )
        last_share_ms = _num(collective.get("lastShare"))
        view["last_share_ts"] = int(last_share_ms / 1000) or None
        if revenue.get("payoutThreshold"):
            view["threshold_xmr"] = _xmr(revenue.get("payoutThreshold"))
        ps = pool_stats.get("pool_statistics", {})
        collective_pool = ps.get("collective", {}) if isinstance(ps, dict) else {}
        view["pool_hashrate_hs"] = _num(collective_pool.get("hashRate")) or None
        view["pool_miners"] = int(_num(collective_pool.get("miners"))) or None
        network = pool_stats.get("network_statistics", {}) if isinstance(pool_stats, dict) else {}
        diff = _num(network.get("difficulty"))
        if diff:
            view["network_hashrate_hs"] = diff / BLOCK_TIME_SECONDS
        view["block_reward_xmr"] = _xmr(network.get("value")) or None
        market = pool_stats.get("market", {}) if isinstance(pool_stats, dict) else {}
        view["price_usd"] = _num(market.get("price_usd")) or None
        view["price_eur"] = _num(market.get("price_eur")) or None
        view["price_rub"] = _num(market.get("price_rub")) or None

    view["total_xmr"] = round(view["balance_xmr"] + view["paid_xmr"], 12)
    threshold = max(view["threshold_xmr"], 1e-9)
    view["progress_pct"] = min(100.0, round(100.0 * view["balance_xmr"] / threshold, 2))
    return view
