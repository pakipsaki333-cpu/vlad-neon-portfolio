"""Offline Ozon integration tests: parsers, DB isolation and scanner
partial-failure behaviour.  No network or real browser is touched.

Run from price-hunter/:  python -m unittest discover -s tests -v
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.db import DB  # noqa: E402
from app.ozon_client import (  # noqa: E402
    OzonClient, OzonUnavailable, _Response as _OzonResponse,
    parse_composer_search, parse_ozon_dom_items)
from app.scanner import Scanner  # noqa: E402
from app.telegram import alert_text  # noqa: E402
from app.wb_client import WBUnavailable  # noqa: E402
from app.wb_dom import OZON_HOME, ChromeBrowserSession  # noqa: E402
from desktop.controller import DesktopController  # noqa: E402


def _tile_payload() -> dict:
    widget = {
        "items": [{
            "sku": 123456,
            "action": {"link": "/product/naushniki-123456/?oos_search=false"},
            "tileImage": {"coverImage": "https://ir.ozone.ru/s3/test.jpg"},
            "mainState": [
                {
                    "type": "priceV2",
                    "priceV2": {"price": [
                        {"text": "1 299 ₽", "textStyle": "PRICE"},
                        {"text": "4 999 ₽", "textStyle": "ORIGINAL_PRICE"},
                    ]},
                },
                {"id": "name", "textDS": {"text": "Наушники беспроводные"}},
                {
                    "labelListV2": {"items": [
                        {"type": "icon", "icon": {"image": "ic_s_star"}},
                        {"type": "text", "text": {"text": "4.9"}},
                        {"type": "text", "text": {"text": "1 234"}},
                    ]},
                },
                {
                    "labelListV2": {"items": [
                        {"type": "text", "text": {"text": "Sony"}},
                    ]},
                },
            ],
        }]
    }
    return {"widgetStates": {
        "tileGridDesktop-777": json.dumps(widget, ensure_ascii=False)}}


class OzonParserTest(unittest.TestCase):
    def test_current_tilegrid_widget(self):
        products, shape, raw_count = parse_composer_search(_tile_payload())
        self.assertEqual((shape, raw_count), ("tileGridDesktop", 1))
        self.assertEqual(len(products), 1)
        p = products[0]
        self.assertEqual(p["marketplace"], "ozon")
        self.assertEqual((p["nm"], p["sku"]), (123456, 123456))
        self.assertEqual((p["price_rub"], p["sale_price_rub"]), (4999.0, 1299.0))
        self.assertEqual(p["brand"], "Sony")
        self.assertEqual((p["rating"], p["feedbacks"]), (4.9, 1234))
        self.assertEqual(
            p["url"], "https://www.ozon.ru/product/naushniki-123456/")

    def test_recursive_product_fallback_keeps_large_ruble_price(self):
        widget = {"items": [{"product": {
            "id": 987654,
            "title": "Смартфон",
            "brand": "Apple",
            "price": {"price": 134313, "oldPrice": 199999},
            "rating": 5.0,
            "reviewsCount": 42,
            "imageUrl": "https://ir.ozone.ru/phone.jpg",
        }}]}
        payload = {"widgetStates": {
            "webSearchResultsV2-1": json.dumps(widget, ensure_ascii=False)}}
        products, shape, _ = parse_composer_search(payload)
        self.assertEqual(shape, "webSearchResults.product")
        self.assertEqual(products[0]["sale_price_rub"], 134313.0)

    def test_unparsed_tile_is_shape_drift_not_empty(self):
        payload = {"widgetStates": {"tileGridDesktop-1": json.dumps({
            "items": [{"sku": 1, "mainState": []}]})}}
        products, shape, raw_count = parse_composer_search(payload)
        self.assertIsNone(products)
        self.assertEqual((shape, raw_count), ("unparsed-search-widget", 1))

    def test_rendered_dom_ignores_installment_and_delivery(self):
        products = parse_ozon_dom_items([{
            "id": 456789,
            "href": "https://www.ozon.ru/product/test-456789/?from=search",
            "photo": "https://ir.ozone.ru/dom.jpg",
            "text": (
                "С Ozon Картой\n1 036 ₽\n5 999 ₽\n−83%\n"
                "259 ₽ × 4 платежа\nДоставка 99 ₽\n50 ₽/шт\n"
                "Наушники тестовые\n4.8 532 отзыва"
            ),
            "delText": "5 999 ₽",
        }])
        self.assertEqual(len(products), 1)
        p = products[0]
        self.assertEqual((p["price_rub"], p["sale_price_rub"]), (5999.0, 1036.0))
        self.assertEqual(p["name"], "Наушники тестовые")
        self.assertEqual((p["rating"], p["feedbacks"]), (4.8, 532))

    def test_search_path_and_transport_alias(self):
        self.assertEqual(
            OzonClient._search_path("айфон", page=2, sort="benefit"),
            "/search/?text=%D0%B0%D0%B9%D1%84%D0%BE%D0%BD"
            "&from_global=true&sorting=discount&page=2")
        self.assertEqual(OzonClient(transport="dom").transport, "chrome")

    def test_shared_browser_session_can_target_ozon(self):
        session = ChromeBrowserSession(home_url=OZON_HOME)
        self.assertEqual(session.home_host, "www.ozon.ru")
        self.assertEqual(
            session._challenge_outcome("Подтвердите, что вы человек"),
            "captcha")
        same_endpoint = ChromeBrowserSession(
            profile_dir=str(session.profile_dir), port=session.port)
        other_endpoint = ChromeBrowserSession(
            profile_dir=str(session.profile_dir), port=session.port + 1)
        self.assertIs(session._lock, same_endpoint._lock)
        self.assertIsNot(session._lock, other_endpoint._lock)

    def test_each_marketplace_keeps_its_own_browser_tab(self):
        def page(tid, url):
            return {"id": tid, "type": "page", "url": url,
                    "webSocketDebuggerUrl": f"ws://127.0.0.1/{tid}"}

        wb_tab = page("wb", "https://www.wildberries.ru/catalog/0/search.aspx")
        ozon_tab = page("ozon", "https://www.ozon.ru/search/?text=iphone")
        ozon = ChromeBrowserSession(home_url=OZON_HOME)
        wb = ChromeBrowserSession()

        # Each session adopts its own marketplace's tab, in any order.
        self.assertEqual(ozon._select_target([wb_tab, ozon_tab])["id"], "ozon")
        self.assertEqual(wb._select_target([ozon_tab, wb_tab])["id"], "wb")
        # Never steal the other marketplace's tab: caller creates a new one.
        self.assertIsNone(ozon._select_target([wb_tab]))
        self.assertIsNone(wb._select_target([ozon_tab]))
        # A blank tab may be adopted; an already-owned tab wins overall.
        blank = page("blank", "about:blank")
        self.assertEqual(ozon._select_target([wb_tab, blank])["id"], "blank")
        ozon._target_id = "ozon"
        self.assertEqual(
            ozon._select_target([page("ozon2", "https://www.ozon.ru/"),
                                 ozon_tab])["id"], "ozon")
        # Domain checks are registrable-domain based, not substring based.
        self.assertTrue(ozon._is_home_url("https://ozon.ru/"))
        self.assertTrue(ozon._is_home_url("https://api.ozon.ru/x"))
        self.assertFalse(ozon._is_home_url("https://www.ozon.ru.example/"))
        self.assertFalse(wb._is_home_url("https://www.ozon.ru/"))

        # Explicit "open browser" activates only its own tab.
        sent = []
        ozon.ensure_ready = lambda home_url=None: {"url": "https://www.ozon.ru/"}
        ozon._send = lambda method, params=None: sent.append(method) or {}
        ozon.evaluate = lambda expression: "https://www.ozon.ru/"
        info = ozon.bring_to_front()
        self.assertEqual(sent, ["Page.bringToFront"])
        self.assertEqual(info["url"], "https://www.ozon.ru/")

    def test_flaky_empty_dom_fails_fast_without_full_matrix(self):
        class FlakyOzon(OzonClient):
            def __init__(self):
                super().__init__(transport="chrome")
                self.raw_calls = 0
                self.dom_calls = 0

            def _raw_get(self, transport, url, params):
                self.raw_calls += 1
                return _OzonResponse(
                    status_code=403, text="", headers={}, url=url,
                    transport=transport, elapsed_ms=1)

            def _search_dom(self, query, page, sort, attempts):
                self.dom_calls += 1
                attempts.append({
                    "endpoint": "https://www.ozon.ru/search/",
                    "transport": "chrome-dom", "status": 200,
                    "outcome": "bad_shape", "products": 0})
                return [], False

        client = FlakyOzon()
        client._prefer_dom_search = True
        for expected_streak in (1, 2):
            with self.assertRaises(OzonUnavailable):
                client.search("наушники")
            self.assertEqual(client.raw_calls, 0)  # matrix not replayed
            self.assertTrue(client._prefer_dom_search)
            self.assertEqual(client._dom_empty_streak, expected_streak)
        with self.assertRaises(OzonUnavailable):
            client.search("наушники")
        self.assertGreater(client.raw_calls, 0)
        self.assertFalse(client._prefer_dom_search)
        self.assertEqual(client._dom_empty_streak, 0)
        self.assertEqual(client.dom_calls, 4)

    def test_search_falls_back_to_rendered_dom(self):
        class FakeResponse:
            status_code = 200

        class FakeBrowser:
            def rendered_page_payload(self, url, selector, js, scrolls=0):
                return {
                    "url": url,
                    "bodyText": "Ozon",
                    "items": [{
                        "id": 222,
                        "href": "https://www.ozon.ru/product/dom-222/",
                        "text": "500 ₽\n2 000 ₽\nDOM товар\n5.0 10 оценок",
                        "delText": "2 000 ₽",
                    }],
                }

        client = OzonClient(transport="chrome")
        client._browser_session = FakeBrowser()

        def bad_shape(endpoint, site_path, attempts):
            return FakeResponse(), {}, {
                "endpoint": endpoint, "transport": "chrome",
                "status": 200, "outcome": "ok", "elapsed_ms": 1,
            }

        client._request_json = bad_shape  # type: ignore[method-assign]
        products = client.search("тест")
        self.assertEqual([p["nm"] for p in products], [222])
        self.assertEqual(client.last_transport, "chrome-dom")
        self.assertTrue(client._prefer_dom_search)


class MarketplaceDatabaseTest(unittest.TestCase):
    @staticmethod
    def _product(marketplace: str, name: str) -> dict:
        return {
            "marketplace": marketplace, "nm": 777, "name": name,
            "brand": "B", "supplier": "", "photo": "",
            "url": ("https://www.wildberries.ru/catalog/777/detail.aspx"
                    if marketplace == "wb"
                    else "https://www.ozon.ru/product/test-777/"),
            "rating": 5, "feedbacks": 1,
            "price_rub": 5000.0, "sale_price_rub": 1000.0,
        }

    def test_old_wb_database_migrates_and_sku_namespaces_stay_separate(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = Path(d) / "old.db"
            raw = sqlite3.connect(path)
            raw.executescript("""
                CREATE TABLE products (
                    nm INTEGER PRIMARY KEY, name TEXT, brand TEXT,
                    supplier TEXT, photo TEXT, url TEXT, rating REAL,
                    feedbacks INTEGER, first_seen INTEGER, last_seen INTEGER,
                    last_price REAL, last_sale_price REAL);
                CREATE TABLE snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, nm INTEGER,
                    ts INTEGER, price REAL, sale_price REAL);
                CREATE TABLE alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, nm INTEGER,
                    ts INTEGER, name TEXT, brand TEXT, photo TEXT, url TEXT,
                    price REAL, sale_price REAL, discount_pct REAL,
                    drop_vs_median_pct REAL, hist_median REAL, saving REAL,
                    score INTEGER, level TEXT, reasons TEXT, source TEXT,
                    telegram_sent INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0,
                    raw TEXT);
            """)
            raw.execute(
                "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (777, "Старый WB", "B", "", "", "https://wb.test", 5, 1,
                 1, 2, 4000, 3000))
            raw.execute(
                "INSERT INTO snapshots (nm,ts,price,sale_price) VALUES (?,?,?,?)",
                (777, 10, 4000, 3000))
            raw.commit()
            raw.close()

            db = DB(path)
            try:
                self.assertEqual(db.products(marketplace="wb")[0]["name"],
                                 "Старый WB")
                db.upsert_product(self._product("ozon", "Новый Ozon"))
                stats = db.stats()
                self.assertEqual((stats["products_wb"], stats["products_ozon"]),
                                 (1, 1))
                self.assertEqual(
                    db.product_history(777, marketplace="wb")[0]["sale_price"],
                    3000)
                self.assertEqual(
                    db.product_history(777, marketplace="ozon")[0]["sale_price"],
                    1000)

                alert = {
                    **self._product("wb", "WB alert"),
                    "discount_pct": 80, "saving": 4000, "score": 90,
                    "level": "FIRE", "reasons": ["test"], "source": "live",
                }
                db.add_alert(alert)
                self.assertTrue(db.recent_alert(777, 1000, 6, marketplace="wb"))
                self.assertFalse(
                    db.recent_alert(777, 1000, 6, marketplace="ozon"))
                self.assertEqual(db.alerts(marketplace="wb")[0]["marketplace"], "wb")
                self.assertEqual(db.alerts(marketplace="ozon"), [])
            finally:
                db.close()

            check = sqlite3.connect(path)
            pk = [r[1] for r in check.execute("PRAGMA table_info(products)")
                  if r[5]]
            self.assertEqual(pk, ["marketplace", "nm"])
            check.close()

    def test_interrupted_migration_staging_table_is_recovered(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = Path(d) / "interrupted.db"
            raw = sqlite3.connect(path)
            raw.execute("""
                CREATE TABLE products_marketplace_v2 (
                    marketplace TEXT NOT NULL DEFAULT 'wb',
                    nm INTEGER NOT NULL, name TEXT, brand TEXT,
                    supplier TEXT, photo TEXT, url TEXT, rating REAL,
                    feedbacks INTEGER, first_seen INTEGER, last_seen INTEGER,
                    last_price REAL, last_sale_price REAL,
                    PRIMARY KEY (marketplace, nm))""")
            raw.execute(
                "INSERT INTO products_marketplace_v2 VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("wb", 777, "Recovered WB", "B", "", "", "https://wb.test",
                 5, 1, 1, 2, 4000, 3000))
            raw.commit()
            raw.close()

            db = DB(path)
            try:
                self.assertEqual(db.products(marketplace="wb")[0]["name"],
                                 "Recovered WB")
            finally:
                db.close()
            check = sqlite3.connect(path)
            tables = {r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("products_marketplace_v2", tables)
            check.close()


def _settings(**over) -> Settings:
    settings = Settings()
    settings._data.update({
        "mode": "live", "queries": ["тест"], "sorts": ["popular"],
        "pages_per_query": 1, "wb_enabled": True, "ozon_enabled": True,
        "wb_transport": "auto", "ozon_transport": "auto",
        "wb_request_delay_sec": 0, "ozon_request_delay_sec": 0,
        "telegram_enabled": False, "telegram_token": "",
        "telegram_chat_id": "", "verify_with_price_history": True,
    })
    settings._data.update(over)
    return settings


class _FakeWBAlert:
    def search(self, q, page=1, sort="popular"):
        return [{
            "marketplace": "wb", "nm": 888, "name": "WB ошибка цены",
            "brand": "B", "supplier": "", "price_rub": 10000.0,
            "sale_price_rub": 1000.0, "rating": 5, "feedbacks": 1000,
            "photo": "", "url": "https://www.wildberries.ru/catalog/888/detail.aspx",
        }]

    def history_median(self, nm):
        return 9500.0

    def probe(self, q="наушники"):
        return {"ok": True, "outcome": "ok", "message": "WB доступен",
                "attempts": [], "products": 1}

    def close(self):
        pass


class _CountingOzonDown:
    calls = 0

    def search(self, q, page=1, sort="popular"):
        self.calls += 1
        raise OzonUnavailable("Ozon blocked", outcome="blocked")

    def probe(self, q="наушники"):
        return {"ok": False, "outcome": "blocked", "marketplace": "ozon",
                "message": "Ozon blocked", "attempts": [], "products": 0}

    def close(self):
        pass


class _FakeWBDown:
    def search(self, q, page=1, sort="popular"):
        raise WBUnavailable("WB blocked", outcome="blocked")

    def history_median(self, nm):
        return None

    def probe(self, q="наушники"):
        return {"ok": False, "outcome": "blocked", "message": "WB blocked",
                "attempts": [], "products": 0}

    def close(self):
        pass


class _FakeOzonOK:
    def search(self, q, page=1, sort="popular"):
        return [{
            "marketplace": "ozon", "nm": 777, "sku": 777,
            "name": "Ozon товар", "brand": "B", "supplier": "",
            "price_rub": 5000.0, "sale_price_rub": 4500.0,
            "rating": 5, "feedbacks": 10, "photo": "",
            "url": "https://www.ozon.ru/product/test-777/",
        }]

    def history_median(self, sku):
        return None

    def probe(self, q="наушники"):
        return {"ok": True, "outcome": "ok", "marketplace": "ozon",
                "message": "Ozon доступен", "transport": "fake",
                "attempts": [], "products": 1}

    def close(self):
        pass


class _OrderCheckingOzon(_FakeOzonOK):
    db = None
    alerts_seen_on_fetch = None

    def search(self, q, page=1, sort="popular"):
        if self.db is not None:
            self.alerts_seen_on_fetch = self.db.stats()["alerts_total"]
        return super().search(q, page=page, sort=sort)


def _inject(scanner: Scanner, marketplace: str, client) -> None:
    cfg = scanner.settings.get()
    scanner._client_caches[marketplace] = client
    scanner._client_sigs[marketplace] = scanner._client_signature_for(
        cfg, marketplace)
    if marketplace == "wb":
        scanner._client_cache = client
        scanner._client_sig = scanner._client_sigs[marketplace]


class MarketplaceScannerTest(unittest.TestCase):
    def test_wb_failure_does_not_discard_ozon_products(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                scanner = Scanner(db, _settings())
                _inject(scanner, "wb", _FakeWBDown())
                _inject(scanner, "ozon", _FakeOzonOK())
                scanner.last_probe_ts = 999_999_999_999  # live: skip probes
                result = scanner.scan_once()
                self.assertEqual(result["scanned"], 1)
                self.assertEqual(result["scanned_ozon"], 1)
                self.assertEqual(result["request_failures"], 1)
                self.assertEqual(result["effective_mode"], "live")
                self.assertIn("Частично", result["error"])
                self.assertEqual(db.stats()["products_ozon"], 1)
                self.assertEqual(db.runs()[0]["source"], "live:wb+ozon")
            finally:
                scanner.close(close_browser=False)
                db.close()

    def test_auto_is_live_when_only_ozon_probe_succeeds(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                scanner = Scanner(db, _settings(mode="auto"))
                _inject(scanner, "wb", _FakeWBDown())
                _inject(scanner, "ozon", _FakeOzonOK())
                self.assertEqual(scanner.resolve_mode(force=True), "live")
                status = scanner.status()
                self.assertFalse(status["marketplaces"]["wb"]["available"])
                self.assertTrue(status["marketplaces"]["ozon"]["available"])
            finally:
                scanner.close(close_browser=False)
                db.close()

    def test_wb_products_are_processed_before_ozon_fetch(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                ozon = _OrderCheckingOzon()
                ozon.db = db
                scanner = Scanner(db, _settings())
                _inject(scanner, "wb", _FakeWBAlert())
                _inject(scanner, "ozon", ozon)
                scanner.last_probe_ts = 999_999_999_999
                result = scanner.scan_once()
                self.assertEqual(result["alerts"], 1)
                self.assertEqual(ozon.alerts_seen_on_fetch, 1)
            finally:
                scanner.close(close_browser=False)
                db.close()

    def test_history_only_scan_fetches_baseline_without_big_sticker(self):
        class HistoryOnlyWB:
            history_calls = 0

            def search(self, q, page=1, sort="popular"):
                return [{
                    "marketplace": "wb", "nm": 999,
                    "name": "WB товар с историей", "brand": "B",
                    "supplier": "", "price_rub": 10000.0,
                    "sale_price_rub": 6000.0, "rating": 5,
                    "feedbacks": 500, "photo": "",
                    "url": "https://www.wildberries.ru/catalog/999/detail.aspx",
                }]

            def history_stats(self, nm):
                self.history_calls += 1
                return {"count": 30, "median": 10000.0,
                        "min": 6500.0, "max": 11000.0}

            def probe(self, q="наушники"):
                return {"ok": True, "outcome": "ok", "message": "WB доступен",
                        "attempts": [], "products": 1}

            def close(self):
                pass

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                wb = HistoryOnlyWB()
                scanner = Scanner(db, _settings(
                    ozon_enabled=False, history_only=True))
                _inject(scanner, "wb", wb)
                scanner.last_probe_ts = 999_999_999_999
                result = scanner.scan_once()
                self.assertEqual(wb.history_calls, 1)
                self.assertEqual(result["alerts"], 1)
                alert = db.alerts(limit=1)[0]
                self.assertEqual(alert["drop_vs_median_pct"], 40.0)
                self.assertEqual(alert["saving"], 4000.0)
                self.assertEqual(alert["hist_min"], 6500.0)
                self.assertGreater(alert["vs_hist_min_pct"], 0)
            finally:
                scanner.close(close_browser=False)
                db.close()

    def test_cycle_interleaves_queries_between_marketplaces(self):
        # Regression: fetching all of WB before Ozon let a long WB DOM
        # pass (16 queries + price-history processing) starve Ozon for
        # entire app sessions — the user's Ozon DB stayed at 0 while
        # Ozon diagnostics passed.  Queries must alternate by source.
        calls: list[tuple[str, str]] = []

        class RecorderWB(_FakeWBAlert):
            def search(self, q, page=1, sort="popular"):
                calls.append(("wb", q))
                return super().search(q, page=page, sort=sort)

        class RecorderOzon(_FakeOzonOK):
            def search(self, q, page=1, sort="popular"):
                calls.append(("ozon", q))
                return super().search(q, page=page, sort=sort)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                scanner = Scanner(db, _settings(queries=["q1", "q2"]))
                _inject(scanner, "wb", RecorderWB())
                _inject(scanner, "ozon", RecorderOzon())
                scanner.last_probe_ts = 999_999_999_999
                result = scanner.scan_once()
                self.assertEqual(
                    calls,
                    [("wb", "q1"), ("ozon", "q1"),
                     ("wb", "q2"), ("ozon", "q2")])
                self.assertEqual(result["scanned_ozon"], 1)
                self.assertEqual(db.stats()["products_ozon"], 1)
                ozon_status = scanner.status()["marketplaces"]["ozon"]
                self.assertEqual(ozon_status["last_products"], 1)
                self.assertIsNotNone(ozon_status["last_scan_ts"])
            finally:
                scanner.close(close_browser=False)
                db.close()

    def test_auto_skips_known_down_source_until_reprobe_is_due(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                ozon = _CountingOzonDown()
                scanner = Scanner(db, _settings(mode="auto"))
                _inject(scanner, "wb", _FakeWBAlert())
                _inject(scanner, "ozon", ozon)
                scanner.effective_mode = "live"
                scanner._state("wb")["available"] = True
                scanner._state("ozon").update(
                    available=False, next_probe_ts=999_999_999_999,
                    last_error="Ozon blocked")
                result = scanner.scan_once()
                self.assertEqual(ozon.calls, 0)
                self.assertEqual(result["scanned"], 1)
                self.assertEqual(result["marketplaces"]["ozon"]["skipped"], 1)
            finally:
                scanner.close(close_browser=False)
                db.close()


class LowestPriceSearchTest(unittest.TestCase):
    @staticmethod
    def _product(marketplace: str, nm: int, name: str, sale: float,
                 basic: float | None = None) -> dict:
        return {
            "marketplace": marketplace, "nm": nm,
            **({"sku": nm} if marketplace == "ozon" else {}),
            "name": name, "brand": "Apple", "supplier": "Seller",
            "price_rub": basic or sale, "sale_price_rub": sale,
            "rating": 4.9, "feedbacks": 100, "photo": "",
            "url": (f"https://www.ozon.ru/product/iphone-{nm}/"
                    if marketplace == "ozon"
                    else f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"),
        }

    def test_cross_marketplace_lowest_excludes_accessories_and_wrong_variant(self):
        product = self._product

        class FakeWB:
            def __init__(self):
                self.sorts = []

            def search(self, q, page=1, sort="popular"):
                self.sorts.append(sort)
                return [
                    product("wb", 2001, "Чехол для iPhone 15 128 ГБ", 500),
                    product("wb", 2002, "Смартфон iPhone 15 128 ГБ", 65_000),
                    product("wb", 2003, "Смартфон iPhone 15 128 ГБ", 63_000),
                ]

        class FakeOzon:
            def __init__(self):
                self.sorts = []

            def search(self, q, page=1, sort="popular"):
                self.sorts.append(sort)
                return [
                    product("ozon", 3001, "Смартфон iPhone 15 Pro 128 ГБ", 60_000),
                    product("ozon", 3002, "Смартфон iPhone 15 128 ГБ", 62_000),
                ]

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                scanner = Scanner(db, _settings())
                wb, ozon = FakeWB(), FakeOzon()
                _inject(scanner, "wb", wb)
                _inject(scanner, "ozon", ozon)
                scanner.last_probe_ts = 999_999_999_999  # live: skip probes
                result = scanner.find_lowest_prices(
                    "iPhone 15 128 ГБ", pages=1, record=True)

                self.assertTrue(result["ok"])
                self.assertEqual(wb.sorts, ["priceup", "popular"])
                self.assertEqual(ozon.sorts, ["priceup", "popular"])
                self.assertEqual(
                    [offer["sale_price_rub"] for offer in result["offers"]],
                    [62_000.0, 63_000.0, 65_000.0])
                self.assertEqual(result["cheapest"]["marketplace"], "ozon")
                self.assertEqual(result["cheapest"]["nm"], 3002)
                self.assertEqual(
                    result["by_marketplace"]["wb"]["cheapest"]["nm"], 2003)
                self.assertEqual(result["excluded_accessories"], 1)
                self.assertTrue(all(
                    offer["match_label"] == "точное"
                    for offer in result["offers"]))
                self.assertEqual(result["recorded"], 3)
                self.assertEqual(db.stats()["products_seen"], 3)
                self.assertEqual(db.stats()["alerts_total"], 0)
            finally:
                scanner.close(close_browser=False)
                db.close()

    def test_strict_lowest_search_rejects_close_variant(self):
        product = self._product

        class FakeWB:
            def search(self, q, page=1, sort="popular"):
                return [product(
                    "wb", 4001, "Смартфон iPhone 15 Pro 128 ГБ", 60_000)]

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            try:
                scanner = Scanner(db, _settings())
                _inject(scanner, "wb", FakeWB())
                scanner.last_probe_ts = 999_999_999_999
                strict = scanner.find_lowest_prices(
                    "iPhone 15 128 ГБ", marketplaces=["wb"], pages=1,
                    strict_match=True, record=False)
                relaxed = scanner.find_lowest_prices(
                    "iPhone 15 128 ГБ", marketplaces=["wb"], pages=1,
                    strict_match=False, record=False)
                self.assertEqual(strict["offers"], [])
                self.assertEqual(len(relaxed["offers"]), 1)
                self.assertEqual(relaxed["offers"][0]["match_label"], "близкое")
            finally:
                scanner.close(close_browser=False)
                db.close()


class MarketplacePresentationTest(unittest.TestCase):
    def test_telegram_labels_ozon_and_sku(self):
        text = alert_text({
            "level": "FIRE", "score": 91, "brand": "Sony",
            "name": "Наушники", "sale_price_rub": 1000, "price_rub": 5000,
            "discount_pct": 80, "saving": 4000, "reasons": ["тест"],
            "marketplace": "ozon", "nm": 777, "sku": 777,
            "url": "https://www.ozon.ru/product/test-777/",
            "source": "live",
        })
        self.assertIn("Ozon", text)
        self.assertIn("SKU Ozon", text)
        self.assertNotIn("Артикул WB", text)

    def test_controller_dispatches_ozon_diagnostics_and_history(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = DB(Path(d) / "t.db")
            ctrl = DesktopController(settings=_settings(mode="demo"), db=db)
            try:
                seen = {}

                def fake_diagnose(query=None, marketplace="wb"):
                    seen.update(query=query, marketplace=marketplace)
                    return {"ok": True, "marketplace": marketplace}

                ctrl.scanner.diagnose = fake_diagnose  # type: ignore[method-assign]
                self.assertEqual(ctrl.diagnose_ozon("телефон")["marketplace"],
                                 "ozon")
                self.assertEqual(seen, {"query": "телефон", "marketplace": "ozon"})
                db.upsert_product({
                    "marketplace": "ozon", "nm": 777, "name": "Ozon",
                    "brand": "", "supplier": "", "photo": "",
                    "url": "https://www.ozon.ru/product/x-777/",
                    "price_rub": 100, "sale_price_rub": 50,
                })
                self.assertEqual(
                    len(ctrl.product_history(777, marketplace="ozon")), 1)
                self.assertEqual(
                    len(ctrl.product_history(777, marketplace="wb")), 0)
            finally:
                ctrl.shutdown(close_browser=False)


if __name__ == "__main__":
    unittest.main()
