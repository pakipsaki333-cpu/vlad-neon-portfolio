"""DEMO data generator.

Used only when WB is unreachable (like this sandbox) or mode=demo.
It simulates a realistic catalog: most products keep stable prices with
normal WB-style sticker discounts; occasionally a product 'crashes'
(seller self-buyout dump / fat-finger error) for one or two scans.
Everything born here is labelled source='demo' end-to-end - UI badge,
Telegram footnote - so it can never be confused with a real find.
"""
from __future__ import annotations

import random

_CATALOG = [
    # nm, brand, name, base price RUB, normal sticker discount %
    (154368401, "Sony", "Наушники беспроводные WH-1000XM5", 34_990, 25),
    (176543210, "Apple", "Наушники AirPods Pro 2 USB-C", 27_990, 18),
    (210987654, "Dyson", "Фен Supersonic HD15 с насадками", 49_990, 12),
    (198765432, "LEGO", "Конструктор Icons Букет цветов 10314", 7_499, 30),
    (165432198, "Nike", "Кроссовки Air Force 1 '07", 14_999, 35),
    (187654321, "Samsung", "Смартфон Galaxy S24 256 ГБ", 79_990, 22),
    (143219876, "Roborock", "Робот-пылесос S8 с влажной уборкой", 44_990, 28),
    (129876543, "MSI", "Ноутбук игровой Katana 15 RTX 4060", 119_990, 15),
    (201234567, "Sony", "Игровая приставка PlayStation 5 Slim", 59_990, 8),
    (112233445, "DeLonghi", "Кофемашина Magnifica S автоматическая", 54_990, 32),
    (223344556, "Adidas", "Кроссовки беговые Ultraboost Light", 19_999, 40),
    (334455667, "Xiaomi", "Электросамокат Electric Scooter 4 Pro", 39_990, 20),
    (445566778, "Garmin", "Умные часы Fenix 7 Pro Solar", 89_990, 14),
    (556677889, "LG", "Телевизор OLED evo C3 55\" 4K", 149_990, 26),
    (667788990, "Givenchy", "Парфюмерная вода L'Interdit 80 мл", 11_490, 45),
    (778899001, "KitchenAid", "Миксер планетарный Artisan 4.8 л", 64_990, 18),
]

_state: dict[int, dict] = {}


def _product(nm, brand, name, price, sale, crashed=False):
    return {
        "nm": nm, "name": name, "brand": brand,
        "supplier": f"Demo Seller {nm % 7 + 1}",
        "price_rub": round(price, 2), "sale_price_rub": round(sale, 2),
        "rating": round(random.uniform(4.5, 5.0), 1) if not crashed else 4.8,
        "feedbacks": random.randint(120, 4200),
        "photo": "",
        "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
        "_demo_crashed": crashed,
    }


def generate_scan(queries: list[str]) -> list[dict]:
    """One simulated scan across the demo catalog."""
    out = []
    # Pick 0-2 victims for a price crash this round (weighted to sometimes be quiet).
    victims = set(random.sample(_CATALOG, k=random.choice([0, 1, 1, 2])))
    for nm, brand, name, base, normal_disc in _CATALOG:
        st = _state.setdefault(nm, {"price": base * random.uniform(0.98, 1.05)})
        # Gentle price drift so history/median looks alive.
        st["price"] *= random.uniform(0.98, 1.02)
        price = st["price"]
        if (nm, brand, name, base, normal_disc) in victims:
            crash_disc = random.uniform(82, 96)          # the 'error'
            sale = price * (1 - crash_disc / 100)
            out.append(_product(nm, brand, name, price, sale, crashed=True))
        else:
            disc = max(5.0, random.gauss(normal_disc, 6))
            sale = price * (1 - disc / 100)
            out.append(_product(nm, brand, name, price, sale))
    # Noise listings with fake WB-style sticker discounts: inflated priceU,
    # but the sale price is the same every day. Stable nms + stable prices,
    # so (a) their WB-style history median ~= today's sale price and
    # (b) our own snapshot history converges too — detector must reject them.
    for nm, name, base, fake_disc in _NOISE:
        st = _state.setdefault(nm, {"price": base})
        st["price"] *= random.uniform(0.99, 1.01)
        sale = st["price"] * (1 - fake_disc / 100) * random.uniform(0.98, 1.02)
        out.append(_product(nm, "NoName", name, st["price"], sale))
    random.shuffle(out)
    return out


# nm, name, inflated 'before' price, everyday fake discount %
_NOISE = [
    (900_000_001, "Платье женское вечернее", 15_999, 72),
    (900_000_002, "Сумка женская через плечо", 8_999, 68),
    (900_000_003, "Крем для лица омолаживающий", 4_599, 75),
    (900_000_004, "Футболка оверсайз базовая", 3_999, 70),
]


def demo_history_median(nm: int) -> float | None:
    """Pretend WB price-history for demo items: near their normal sale price."""
    for c_nm, _b, _n, base, disc in _CATALOG:
        if c_nm == nm:
            return base * (1 - disc / 100)
    for c_nm, _n, base, disc in _NOISE:
        if c_nm == nm:
            # History proves the 'discount' is the everyday price.
            return base * (1 - disc / 100)
    return None


def demo_history_stats(nm: int) -> dict:
    """Simulated external history summary, including a marketplace low."""
    median = demo_history_median(nm)
    if median is None:
        return {"count": 0, "median": None, "min": None, "max": None}
    return {"count": 30, "median": float(median),
            "min": float(median * 0.97), "max": float(median * 1.05)}
