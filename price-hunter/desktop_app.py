"""Price Hunter — Windows desktop app entry point.

Usage:
  python desktop_app.py              # GUI (starts hunting immediately)
  python desktop_app.py --minimized  # start in the tray (used by autostart)
  python desktop_app.py --self-test  # headless: one demo scan, prints JSON
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


def self_test() -> int:
    """Headless engine check (no display needed): force one demo scan."""
    _setup_logging()
    from desktop.controller import DesktopController
    ctrl = DesktopController()
    ctrl.scanner.effective_mode = "demo"   # deterministic, offline
    result = ctrl.scanner.scan_once()
    result["alerts_in_db"] = len(ctrl.alerts(50))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("scanned", 0) > 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Price Hunter desktop app")
    ap.add_argument("--minimized", action="store_true",
                    help="start hidden in the tray")
    ap.add_argument("--self-test", action="store_true",
                    help="run one headless demo scan and exit")
    ap.add_argument("--gui-smoke", action="store_true",
                    help="open the window and close it after ~1s (CI)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

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
