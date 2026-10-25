"""Price Hunter — Windows desktop app entry point.

Usage:
  python desktop_app.py              # GUI (starts hunting immediately)
  python desktop_app.py --minimized  # start in the tray (used by autostart)
  python desktop_app.py --self-test  # headless: one demo scan, prints JSON
  python desktop_app.py --diagnose-wb  # WB connectivity verdict (JSON + file)
  python desktop_app.py --diagnose-ozon  # Ozon connectivity verdict
  python desktop_app.py --diagnose-all  # enabled WB/Ozon verdicts
  python desktop_app.py --open-wb-browser  # open Chrome/Edge on WB
  python desktop_app.py --open-ozon-browser  # open Chrome/Edge on Ozon
  python desktop_app.py --gui-smoke  # CI: build the window, close after 1s
  PriceHunter.exe                    # packaged build, same flags work

Data (settings.json, price DB, log) for the packaged .exe lives in
%APPDATA%\\PriceHunter so it survives exe updates and never needs admin
rights; from sources it stays in ./data next to the project.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def _setup_data_dir() -> Path:
    """Must run BEFORE anything imports app.config (it freezes DATA_DIR)."""
    if os.environ.get("PRICE_HUNTER_DATA_DIR"):
        data = Path(os.environ["PRICE_HUNTER_DATA_DIR"])
    elif getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or str(Path.home())
        data = Path(base) / "PriceHunter"
        os.environ["PRICE_HUNTER_DATA_DIR"] = str(data)
    else:
        data = Path(__file__).resolve().parent / "data"
    data.mkdir(parents=True, exist_ok=True)
    return data


DATA_DIR = _setup_data_dir()


def resource_path(rel: str) -> Path:
    """Path to a bundled asset, both from sources and inside the .exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


def _setup_logging() -> None:
    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(
            DATA_DIR / "price_hunter.log", encoding="utf-8"))
    except Exception:
        pass
    if sys.stderr:  # frozen windowed exe has no stderr stream
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _configure_stdout() -> None:
    """Windows CI consoles are often cp1252 and crash on Cyrillic JSON."""
    if sys.stdout:
        try:
            sys.stdout.reconfigure(encoding="utf-8",
                                   errors="backslashreplace")
        except Exception:
            pass


def self_test() -> int:
    """Headless engine check (no display needed): force one demo scan."""
    _setup_logging()
    os.environ["HUNTER_MODE"] = "demo"   # Settings reads this on init
    from desktop.controller import DesktopController
    ctrl = DesktopController()
    try:
        result = ctrl.scanner.scan_once()
        result["alerts_in_db"] = len(ctrl.alerts(50))
    finally:
        ctrl.shutdown()
    _configure_stdout()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("scanned", 0) > 0 else 1


def _write_diagnostics(name: str, result: dict) -> str:
    text = json.dumps(result, ensure_ascii=False, indent=2)
    try:
        (DATA_DIR / name).write_text(text, encoding="utf-8")
    except Exception:
        pass
    return text


def diagnose_marketplace(marketplace: str, query: str | None = None) -> int:
    """Print + persist one marketplace verdict (support artifact)."""
    _setup_logging()
    from desktop.controller import DesktopController
    ctrl = DesktopController()
    try:
        result = ctrl.diagnose_marketplace(marketplace, query)
    finally:
        # Keep a browser window opened by diagnostics alive so the user
        # can finish its check there and rerun this command.
        ctrl.shutdown(close_browser=False)
    filename = ("wb-diagnostics.json" if marketplace == "wb"
                else "ozon-diagnostics.json")
    text = _write_diagnostics(filename, result)
    if sys.stdout:
        _configure_stdout()
        print(text)
    return 0 if result.get("ok") else 2


def diagnose_wb(query: str | None = None) -> int:
    return diagnose_marketplace("wb", query)


def diagnose_ozon(query: str | None = None) -> int:
    return diagnose_marketplace("ozon", query)


def diagnose_all(query: str | None = None) -> int:
    """Print + persist independent verdicts for enabled marketplaces."""
    _setup_logging()
    from desktop.controller import DesktopController
    ctrl = DesktopController()
    try:
        result = ctrl.diagnose_all(query)
    finally:
        ctrl.shutdown(close_browser=False)
    text = _write_diagnostics("marketplace-diagnostics.json", result)
    for marketplace, one in (result.get("results") or {}).items():
        if isinstance(one, dict):
            _write_diagnostics(f"{marketplace}-diagnostics.json", one)
    if sys.stdout:
        _configure_stdout()
        print(text)
    return 0 if result.get("ok") else 2


def open_marketplace_browser(marketplace: str) -> int:
    """Open the shared Chrome/Edge on one marketplace and leave it open."""
    _setup_logging()
    from desktop.controller import DesktopController
    ctrl = DesktopController()
    exit_code = 0
    try:
        try:
            result = ctrl.open_marketplace_browser(marketplace)
        except Exception as e:
            result = {"ok": False, "marketplace": marketplace, "error": str(e)}
            exit_code = 2
    finally:
        ctrl.shutdown(close_browser=False)
    if sys.stdout:
        _configure_stdout()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


def open_wb_browser() -> int:
    return open_marketplace_browser("wb")


def open_ozon_browser() -> int:
    return open_marketplace_browser("ozon")


def main() -> int:
    ap = argparse.ArgumentParser(description="Price Hunter desktop app")
    ap.add_argument("--minimized", action="store_true",
                    help="start hidden in the tray")
    ap.add_argument("--self-test", action="store_true",
                    help="run one headless demo scan and exit")
    ap.add_argument("--gui-smoke", action="store_true",
                    help="open the window and close it after ~1s (CI)")
    ap.add_argument("--diagnose-wb", nargs="?", const="__default__",
                    default=None, metavar="QUERY",
                    help="check WB connectivity, print JSON verdict and exit")
    ap.add_argument("--diagnose-ozon", nargs="?", const="__default__",
                    default=None, metavar="QUERY",
                    help="check Ozon connectivity, print JSON verdict and exit")
    ap.add_argument("--diagnose-all", nargs="?", const="__default__",
                    default=None, metavar="QUERY",
                    help="check every enabled marketplace and exit")
    ap.add_argument("--open-wb-browser", action="store_true",
                    help="open the dedicated Chrome/Edge window for WB")
    ap.add_argument("--open-ozon-browser", action="store_true",
                    help="open the dedicated Chrome/Edge window for Ozon")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.diagnose_wb is not None:
        return diagnose_wb(None if args.diagnose_wb == "__default__"
                           else args.diagnose_wb)
    if args.diagnose_ozon is not None:
        return diagnose_ozon(None if args.diagnose_ozon == "__default__"
                             else args.diagnose_ozon)
    if args.diagnose_all is not None:
        return diagnose_all(None if args.diagnose_all == "__default__"
                            else args.diagnose_all)
    if args.open_wb_browser:
        return open_wb_browser()
    if args.open_ozon_browser:
        return open_ozon_browser()

    _setup_logging()
    from desktop.controller import DesktopController
    from desktop.gui import run_app
    ctrl = DesktopController()
    ico = resource_path("assets/icon.ico")
    png = resource_path("assets/icon.png")
    icon = str(ico) if ico.exists() else (str(png) if png.exists() else None)
    return run_app(ctrl, minimized=args.minimized, icon_path=icon,
                   smoke=args.gui_smoke)


if __name__ == "__main__":
    raise SystemExit(main())
