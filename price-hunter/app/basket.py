"""WB basket host calculation.

Wildberries serves product photos and price-history from
basket-XX.wbbasket.ru where XX depends on ``vol = nm // 100000``.
"""
from __future__ import annotations

# (vol_from, vol_to, basket_no) — known public ranges.
_RANGES = [
    (0, 143, 1), (144, 287, 2), (288, 431, 3), (432, 719, 4),
    (720, 1007, 5), (1008, 1061, 6), (1062, 1115, 7), (1116, 1169, 8),
    (1170, 1313, 9), (1314, 1601, 10), (1602, 1655, 11), (1656, 1919, 12),
    (1920, 2045, 13), (2046, 2189, 14), (2190, 2405, 15), (2406, 2621, 16),
    (2622, 2837, 17), (2838, 3053, 18), (3054, 3269, 19), (3270, 3485, 20),
    (3486, 3701, 21), (3702, 3917, 22), (3918, 4133, 23), (4134, 4349, 24),
    (4350, 4565, 25), (4566, 4781, 26), (4782, 4997, 27), (4998, 5213, 28),
]


def basket_no(nm: int) -> int:
    vol = nm // 100_000
    for lo, hi, no in _RANGES:
        if lo <= vol <= hi:
            return no
    # Extrapolate beyond the known table: every further 216 vols = +1 basket.
    last_hi, last_no = _RANGES[-1][1], _RANGES[-1][2]
    return last_no + 1 + (vol - last_hi - 1) // 216


def photo_url(nm: int, size: str = "c516x688") -> str:
    vol, part = nm // 100_000, nm // 1_000
    return (
        f"https://basket-{basket_no(nm):02d}.wbbasket.ru"
        f"/vol{vol}/part{part}/{nm}/images/{size}/1.webp"
    )


def price_history_url(nm: int) -> str:
    vol, part = nm // 100_000, nm // 1_000
    return (
        f"https://basket-{basket_no(nm):02d}.wbbasket.ru"
        f"/vol{vol}/part{part}/{nm}/info/price-history.json"
    )


def product_url(nm: int) -> str:
    return f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
