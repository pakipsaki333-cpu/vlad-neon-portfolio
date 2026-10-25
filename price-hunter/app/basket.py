"""WB basket host calculation.

Wildberries serves product photos and price-history from
basket-XX.wbbasket.ru where XX depends on ``vol = nm // 100000``.
"""
from __future__ import annotations

# (vol_from, vol_to, basket_no) — mirrored from WB's own public
# https://cdn.wbbasket.ru/api/v3/upstreams (mediabasket_route_map,
# verified 2026-10-25). WB extends this table as new vols appear.
_RANGES = [
    (0, 143, 1), (144, 287, 2), (288, 431, 3), (432, 719, 4),
    (720, 1007, 5), (1008, 1061, 6), (1062, 1115, 7), (1116, 1169, 8),
    (1170, 1313, 9), (1314, 1601, 10), (1602, 1655, 11), (1656, 1919, 12),
    (1920, 2045, 13), (2046, 2189, 14), (2190, 2405, 15), (2406, 2621, 16),
    (2622, 2837, 17), (2838, 3053, 18), (3054, 3269, 19), (3270, 3485, 20),
    (3486, 3701, 21), (3702, 3917, 22), (3918, 4133, 23), (4134, 4349, 24),
    (4350, 4565, 25), (4566, 4877, 26), (4878, 5189, 27), (5190, 5501, 28),
    (5502, 5813, 29), (5814, 6125, 30), (6126, 6437, 31), (6438, 6749, 32),
    (6750, 7061, 33), (7062, 7373, 34), (7374, 7685, 35), (7686, 7997, 36),
    (7998, 8309, 37), (8310, 8741, 38), (8742, 9173, 39), (9174, 9605, 40),
    (9606, 10373, 41), (10374, 11141, 42), (11142, 11909, 43),
    (11910, 12677, 44), (12678, 13445, 45), (13446, 14213, 46),
    (14214, 14981, 47), (14982, 15749, 48), (15750, 16517, 49),
    (16518, 17285, 50), (17286, 18053, 51), (18054, 18821, 52),
    (18822, 19589, 53), (19590, 20357, 54), (20358, 21125, 55),
    (21126, 21893, 56), (21894, 22661, 57), (22662, 23429, 58),
    (23430, 24197, 59), (24198, 24965, 60), (24966, 25733, 61),
    (25734, 26501, 62), (26502, 27269, 63), (27270, 28037, 64),
    (28038, 28805, 65),
]


def basket_no(nm: int) -> int:
    vol = nm // 100_000
    for lo, hi, no in _RANGES:
        if lo <= vol <= hi:
            return no
    # Extrapolate beyond the known table using the newest range width.
    last_lo, last_hi, last_no = _RANGES[-1]
    width = last_hi - last_lo + 1
    return last_no + 1 + (vol - last_hi - 1) // width


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


def price_history_urls(nm: int) -> list[str]:
    """Both CDN domains serve the same shard; try the legacy host first."""
    vol, part = nm // 100_000, nm // 1_000
    no = basket_no(nm)
    path = f"/vol{vol}/part{part}/{nm}/info/price-history.json"
    return [
        f"https://basket-{no:02d}.wbbasket.ru{path}",
        f"https://basket-{no:02d}.wbcontent.net{path}",
    ]


def product_url(nm: int) -> str:
    return f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
