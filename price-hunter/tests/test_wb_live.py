"""Live-WB client regression tests with a fake transport (no network).

These pin the exact failure the user hit: WB answers 200 with top-level
``products`` (v4/v18) instead of ``data.products``, endpoints rotate,
some networks get 403 for plain httpx, and one dead query must not
discard the whole scan.

Run from price-hunter/:  python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.wb_client as wbc  # noqa: E402
from app.basket import basket_no  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import DB  # noqa: E402
from app.scanner import Scanner  # noqa: E402
from app.wb_client import (WBClient, WBUnavailable, _Response,  # noqa: E402
                           extract_products, normalize_product)
from app.wb_dom import (ChromeBrowserSession,  # noqa: E402
                        find_browser_executable, parse_dom_search_items)

RAW = {"id": 1097975275, "name": "Наушники X", "brand": "Brand",
       "supplier": "Seller", "reviewRating": 4.9, "feedbacks": 123,
       "sizes": [{"name": "42", "stocks": [{"qty": 2}],
                  "price": {"basic": 202_400, "product": 23_800}}]}


class FakeClient(WBClient):
    def __init__(self, handler, transport="httpx"):
        super().__init__(transport=transport)
        self.handler = handler
        self.calls: list[tuple[str, str]] = []

    def _raw_get(self, transport, url, params):
        self.calls.append((transport, url))
        status, body = self.handler(transport, url, params, len(self.calls))
        text = body if isinstance(body, str) else json.dumps(body)
        return _Response(status, text, {"content-type": "application/json"},
                         url, transport, 1)


class DOMFallbackClient(FakeClient):
    def _search_dom(self, query, page, sort, attempts):
        attempts.append({
            "endpoint": "https://www.wildberries.ru/catalog/0/search.aspx",
            "transport": "chrome-dom", "status": 200, "outcome": "ok",
            "shape": "rendered-dom", "raw_products": 1, "products": 1,
        })
        product = normalize_product(RAW)
        product["source_transport"] = "chrome-dom"
        return [product], False


class FlakyDOMClient(FakeClient):
    """DOM renders an empty grid (e.g. throttled background tab)."""

    def __init__(self, handler, transport="auto"):
        super().__init__(handler, transport=transport)
        self.dom_calls = 0

    def _search_dom(self, query, page, sort, attempts):
        self.dom_calls += 1
        attempts.append({
            "endpoint": "https://www.wildberries.ru/catalog/0/search.aspx",
            "transport": "chrome-dom", "status": 200,
            "outcome": "bad_shape", "shape": "rendered-dom",
            "raw_products": 0, "products": 0,
        })
        return [], False


class ShapeTest(unittest.TestCase):
    def test_top_level_products_v18(self):
        c = FakeClient(lambda t, u, p, n: (200, {"products": [RAW], "total": 1,
                                                 "metadata": {"rs": 100}}))
        out = c.search("наушники")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sale_price_rub"], 238.0)
        self.assertEqual(c.last_attempts[-1]["shape"], "products")

    def test_legacy_data_products(self):
        c = FakeClient(lambda t, u, p, n: (200, {"data": {"products": [RAW]}}))
        self.assertEqual(len(c.search("наушники")), 1)

    def test_unknown_shape_is_not_empty(self):
        prods, shape, _ = extract_products({"unexpected": []})
        self.assertIsNone(prods)
        self.assertEqual(shape, "unknown")

    def test_first_endpoint_blocked_second_works(self):
        def handler(t, u, p, n):
            return ((403, "<html>Forbidden</html>") if "u-search" in u
                    else (200, {"products": [RAW]}))
        c = FakeClient(handler)
        self.assertEqual(len(c.search("наушники")), 1)
        outcomes = [a["outcome"] for a in c.last_attempts]
        self.assertEqual(outcomes[0], "blocked")
        self.assertEqual(outcomes[-1], "ok")

    def test_all_blocked_raises_with_diagnostics(self):
        c = FakeClient(lambda t, u, p, n: (403, "<html>wbaas antibot</html>"))
        with self.assertRaises(WBUnavailable) as ctx:
            c.search("наушники")
        self.assertEqual(ctx.exception.outcome, "blocked")
        self.assertTrue(ctx.exception.attempts)

    def test_browser_transport_fallback_on_403(self):
        def handler(t, u, p, n):
            return ((403, "Forbidden") if t == "httpx"
                    else (200, {"products": [RAW]}))
        c = FakeClient(handler, transport="auto")
        with mock.patch.object(wbc, "curl_cffi_available", return_value=True):
            out = c.search("наушники")
        self.assertEqual(len(out), 1)
        self.assertEqual(c.last_transport, "browser")

    def test_rendered_dom_fallback_after_all_403(self):
        # This is the user's confirmed 2026-10-25 case: LIVE is selected,
        # but httpx and curl_cffi both receive 403 while a real browser
        # can render WB.  Rendered tiles are the final fallback.
        c = DOMFallbackClient(
            lambda t, u, p, n: (403, "<html>wbaas antibot</html>"),
            transport="auto")
        with mock.patch.object(wbc, "curl_cffi_available", return_value=True), \
                mock.patch.object(wbc, "chrome_available", return_value=True):
            out = c.search("смартфон iphone")
        self.assertEqual(len(out), 1)
        self.assertEqual(c.last_transport, "chrome-dom")
        self.assertEqual(out[0]["source_transport"], "chrome-dom")
        # The next query goes straight to DOM instead of replaying the
        # already-refused API endpoint/transport matrix.
        c.calls.clear()
        with mock.patch.object(wbc, "chrome_available", return_value=True):
            self.assertEqual(len(c.search("наушники sony")), 1)
        self.assertEqual(c.calls, [])

    def test_flaky_empty_dom_fails_fast_without_full_matrix(self):
        # Regression: with WB and Ozon in separate tabs, one flaky
        # empty render of the background WB tab used to flip the
        # client out of DOM mode, so every query paid the full
        # endpoint/transport matrix plus a second DOM wait and whole
        # scans looked frozen.  DOM mode must survive single empties.
        c = FlakyDOMClient(lambda t, u, p, n: (403, "Forbidden"))
        c._prefer_dom_search = True
        with mock.patch.object(wbc, "chrome_available", return_value=True), \
                mock.patch.object(wbc, "curl_cffi_available", return_value=True):
            for expected_streak in (1, 2):
                c.calls.clear()
                with self.assertRaises(WBUnavailable):
                    c.search("наушники")
                self.assertEqual(c.calls, [])  # API matrix not replayed
                self.assertTrue(c._prefer_dom_search)
                self.assertEqual(c._dom_empty_streak, expected_streak)
            # Third consecutive empty retests the matrix once, then
            # gives up on DOM preference instead of grinding forever.
            c.calls.clear()
            with self.assertRaises(WBUnavailable):
                c.search("наушники")
            self.assertTrue(c.calls)
            self.assertFalse(c._prefer_dom_search)
            self.assertEqual(c._dom_empty_streak, 0)
            self.assertEqual(c.dom_calls, 4)  # 3 prefer tries + 1 final

    def test_auto_transport_chain_includes_real_chrome_last(self):
        c = FakeClient(lambda t, u, p, n: (200, {"products": [RAW]}),
                       transport="auto")
        with mock.patch.object(wbc, "curl_cffi_available", return_value=True), \
                mock.patch.object(wbc, "chrome_available", return_value=True):
            self.assertEqual(
                c._transports_for("https://search.wb.ru/search"),
                ["httpx", "browser", "chrome"])

    def test_history_median_is_cached(self):
        # The scanner asks for the external history median of every
        # "likely" product each cycle; without a cache that was
        # hundreds of identical basket fetches per WB pass.
        c = FakeClient(lambda t, u, p, n: (200, {"products": [RAW]}))
        calls = []

        def fake_history(nm):
            calls.append(nm)
            return [{"ts": 1, "price_rub": 100.0},
                    {"ts": 2, "price_rub": 200.0},
                    {"ts": 3, "price_rub": 300.0}]

        c.price_history = fake_history
        self.assertEqual(c.history_median(123), 200.0)
        self.assertEqual(c.history_median(123), 200.0)
        self.assertEqual(c.history_stats(123),
                         {"count": 3, "median": 200.0,
                          "min": 100.0, "max": 300.0})
        self.assertEqual(c.history_min(123), 100.0)
        self.assertEqual(calls, [123])

    def test_card_detail_v4_shape(self):
        seen_urls = {}

        def handler(t, u, p, n):
            seen_urls["url"] = u
            return 200, {"products": [RAW], "promotions": []}
        c = FakeClient(handler)
        out = c.card_detail([RAW["id"]])
        self.assertEqual(len(out), 1)
        self.assertIn("/cards/v4/detail", seen_urls["url"])


class BrowserDOMTest(unittest.TestCase):
    def test_launch_args_disable_background_throttling(self):
        # WB and Ozon own separate tabs; the background one must keep
        # rendering/timers alive or its DOM search stalls the scan.
        session = ChromeBrowserSession(executable="chrome-stub")
        session.executable = "chrome-stub"  # normally resolved in _launch()
        args = session._launch_args()
        self.assertIn("--disable-background-timer-throttling", args)
        self.assertIn("--disable-backgrounding-occluded-windows", args)
        self.assertIn("--disable-renderer-backgrounding", args)
        self.assertTrue(any(
            a.startswith("--disable-features=IntensiveWakeUpThrottling")
            for a in args))

    def test_parse_rendered_tile_text(self):
        items = [{
            "id": 123456,
            "title": "Apple / iPhone 16 128 ГБ",
            "text": ("-71%\n9 990 ₽\n33 990 ₽\nс WB Кошельком\n"
                     "Apple / iPhone 16 128 ГБ\n4,9 • 1 234 оценки\n"
                     "от 499 ₽/мес"),
            "delText": "33 990 ₽",
            "href": "https://www.wildberries.ru/catalog/123456/detail.aspx",
            "photo": "https://example.invalid/photo.webp",
        }]
        out = parse_dom_search_items(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["brand"], "Apple")
        self.assertEqual(out[0]["name"], "iPhone 16 128 ГБ")
        self.assertEqual(out[0]["sale_price_rub"], 9990.0)
        self.assertEqual(out[0]["price_rub"], 33990.0)
        self.assertEqual(out[0]["rating"], 4.9)
        self.assertEqual(out[0]["feedbacks"], 1234)

    def test_custom_browser_path_must_exist(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            fake = Path(d) / "chrome.exe"
            fake.write_bytes(b"stub")
            self.assertEqual(find_browser_executable(str(fake)), str(fake))
            self.assertIsNone(find_browser_executable(str(Path(d) / "no.exe")))


class NormalizePriceTest(unittest.TestCase):
    def test_cheapest_in_stock_size(self):
        raw = {"id": 1, "name": "x", "sizes": [
            {"name": "S", "stocks": [{"qty": 0}],
             "price": {"basic": 100_000, "product": 5_000}},
            {"name": "M", "stocks": [{"qty": 3}],
             "price": {"basic": 100_000, "product": 40_000}},
        ]}
        p = normalize_product(raw)
        self.assertEqual((p["sale_price_rub"], p["size"]), (400.0, "M"))

    def test_basket_42_for_current_ids(self):
        self.assertEqual(basket_no(1_097_975_275), 42)


def _settings(**over) -> Settings:
    s = Settings()
    s._data.update({"mode": "live", "queries": ["bad", "good"],
                    "sorts": ["popular"], "pages_per_query": 1,
                    "ozon_enabled": False,
                    "wb_request_delay_sec": 0.01})
    s._data.update(over)
    return s


class _DummyWB:
    def search(self, q, page=1, sort="popular"):
        if q == "bad":
            raise WBUnavailable("WB blocked", outcome="blocked")
        return [{"nm": 555, "name": "Товар", "brand": "B", "supplier": "S",
                 "price_rub": 5000.0, "sale_price_rub": 4500.0, "rating": 5,
                 "feedbacks": 10, "photo": "", "url": "https://example.invalid"}]

    def history_median(self, nm):
        return None

    def probe(self, q="наушники"):
        return {"ok": True, "outcome": "ok", "message": "WB доступен (fake)",
                "transport": "fake", "products": 1}


class ScannerResilienceTest(unittest.TestCase):
    def _scanner(self, db, settings):
        sc = Scanner(db, settings)
        cfg = settings.get()
        sc._client_cache = _DummyWB()  # type: ignore[assignment]
        sc._client_sig = sc._client_signature(cfg)
        sc.last_probe_ts = 999_999_999_999  # live mode: skip probe
        return sc

    def test_partial_failure_keeps_found_products(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                sc = self._scanner(db, _settings())
                result = sc.scan_once()
                self.assertEqual(result["scanned"], 1)
                self.assertEqual(result["request_failures"], 1)
                self.assertIn("Частично", result["error"])
                self.assertEqual(db.stats()["products_seen"], 1)
            finally:
                db.close()

    def test_scan_reports_progress_and_clears_it(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                sc = self._scanner(db, _settings())
                seen: list[dict | None] = []
                dummy = sc._client_cache
                original = dummy.search

                def spy(q, page=1, sort="popular"):
                    seen.append(dict(sc._progress) if sc._progress else None)
                    return original(q, page=page, sort=sort)

                dummy.search = spy
                sc.scan_once()
                self.assertTrue(seen and seen[0] is not None)
                self.assertEqual(seen[0]["marketplace"], "wb")
                self.assertEqual(seen[0]["query_total"], 2)
                self.assertEqual(seen[0]["query"], "bad")
                status = sc.status()
                self.assertIsNone(status["progress"])
                self.assertFalse(status["stopping"])
            finally:
                db.close()

    def test_auto_reprobes_after_fallback(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                sc = self._scanner(db, _settings(mode="auto"))
                sc.effective_mode = "demo"
                sc.next_live_probe_ts = 1  # long due
                self.assertEqual(sc.resolve_mode(), "live")
                self.assertIsNotNone(sc.last_probe)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
