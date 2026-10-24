"""Anomaly detector: is this price a real error / dump, or WB marketing?

Signals, strongest first:
  1. Drop vs product's own price history median (ours + WB price-history).
     A self-buyout dump or a fat-finger price shows up here even when the
     sticker discount looks normal.
  2. Sticker discount (priceU -> salePriceU). Necessary but NOT sufficient:
     WB sellers routinely inflate priceU to fake 80-90% discounts.
  3. Absolute saving in roubles - a 90% drop on a 300 RUB trinket is noise.
  4. Social proof (feedbacks/rating) - real sellable goods over junk listings.

Score 0..100 -> levels: FIR3 (>=70) / HOT (>=50) / WATCH (>=35).
"""
from __future__ import annotations

from typing import Any


def evaluate(p: dict[str, Any], own_stats: dict, wb_hist_median: float | None,
             cfg: dict) -> dict | None:
    price, sale = p["price_rub"], p["sale_price_rub"]
    if sale <= 0 or price < cfg["min_original_price_rub"]:
        return None
    discount = (price - sale) / price * 100 if price > sale else 0.0
    saving = price - sale

    # Best available "normal price" reference.
    ref_median = None
    if own_stats.get("count", 0) >= 3 and own_stats.get("median"):
        ref_median = own_stats["median"]
    if wb_hist_median:
        # WB price-history stores pre-SPP prices, typically >= sale price,
        # so it is a conservative (high) reference - good for confirmation.
        ref_median = max(ref_median or 0, wb_hist_median) or wb_hist_median

    # Signed on purpose: <= 0 means "today's price is the everyday price",
    # which is exactly how fake WB sticker discounts get caught below.
    drop_vs_median = None
    if ref_median:
        drop_vs_median = (ref_median - sale) / ref_median * 100

    reasons: list[str] = []
    score = 0

    if discount >= 90:
        score += 45; reasons.append(f"скидка по карточке −{discount:.0f}%")
    elif discount >= 80:
        score += 38; reasons.append(f"скидка по карточке −{discount:.0f}%")
    elif discount >= 70:
        score += 28; reasons.append(f"скидка по карточке −{discount:.0f}%")
    elif discount >= cfg["min_discount_pct"]:
        score += 18; reasons.append(f"скидка по карточке −{discount:.0f}%")

    if drop_vs_median is not None:
        if drop_vs_median >= 60:
            score += 40; reasons.append(
                f"на {drop_vs_median:.0f}% ниже медианы истории ({ref_median:,.0f} ₽)")
        elif drop_vs_median >= 40:
            score += 30; reasons.append(
                f"на {drop_vs_median:.0f}% ниже медианы истории ({ref_median:,.0f} ₽)")
        elif drop_vs_median >= cfg["min_drop_vs_history_pct"]:
            score += 18; reasons.append(
                f"на {drop_vs_median:.0f}% ниже медианы истории ({ref_median:,.0f} ₽)")
        elif drop_vs_median < 10 and discount >= 70:
            # History says this 'discount' is the everyday price -> fake.
            score -= 25; reasons.append("⚠ история: «скидка» похожа на обычную цену")

    if saving >= 20_000:
        score += 12; reasons.append(f"выгода {saving:,.0f} ₽")
    elif saving >= 5_000:
        score += 8; reasons.append(f"выгода {saving:,.0f} ₽")
    elif saving >= cfg["min_saving_rub"]:
        score += 4

    fb = p.get("feedbacks", 0)
    if fb >= 100:
        score += 6; reasons.append(f"живой товар: {fb} отзывов")
    elif fb >= 20:
        score += 3
    elif fb == 0:
        score -= 8; reasons.append("нет отзывов — проверяй продавца")

    score = max(0, min(100, score))

    is_candidate = (
        discount >= cfg["min_discount_pct"]
        or (drop_vs_median is not None
            and drop_vs_median >= cfg["min_drop_vs_history_pct"])
    )
    if not is_candidate or saving < cfg["min_saving_rub"] or score < cfg["min_score"]:
        return None

    level = "FIRE" if score >= 70 else "HOT" if score >= 50 else "WATCH"
    return {
        **p,
        "discount_pct": round(discount, 1),
        "drop_vs_median_pct": round(drop_vs_median, 1)
        if drop_vs_median and drop_vs_median > 0 else None,
        "hist_median": round(ref_median, 2) if ref_median else None,
        "saving": round(saving, 2),
        "score": score,
        "level": level,
        "reasons": reasons,
    }
