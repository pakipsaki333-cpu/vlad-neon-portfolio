"""Scanner: poll WB search by queries, record prices, detect anomalies,
fire Telegram alerts. Runs in a background thread with start/stop control.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from . import demo, detector
from .config import Settings
from .db import DB
from .telegram import TelegramNotifier
from .wb_client import WBClient, WBUnavailable

log = logging.getLogger("scanner")


class Scanner:
    def __init__(self, db: DB, settings: Settings):
        self.db = db
        self.settings = settings
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._scan_now = threading.Event()
        self.running = False
        self.scanning = False
        self.effective_mode = "—"          # 'live' | 'demo' after first probe
        self.last_scan_ts: int | None = None
        self.next_scan_ts: int | None = None
        self.last_error: str | None = None
        self._listeners: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------------ #
    def on_alert(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def _emit(self, alert: dict) -> None:
        for cb in self._listeners:
            try:
                cb(alert)
            except Exception:
                pass

    def _client(self) -> WBClient:
        cfg = self.settings.get()
        return WBClient(dest=cfg["dest"], spp=cfg["spp"])

    def resolve_mode(self) -> str:
        mode = self.settings.get().get("mode", "auto")
        if mode in ("live", "demo"):
            self.effective_mode = mode
            return mode
        # auto: probe WB once; sandbox / blocked network -> demo.
        ok = self._client().ping()
        self.effective_mode = "live" if ok else "demo"
        log.info("auto mode -> %s (WB reachable: %s)", self.effective_mode, ok)
        return self.effective_mode

    # ------------------------------------------------------------------ #
    def scan_once(self) -> dict:
        cfg = self.settings.get()
        mode = self.effective_mode if self.effective_mode in ("live", "demo") \
            else self.resolve_mode()
        run_id = self.db.start_run(mode, len(cfg["queries"]))
        scanned = new_products = alerts_made = 0
        error = None
        self.scanning = True
        try:
            products: list[dict[str, Any]] = []
            wb = None
            if mode == "live":
                wb = self._client()
                seen: set[int] = set()
                for q in cfg["queries"]:
                    if self._stop.is_set():
                        break
                    for sort in cfg.get("sorts", ["popular"]):
                        for page in range(1, cfg["pages_per_query"] + 1):
                            try:
                                batch = wb.search(q, page=page, sort=sort)
                            except WBUnavailable as e:
                                raise RuntimeError(str(e)) from e
                            for p in batch:
                                if p["nm"] not in seen:
                                    seen.add(p["nm"])
                                    products.append(p)
                            time.sleep(0.7)  # be polite to WB
            else:
                products = demo.generate_scan(cfg["queries"])

            notifier = TelegramNotifier(cfg["telegram_token"], cfg["telegram_chat_id"])
            for p in products:
                scanned += 1
                if self.db.upsert_product(p):
                    new_products += 1
                own = self.db.history_stats(p["nm"])
                hist_median = None
                # Only spend an extra request on likely candidates.
                discount = 0.0
                if p["price_rub"] > p["sale_price_rub"]:
                    discount = (p["price_rub"] - p["sale_price_rub"]) / p["price_rub"] * 100
                likely = discount >= cfg["min_discount_pct"] or (
                    own.get("median") and p["sale_price_rub"] < own["median"] * 0.7)
                if likely and cfg.get("verify_with_price_history"):
                    if mode == "live" and wb is not None:
                        hist_median = wb.history_median(p["nm"])
                    elif mode == "demo":
                        hist_median = demo.demo_history_median(p["nm"])

                alert = detector.evaluate(p, own, hist_median, cfg)
                if not alert:
                    continue
                if self.db.recent_alert(p["nm"], p["sale_price_rub"],
                                         cfg["alert_cooldown_hours"]):
                    continue
                alert["source"] = mode
                alert["photo"] = p.get("photo") or ""
                alert["raw"] = {**p, "detected_at": int(time.time()),
                                "own_history": own, "wb_hist_median": hist_median}
                alert_id = self.db.add_alert(alert)
                alert["id"] = alert_id
                alert["ts"] = int(time.time())
                alerts_made += 1
                log.info("ALERT nm=%s %s₽ -%.0f%% score=%s (%s)",
                         p["nm"], p["sale_price_rub"], alert["discount_pct"],
                         alert["score"], mode)
                if cfg.get("telegram_enabled") and notifier.configured:
                    if notifier.send_alert(alert):
                        self.db.mark_telegram_sent(alert_id)
                        alert["telegram_sent"] = 1
                self._emit(alert)
        except Exception as e:
            error = str(e)[:500]
            self.last_error = error
            log.warning("scan failed: %s", e)
            if mode == "live" and self.settings.get().get("mode") == "auto":
                # WB died mid-flight (throttle/block) - degrade to demo visibly
                # rather than silently producing nothing.
                self.effective_mode = "demo"
                log.warning("falling back to DEMO mode")
        finally:
            self.scanning = False
            self.last_scan_ts = int(time.time())
            self.db.finish_run(run_id, scanned, new_products, alerts_made, error)
        return {"scanned": scanned, "new": new_products,
                "alerts": alerts_made, "error": error, "mode": mode}

    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        self.resolve_mode()
        while not self._stop.is_set():
            self.scan_once()
            interval = int(self.settings.get()["interval_sec"])
            # Demo: scan more often so the preview feels alive.
            if self.effective_mode == "demo":
                interval = min(interval, 45)
            self.next_scan_ts = int(time.time()) + interval
            self._scan_now.wait(timeout=interval)
            self._scan_now.clear()
        self.running = False
        self.next_scan_ts = None

    def start(self) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        if not self.running:
            return False
        self._stop.set()
        self._scan_now.set()
        return True

    def trigger(self) -> None:
        if self.running:
            self._scan_now.set()
        else:
            threading.Thread(target=self.scan_once, daemon=True).start()

    def status(self) -> dict:
        return {
            "running": self.running,
            "scanning": self.scanning,
            "mode": self.settings.get().get("mode", "auto"),
            "effective_mode": self.effective_mode,
            "last_scan_ts": self.last_scan_ts,
            "next_scan_ts": self.next_scan_ts if self.running else None,
            "last_error": self.last_error,
        }
