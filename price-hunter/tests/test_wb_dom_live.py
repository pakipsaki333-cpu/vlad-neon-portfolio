"""Optional real-browser CDP smoke test.

Skipped in normal/offline runs.  Windows CI sets
``PRICE_HUNTER_BROWSER_SMOKE=1`` and runs this module explicitly against
the runner's installed Edge/Chrome.  It navigates only to local
``data:`` pages resembling rendered WB and Ozon tiles; it does not call
either marketplace search.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ozon_client import (  # noqa: E402
    _OZON_SEARCH_EXTRACT_JS, parse_ozon_dom_items)
from app.wb_dom import (ChromeBrowserSession, _SEARCH_EXTRACT_JS,  # noqa: E402
                        parse_dom_search_items)

HTML = """
<html><body><article>
<a href="https://www.wildberries.ru/catalog/123456/detail.aspx"
   aria-label="Apple / iPhone 16 128 ГБ">
<div>-71%</div><div>9 990 ₽</div><del>33 990 ₽</del>
<div>Apple / iPhone 16 128 ГБ</div><div>4,9 • 1 234 оценки</div>
</a></article></body></html>
"""

HTML_OZON = """
<html><body><article>
<a href="https://www.ozon.ru/product/naushniki-987654/"
   aria-label="Наушники Ozon"><img alt="Наушники Ozon"></a>
<div>1 299 ₽</div><del>4 999 ₽</del>
<div>Наушники Ozon</div><div>4,8 • 532 отзыва</div>
</article></body></html>
"""


@unittest.skipUnless(os.environ.get("PRICE_HUNTER_BROWSER_SMOKE") == "1",
                     "set PRICE_HUNTER_BROWSER_SMOKE=1 to launch a browser")
class RealBrowserSmokeTest(unittest.TestCase):
    def test_cdp_navigation_and_tile_extraction(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            profile = str(Path(d) / "profile")
            session = ChromeBrowserSession(
                profile_dir=profile, port=9333,
                headless=True, wait_sec=30, timeout=30)
            session2 = None
            try:
                session._connect()
                session.navigate(
                    "data:text/html;charset=utf-8," + quote(HTML),
                    wait_selector='a[href*="detail.aspx"]')
                payload = session.evaluate(_SEARCH_EXTRACT_JS)
                products = parse_dom_search_items(payload.get("items") or [])
                self.assertEqual(len(products), 1)
                self.assertEqual(products[0]["nm"], 123456)
                self.assertEqual(products[0]["sale_price_rub"], 9990.0)
                self.assertEqual(products[0]["price_rub"], 33990.0)

                # A second marketplace session on the same browser/profile
                # must get its OWN tab, never navigate the first tab away
                # (regression: opening Ozon flipped straight back to WB).
                session2 = ChromeBrowserSession(
                    profile_dir=profile, port=9333, headless=True,
                    wait_sec=30, timeout=30, home_url="about:blank")
                session2._connect()
                self.assertIsNotNone(session2._target_id)
                self.assertNotEqual(session2._target_id, session._target_id)
                session2.navigate(
                    "data:text/html;charset=utf-8," + quote(HTML_OZON),
                    wait_selector='a[href*="/product/"]')
                payload = session2.evaluate(_OZON_SEARCH_EXTRACT_JS)
                products = parse_ozon_dom_items(payload.get("items") or [])
                self.assertEqual(len(products), 1)
                self.assertEqual(products[0]["marketplace"], "ozon")
                self.assertEqual(products[0]["sku"], 987654)
                self.assertEqual(products[0]["sale_price_rub"], 1299.0)
                self.assertEqual(products[0]["price_rub"], 4999.0)
                # The first tab still shows its own page afterwards.
                self.assertIn("detail.aspx",
                              str(session.evaluate("location.href")))
            finally:
                if session2 is not None:
                    session2.terminate()
                session.terminate()


if __name__ == "__main__":
    unittest.main()
