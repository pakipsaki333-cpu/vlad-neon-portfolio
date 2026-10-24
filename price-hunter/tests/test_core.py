"""Core unit tests (stdlib unittest, no extra deps).

Run from price-hunter/:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import detector  # noqa: E402
from app.basket import basket_no, photo_url  # noqa: E402
from app.config import DEFAULTS  # noqa: E402
from app.db import DB  # noqa: E402
from app.telegram import alert_text  # noqa: E402
from app.wb_client import normalize_product  # noqa: E402

CFG = dict(DEFAULTS)


def prod(price, sale, feedbacks=500, nm=123):
    return {"nm": nm, "name": "Товар", "brand": "Бренд", "supplier": "s",
            "price_rub": price, "sale_price_rub": sale,
            "rating": 4.8, "feedbacks": feedbacks, "photo": "", "url": "u"}


class DetectorTest(unittest.TestCase):
    def test_real_crash_is_fire(self):
        a = detector.evaluate(prod(34_990, 3_499), {"count": 0}, 26_200, CFG)
        self.assertIsNotNone(a)
        self.assertEqual(a["level"], "FIRE")
        self.assertGreaterEqual(a["score"], 70)
        self.assertAlmostEqual(a["discount_pct"], 90.0, delta=0.2)

    def test_fake_sticker_discount_rejected(self):
        # -72% on the sticker, but history median == today's sale price:
        # this 'discount' is the everyday price. Must NOT alert.
        a = detector.evaluate(prod(15_999, 4_480), {"count": 0}, 4_480, CFG)
        self.assertIsNone(a)

    def test_dump_caught_without_big_sticker_discount(self):
        # Self-buyout style: sticker discount only -60% (< threshold), but
        # price is -58% vs its own history median -> still an alert.
        a = detector.evaluate(prod(10_000, 4_000), {"count": 0}, 9_500, CFG)
        self.assertIsNotNone(a)
        self.assertIsNotNone(a["drop_vs_median_pct"])

    def test_normal_price_no_alert(self):
        a = detector.evaluate(prod(10_000, 8_500), {"count": 0}, 8_600, CFG)
        self.assertIsNone(a)

    def test_trinket_below_min_price(self):
        a = detector.evaluate(prod(900, 90), {"count": 0}, 800, CFG)
        self.assertIsNone(a)

    def test_no_feedbacks_penalty_keeps_borderline_out(self):
        with_fb = detector.evaluate(prod(10_000, 3_000, feedbacks=500),
                                    {"count": 0}, None, CFG)
        no_fb = detector.evaluate(prod(10_000, 3_000, feedbacks=0),
                                  {"count": 0}, None, CFG)
        self.assertIsNotNone(with_fb)
        self.assertTrue(no_fb is None or no_fb["score"] < with_fb["score"])


class BasketTest(unittest.TestCase):
    def test_ranges_and_extrapolation(self):
        self.assertEqual(basket_no(100_000), 1)        # vol 1
        self.assertEqual(basket_no(154_368_401), 10)   # vol 1543
        self.assertGreater(basket_no(999_999_999), 28)  # extrapolated

    def test_photo_url(self):
        u = photo_url(154_368_401)
        self.assertIn("basket-10.wbbasket.ru/vol1543/part154368/154368401", u)


class NormalizeTest(unittest.TestCase):
    def test_flat(self):
        p = normalize_product({"id": 42, "name": "x", "brand": "b",
                               "priceU": 100_000, "salePriceU": 40_000})
        self.assertEqual((p["price_rub"], p["sale_price_rub"]), (1000.0, 400.0))

    def test_nested_sizes(self):
        p = normalize_product({"id": 43, "sizes": [
            {"price": {"basic": 200_000, "product": 50_000}}]})
        self.assertEqual((p["price_rub"], p["sale_price_rub"]), (2000.0, 500.0))

    def test_no_price(self):
        self.assertIsNone(normalize_product({"id": 44}))


class DBTest(unittest.TestCase):
    def test_history_stats(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            dbt = DB(Path(d) / "t.db")
            try:
                for sale in (1000.0, 1100.0, 900.0):
                    p = prod(2000.0, sale, nm=777)
                    p.update(photo="", url="")
                    dbt.upsert_product(p)
                stats = dbt.history_stats(777, exclude_latest_minutes=0)
                self.assertEqual(stats["count"], 3)
                self.assertEqual(stats["median"], 1000.0)
                self.assertEqual(stats["prev"], 900.0)  # latest by time, not max
            finally:
                dbt.close()  # Windows locks open SQLite files


class TelegramTest(unittest.TestCase):
    def test_demo_footnote_and_prices(self):
        a = {**prod(34_990, 3_499), "level": "FIRE", "score": 92,
             "discount_pct": 90.0, "saving": 31_491,
             "sale_price_rub": 3_499, "price_rub": 34_990,
             "drop_vs_median_pct": 87.0, "hist_median": 26_200,
             "reasons": ["тест"], "source": "demo"}
        text = alert_text(a)
        self.assertIn("3 499 ₽", text)
        self.assertIn("DEMO", text)


if __name__ == "__main__":
    unittest.main()
