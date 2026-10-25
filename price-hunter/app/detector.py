"""Anomaly detector: is this price a real error / dump, or WB marketing?

Default mode is history-only:

* an alert requires a real drop from the product's price-history median
  (Price Hunter's own snapshots first; WB's external price-history only
  while our own history is still too short);
* the seller's sticker discount is kept in the alert as evidence for the
  user, but it neither triggers an alert nor contributes to the score;
* the minimum-price and minimum-saving gates also use the history
  median, not the potentially inflated "before" price in the card;
* the lowest previously observed marketplace price is carried as
  evidence: breaking that historical low (or sitting right on it)
  strengthens the history-only score.

Setting ``history_only=False`` restores the legacy mixed detector, in
which a very large sticker discount can alert without a history
reference.  That mode is intentionally noisier: marketplaces routinely
inflate the crossed-out price to fake 80-90% discounts.

Other signals in both modes are the absolute saving in roubles (a huge
percentage on a trinket is noise) and social proof (feedbacks/rating).

Score 0..100 -> levels: FIRE (>=70) / HOT (>=50) / WATCH (>=35).
"""
from __future__ import annotations

from typing import Any


def evaluate(p: dict[str, Any], own_stats: dict, wb_hist_median: float | None,
             cfg: dict, wb_hist_min: float | None = None) -> dict | None:
    price = float(p["price_rub"] or 0)
    sale = float(p["sale_price_rub"] or 0)
    if sale <= 0:
        return None

    history_only = bool(cfg.get("history_only", True))
    if not history_only and price < cfg["min_original_price_rub"]:
        return None

    discount = (price - sale) / price * 100 if price > sale else 0.0
    sticker_saving = price - sale

    own_count = int(own_stats.get("count", 0) or 0)
    own_median = own_stats.get("median") \
        if own_count >= 3 and own_stats.get("median") else None
    own_min = own_stats.get("min") \
        if own_count >= 3 and own_stats.get("min") else None
    external_median = float(wb_hist_median) if wb_hist_median else None
    external_min = float(wb_hist_min) if wb_hist_min else None

    # Best available "normal price" reference.  In history-only mode our
    # own snapshots win because they were observed in exactly the same
    # sale-price terms as today's price.  External WB history is a useful
    # bootstrap for newly seen WB products, but can be on a pre-SPP basis.
    ref_median: float | None
    history_source: str | None
    if history_only:
        if own_median:
            ref_median, history_source = float(own_median), "own"
        elif external_median:
            ref_median, history_source = external_median, "external"
        else:
            ref_median, history_source = None, None
        if ref_median is None or ref_median < cfg["min_original_price_rub"]:
            return None
    else:
        ref_median = float(own_median) if own_median else None
        history_source = "own" if own_median else None
        if external_median:
            ref_median = max(ref_median or 0, external_median) or external_median
            history_source = "own+external" if own_median else "external"

    # Signed on purpose: <= 0 means "today's price is the everyday price",
    # which is exactly how fake sticker discounts are caught below.
    drop_vs_median = None
    if ref_median:
        drop_vs_median = (ref_median - sale) / ref_median * 100

    # Lowest price previously known for this same product on this same
    # marketplace.  In history-only mode follow the selected history
    # source; in legacy mode the lowest known value from either history
    # is the marketplace floor.  Positive vs_hist_min means today's price
    # is below that previous low; negative means it is above it.
    if history_only:
        ref_min = float(own_min) if history_source == "own" and own_min \
            else external_min if history_source == "external" else None
    else:
        known_mins = [float(v) for v in (own_min, external_min) if v]
        ref_min = min(known_mins) if known_mins else None
    vs_hist_min = None
    if ref_min:
        vs_hist_min = (ref_min - sale) / ref_min * 100
    is_new_history_min = bool(vs_hist_min is not None and vs_hist_min > 0)

    saving = (ref_median - sale) if history_only and ref_median \
        else sticker_saving
    reasons: list[str] = []
    score = 0

    if history_only:
        if drop_vs_median is not None and \
                drop_vs_median >= cfg["min_drop_vs_history_pct"]:
            # The percentage itself is the main score component (capped),
            # instead of arbitrary points for a seller-controlled sticker.
            score += min(70, int(round(drop_vs_median)))
            if history_source == "own":
                source_text = (
                    f"медианы собственной истории ({own_count} наблюдений)")
            else:
                source_text = "медианы внешней истории WB"
            reasons.append(
                f"цена на {drop_vs_median:.0f}% ниже {source_text}: "
                f"обычно {ref_median:,.0f} ₽")
        if ref_min and vs_hist_min is not None:
            if vs_hist_min > 0:
                # Breaking the previously observed marketplace low is a
                # strong confirmation, with a deliberately modest cap.
                score += min(12, max(4, int(round(vs_hist_min))))
                reasons.append(
                    f"новый исторический минимум на площадке: ниже прежнего "
                    f"минимума {ref_min:,.0f} ₽ на {vs_hist_min:.0f}%")
            elif vs_hist_min >= -3:
                score += 4
                reasons.append(
                    f"цена у исторического минимума площадки "
                    f"{ref_min:,.0f} ₽")
        if saving >= 20_000:
            score += 15
            reasons.append(f"выгода vs истории {saving:,.0f} ₽")
        elif saving >= 5_000:
            score += 10
            reasons.append(f"выгода vs истории {saving:,.0f} ₽")
        elif saving >= cfg["min_saving_rub"]:
            score += 5
    else:
        if discount >= 90:
            score += 45
            reasons.append(f"скидка по карточке −{discount:.0f}%")
        elif discount >= 80:
            score += 38
            reasons.append(f"скидка по карточке −{discount:.0f}%")
        elif discount >= 70:
            score += 28
            reasons.append(f"скидка по карточке −{discount:.0f}%")
        elif discount >= cfg["min_discount_pct"]:
            score += 18
            reasons.append(f"скидка по карточке −{discount:.0f}%")

        if drop_vs_median is not None:
            if drop_vs_median >= 60:
                score += 40
                reasons.append(
                    f"на {drop_vs_median:.0f}% ниже медианы истории "
                    f"({ref_median:,.0f} ₽)")
            elif drop_vs_median >= 40:
                score += 30
                reasons.append(
                    f"на {drop_vs_median:.0f}% ниже медианы истории "
                    f"({ref_median:,.0f} ₽)")
            elif drop_vs_median >= cfg["min_drop_vs_history_pct"]:
                score += 18
                reasons.append(
                    f"на {drop_vs_median:.0f}% ниже медианы истории "
                    f"({ref_median:,.0f} ₽)")
            elif drop_vs_median < 10 and discount >= 70:
                # History says this 'discount' is the everyday price.
                score -= 25
                reasons.append("⚠ история: «скидка» похожа на обычную цену")

        if saving >= 20_000:
            score += 12
            reasons.append(f"выгода {saving:,.0f} ₽")
        elif saving >= 5_000:
            score += 8
            reasons.append(f"выгода {saving:,.0f} ₽")
        elif saving >= cfg["min_saving_rub"]:
            score += 4

    fb = p.get("feedbacks", 0)
    if fb >= 100:
        score += 6
        reasons.append(f"живой товар: {fb} отзывов")
    elif fb >= 20:
        score += 3
    elif fb == 0:
        score -= 8
        reasons.append("нет отзывов — проверяй продавца")

    score = max(0, min(100, score))

    if history_only:
        is_candidate = (
            drop_vs_median is not None
            and drop_vs_median >= cfg["min_drop_vs_history_pct"])
    else:
        is_candidate = (
            discount >= cfg["min_discount_pct"]
            or (drop_vs_median is not None
                and drop_vs_median >= cfg["min_drop_vs_history_pct"])
        )
    if not is_candidate or saving < cfg["min_saving_rub"] \
            or score < cfg["min_score"]:
        return None

    level = "FIRE" if score >= 70 else "HOT" if score >= 50 else "WATCH"
    return {
        **p,
        "discount_pct": round(discount, 1),
        "drop_vs_median_pct": round(drop_vs_median, 1)
        if drop_vs_median and drop_vs_median > 0 else None,
        "hist_median": round(ref_median, 2) if ref_median else None,
        "hist_min": round(ref_min, 2) if ref_min else None,
        "vs_hist_min_pct": round(vs_hist_min, 1)
        if vs_hist_min is not None else None,
        "is_new_history_min": is_new_history_min,
        "history_source": history_source,
        "history_points": own_count if own_median else None,
        "history_only": history_only,
        "saving": round(saving, 2),
        "sticker_saving": round(sticker_saving, 2),
        "score": score,
        "level": level,
        "reasons": reasons,
    }
