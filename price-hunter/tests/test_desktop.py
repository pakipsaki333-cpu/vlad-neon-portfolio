"""Desktop controller tests (headless — no tkinter/display needed).

Run from price-hunter/:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULTS, Settings  # noqa: E402
from app.db import DB  # noqa: E402
from desktop import autostart  # noqa: E402
from desktop.controller import DesktopController, parse_settings_form  # noqa: E402

EXISTING = dict(DEFAULTS, telegram_token="111:old-secret", telegram_chat_id="42")


def form(**over):
    raw = {
        "mode": "auto", "queries": "iphone\nнаушники sony\n",
        "sorts": ["popular", "benefit", "bogus"],
        "pages_per_query": "2", "interval_sec": "30",
        "min_discount_pct": "65", "min_drop_vs_history_pct": "35",
        "min_original_price_rub": "1500", "min_saving_rub": "1000",
        "min_score": "35", "alert_cooldown_hours": "6",
        "dest": "-1257786", "spp": "30",
        "wb_transport": "curl_cffi", "wb_request_delay_sec": "1.5",
        "wb_enabled": True, "ozon_enabled": "true",
        "ozon_transport": "dom", "ozon_request_delay_sec": "3.5",
        "ozon_browser_fallback": "yes",
        "live_probe_interval_sec": "300", "wb_browser_fallback": True,
        "wb_browser_path": "  C:/Program Files/Google/Chrome/Application/chrome.exe  ",
        "wb_browser_port": "9229", "wb_browser_wait_sec": "999",
        "wb_browser_headless": False,
        "verify_with_price_history": True, "telegram_enabled": True,
        "close_to_tray": True, "start_minimized": False,
        "telegram_token": "", "telegram_chat_id": "42",
    }
    raw.update(over)
    return raw


class ParseFormTest(unittest.TestCase):
    def test_empty_token_keeps_saved(self):
        out = parse_settings_form(form(), EXISTING)
        self.assertEqual(out["telegram_token"], "111:old-secret")

    def test_new_token_replaces(self):
        out = parse_settings_form(form(telegram_token=" 222:new "), EXISTING)
        self.assertEqual(out["telegram_token"], "222:new")

    def test_queries_and_sorts_normalized(self):
        out = parse_settings_form(form(), EXISTING)
        self.assertEqual(out["queries"], ["iphone", "наушники sony"])
        self.assertEqual(out["sorts"], ["popular", "benefit"])  # bogus dropped

    def test_interval_clamped_to_60(self):
        out = parse_settings_form(form(interval_sec="5"), EXISTING)
        self.assertEqual(out["interval_sec"], 60)

    def test_wb_transport_normalized(self):
        out = parse_settings_form(form(), EXISTING)
        self.assertEqual(out["wb_transport"], "browser")  # curl_cffi alias
        self.assertEqual(out["wb_request_delay_sec"], 1.5)
        chrome = parse_settings_form(form(wb_transport="dom"), EXISTING)
        self.assertEqual(chrome["wb_transport"], "chrome")
        self.assertTrue(chrome["wb_browser_fallback"])
        self.assertTrue(chrome["wb_browser_path"].endswith("chrome.exe"))
        self.assertEqual(chrome["wb_browser_port"], 9229)
        self.assertEqual(chrome["wb_browser_wait_sec"], 120)
        with self.assertRaises(ValueError):
            parse_settings_form(form(wb_transport="telepathy"), EXISTING)

    def test_marketplaces_and_ozon_transport_normalized(self):
        out = parse_settings_form(form(), EXISTING)
        self.assertTrue(out["wb_enabled"])
        self.assertTrue(out["ozon_enabled"])
        self.assertEqual(out["ozon_transport"], "chrome")  # dom alias
        self.assertEqual(out["ozon_request_delay_sec"], 3.5)
        self.assertTrue(out["ozon_browser_fallback"])
        with self.assertRaises(ValueError):
            parse_settings_form(
                form(wb_enabled=False, ozon_enabled=False), EXISTING)
        with self.assertRaises(ValueError):
            parse_settings_form(form(ozon_transport="telepathy"), EXISTING)

    def test_empty_queries_rejected(self):
        with self.assertRaises(ValueError):
            parse_settings_form(form(queries="  \n "), EXISTING)

    def test_bad_number_rejected(self):
        with self.assertRaises(ValueError):
            parse_settings_form(form(min_score="abc"), EXISTING)

    def test_chat_without_any_token_rejected(self):
        with self.assertRaises(ValueError):
            parse_settings_form(form(), dict(DEFAULTS))  # no saved token either


class AutostartTest(unittest.TestCase):
    def test_unsupported_platforms_degrade(self):
        if sys.platform == "win32":
            self.skipTest("windows: registry path covered by manual check")
        self.assertFalse(autostart.is_supported())
        self.assertFalse(autostart.is_enabled())
        self.assertFalse(autostart.set_enabled(True))


class ControllerTest(unittest.TestCase):
    def test_demo_scan_end_to_end(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            settings = Settings()
            settings._data["mode"] = "demo"  # in-memory only, no network probe
            ctrl = DesktopController(settings=settings,
                                     db=DB(Path(d) / "t.db"))
            result = ctrl.scanner.scan_once()
            self.assertGreater(result["scanned"], 0)
            st = ctrl.status()
            self.assertEqual(st["stats"]["products_seen"], result["scanned"])
            ctrl.shutdown()

    def test_telegram_test_not_configured(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            ctrl = DesktopController(settings=Settings(),
                                     db=DB(Path(d) / "t.db"))
            ok, msg = ctrl.telegram_test("", "")
            # CI/dev env may export TELEGRAM_* — only assert shape then.
            if not ctrl.settings.public()["telegram_configured"]:
                self.assertFalse(ok)
            self.assertTrue(msg)
            ctrl.shutdown()


if __name__ == "__main__":
    unittest.main()
