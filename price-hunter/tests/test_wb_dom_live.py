"""Optional real-browser CDP smoke test.

Skipped in normal/offline runs.  Windows CI sets
``PRICE_HUNTER_BROWSER_SMOKE=1`` and runs this module explicitly against
the runner's installed Edge/Chrome.  It navigates only to a local
``data:`` page resembling a rendered WB tile; it does not call WB.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


@unittest.skipUnless(os.environ.get("PRICE_HUNTER_BROWSER_SMOKE") == "1",
                     "set PRICE_HUNTER_BROWSER_SMOKE=1 to launch a browser")
class RealBrowserSmokeTest(unittest.TestCase):
    def test_cdp_navigation_and_tile_extraction(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            session = ChromeBrowserSession(
                profile_dir=str(Path(d) / "profile"), port=9333,
                headless=True, wait_sec=30, timeout=30)
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
            finally:
                session.terminate()


if __name__ == "__main__":
    unittest.main()
