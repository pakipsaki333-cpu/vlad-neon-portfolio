"""Честный калькулятор доходности.

Формула простая и не обещает чудес:
    доля_сети = твой_хэшрейт / хэшрейт_сети
    XMR/сутки = доля_сети * 720 блоков * награда_за_блок * (1 - комиссия_пула)

Дальше вычитаем электричество. В Германии CPU-майнинг почти всегда в минус —
калькулятор это показывает, а не прячет.
"""

from __future__ import annotations

from typing import Optional

BLOCKS_PER_DAY = 720           # 24*60/2, цель Monero — блок раз в 2 минуты
DEFAULT_NETWORK_HASHRATE_HS = 6.84e9   # difficulty/120 на 26.10.2026 (~820.6 G)
DEFAULT_BLOCK_REWARD_XMR = 0.6035     # с учётом tail emission + комиссий
DEFAULT_PRICE_EUR = 530.0
DEFAULT_EUR_PER_KWH = 0.36     # грубая розничная цена DE, 2026 — поменяй на свою


def estimate(
    hashrate_hs: float,
    *,
    network_hashrate_hs: Optional[float] = None,
    block_reward_xmr: Optional[float] = None,
    fee_pct: float = 0.0,
    threshold_xmr: float = 0.001,
    price_eur: Optional[float] = None,
    power_watts: float = 65.0,
    eur_per_kwh: Optional[float] = None,
) -> dict:
    network = network_hashrate_hs or DEFAULT_NETWORK_HASHRATE_HS
    reward = block_reward_xmr or DEFAULT_BLOCK_REWARD_XMR
    price = price_eur if price_eur and price_eur > 0 else DEFAULT_PRICE_EUR
    kwh_price = DEFAULT_EUR_PER_KWH if eur_per_kwh is None else eur_per_kwh

    share = max(0.0, hashrate_hs) / max(network, 1.0)
    gross_xmr = share * BLOCKS_PER_DAY * reward
    net_xmr = gross_xmr * (1.0 - max(0.0, fee_pct) / 100.0)
    gross_eur = gross_xmr * price
    revenue_eur = net_xmr * price
    power_kwh_day = max(0.0, power_watts) * 24.0 / 1000.0
    power_cost_eur = power_kwh_day * kwh_price
    net_eur = revenue_eur - power_cost_eur

    days_to_payout = (threshold_xmr / net_xmr) if net_xmr > 0 else None

    return {
        "hashrate_hs": max(0.0, hashrate_hs),
        "network_hashrate_hs": network,
        "network_share": share,
        "block_reward_xmr": reward,
        "fee_pct": fee_pct,
        "gross_xmr_per_day": gross_xmr,
        "xmr_per_day": net_xmr,
        "gross_eur_per_day": gross_eur,
        "eur_per_day": revenue_eur,
        "eur_per_month": revenue_eur * 30.0,
        "power_kwh_per_day": power_kwh_day,
        "power_cost_eur_per_day": power_cost_eur,
        "net_eur_per_day": net_eur,
        "net_eur_per_month": net_eur * 30.0,
        "price_eur": price,
        "eur_per_kwh": kwh_price,
        "power_watts": power_watts,
        "days_to_payout": days_to_payout,
        "threshold_xmr": threshold_xmr,
        "profitable": net_eur > 0,
    }


def network_hashrate_from_difficulty(difficulty: float) -> float:
    return difficulty / 120.0
