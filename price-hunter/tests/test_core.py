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
                                    {"count": 0}, 4_700, CFG)
        no_fb = detector.evaluate(prod(10_000, 3_000, feedbacks=0),
                                  {"count": 0}, 4_700, CFG)
        self.assertIsNotNone(with_fb)
        self.assertTrue(no_fb is None or no_fb["score"] < with_fb["score"])

    def test_huge_sticker_without_history_is_not_an_alert(self):
        a = detector.evaluate(prod(100_000, 1_000), {"count": 0}, None, CFG)
        self.assertIsNone(a)

    def test_own_history_can_trigger_without_sticker_discount(self):
        # No crossed-out discount at all (price == sale), but today's
        # price is far below our own observed normal price.
        own = {"count": 12, "median": 10_000, "min": 9_500, "prev": 9_900}
        a = detector.evaluate(prod(4_000, 4_000), own, None, CFG)
        self.assertIsNotNone(a)
        self.assertEqual(a["history_source"], "own")
        self.assertEqual(a["saving"], 6_000)
        self.assertEqual(a["hist_min"], 9_500)
        self.assertTrue(a["is_new_history_min"])
        self.assertTrue(any("новый исторический минимум" in r
                            for r in a["reasons"]))
        self.assertFalse(any("карточке" in r for r in a["reasons"]))

    def test_own_history_wins_over_inflated_external_reference(self):
        own = {"count": 8, "median": 5_000, "min": 4_900, "prev": 5_000}
        a = detector.evaluate(prod(20_000, 4_800), own, 20_000, CFG)
        self.assertIsNone(a)

    def test_external_history_min_breakthrough_is_scored(self):
        a = detector.evaluate(prod(10_000, 6_000), {"count": 0}, 10_000,
                              CFG, wb_hist_min=6_500)
        self.assertIsNotNone(a)
        self.assertEqual(a["hist_min"], 6_500)
        self.assertTrue(a["is_new_history_min"])
        self.assertAlmostEqual(a["vs_hist_min_pct"], 7.7, places=1)

    def test_price_above_historical_min_still_records_min(self):
        own = {"count": 12, "median": 10_000, "min": 4_000, "prev": 9_900}
        a = detector.evaluate(prod(10_000, 6_000), own, None, CFG)
        self.assertIsNotNone(a)
        self.assertEqual(a["hist_min"], 4_000)
        self.assertFalse(a["is_new_history_min"])
        self.assertAlmostEqual(a["vs_hist_min_pct"], -50.0, places=1)
        self.assertFalse(any("минимума площадки" in r for r in a["reasons"]))

    def test_min_price_gate_uses_history_median_not_card_price(self):
        a = detector.evaluate(prod(1_000, 500), {"count": 0}, 5_000, CFG)
        self.assertIsNotNone(a)
        self.assertEqual(a["hist_median"], 5_000)
        self.assertEqual(a["saving"], 4_500)

    def test_legacy_mixed_mode_can_still_use_sticker(self):
        cfg = dict(CFG, history_only=False)
        a = detector.evaluate(prod(10_000, 3_000), {"count": 0}, None, cfg)
        self.assertIsNotNone(a)
        self.assertFalse(a["history_only"])


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

    def test_alert_persists_historical_min(self):
        own = {"count": 12, "median": 10_000, "min": 6_500, "prev": 9_900}
        alert = detector.evaluate(prod(10_000, 6_000, nm=778), own, None, CFG)
        self.assertIsNotNone(alert)
        alert["source"] = "live"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            dbt = DB(Path(d) / "t.db")
            try:
                dbt.upsert_product({**prod(10_000, 6_000, nm=778),
                                    "photo": "", "url": ""})
                dbt.add_alert(alert)
                loaded = dbt.alerts(limit=1)[0]
                self.assertEqual(loaded["hist_min"], 6_500.0)
                self.assertAlmostEqual(loaded["vs_hist_min_pct"], 7.7, places=1)
                self.assertTrue(loaded["history_confirmed"])
            finally:
                dbt.close()

    def test_history_filter_and_stats_exclude_old_sticker_alerts(self):
        own = {"count": 12, "median": 10_000, "min": 6_500, "prev": 9_900}
        confirmed = detector.evaluate(
            prod(10_000, 6_000, nm=778), own, None, CFG)
        legacy = detector.evaluate(
            prod(121_160, 5_351, nm=779), {"count": 0}, None,
            dict(CFG, history_only=False))
        self.assertIsNotNone(confirmed)
        self.assertIsNotNone(legacy)
        self.assertIsNone(legacy["hist_median"])
        confirmed["source"] = legacy["source"] = "live"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            dbt = DB(Path(d) / "t.db")
            try:
                for p in (prod(10_000, 6_000, nm=778),
                          prod(121_160, 5_351, nm=779)):
                    dbt.upsert_product({**p, "photo": "", "url": ""})
                dbt.add_alert(confirmed)
                dbt.add_alert(legacy)

                all_alerts = dbt.alerts(limit=10)
                self.assertEqual({a["nm"] for a in all_alerts}, {778, 779})
                self.assertEqual(
                    {a["nm"]: a["history_confirmed"] for a in all_alerts},
                    {778: True, 779: False})
                filtered = dbt.alerts(
                    limit=10, only_history_confirmed=True)
                self.assertEqual([a["nm"] for a in filtered], [778])

                stats = dbt.stats()
                self.assertEqual(stats["alerts_today"], 2)
                self.assertEqual(stats["alerts_today_history_confirmed"], 1)
                self.assertEqual(stats["alerts_today_unconfirmed"], 1)
                self.assertEqual(stats["alerts_unconfirmed_total"], 1)
                self.assertEqual(
                    stats["potential_saving_history_today"],
                    confirmed["saving"])
            finally:
                dbt.close()


class TelegramTest(unittest.TestCase):
    def test_demo_footnote_and_prices(self):
        a = {**prod(34_990, 3_499), "level": "FIRE", "score": 92,
             "discount_pct": 90.0, "saving": 31_491,
             "sale_price_rub": 3_499, "price_rub": 34_990,
             "drop_vs_median_pct": 87.0, "hist_median": 26_200,
             "hist_min": 4_000, "vs_hist_min_pct": 12.5,
             "reasons": ["тест"], "source": "demo"}
        text = alert_text(a)
        self.assertIn("3 499 ₽", text)
        self.assertIn("ниже обычной цены по истории", text)
        self.assertIn("Новый минимум на площадке", text)
        self.assertIn("4 000 ₽", text)
        self.assertIn("26 200 ₽", text)
        self.assertIn("не была основанием алерта", text)
        self.assertIn("DEMO", text)


if __name__ == "__main__":
    unittest.main()
