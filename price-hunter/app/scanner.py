"""Scanner: poll WB search by queries, record prices, detect anomalies,
fire Telegram alerts. Runs in a background thread with start/stop control.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from . import demo, detector
from .config import DATA_DIR, Settings
from .db import DB
from .telegram import TelegramNotifier
from .wb_client import WBClient, WBUnavailable, curl_cffi_available
from .wb_dom import browser_info

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
        self.mode_reason: str | None = None
        self.last_probe: dict | None = None
        self.last_probe_ts: int | None = None
        self.next_live_probe_ts: int | None = None
        self.live_failures = 0
        self.last_scan_ts: int | None = None
        self.next_scan_ts: int | None = None
        self.last_error: str | None = None
        self._listeners: list[Callable[[dict], None]] = []
        self._client_cache: WBClient | None = None
        self._client_sig: tuple | None = None
        self._probe_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def on_alert(self, cb: Callable[[dict], None]) -> None:
        self._listeners.append(cb)

    def _emit(self, alert: dict) -> None:
        for cb in self._listeners:
            try:
                cb(alert)
            except Exception:
                pass

    def _new_client(self) -> WBClient:
        cfg = self.settings.get()
        return WBClient(
            dest=cfg["dest"], spp=cfg["spp"],
            transport=cfg.get("wb_transport", "auto"),
            browser_fallback=bool(cfg.get("wb_browser_fallback", True)),
            browser_executable=str(cfg.get("wb_browser_path") or ""),
            browser_profile_dir=str(DATA_DIR / "browser-profile"),
            browser_port=int(cfg.get("wb_browser_port", 9229) or 9229),
            browser_headless=bool(cfg.get("wb_browser_headless", False)),
            browser_wait_sec=float(cfg.get("wb_browser_wait_sec", 25) or 25),
        )

    @staticmethod
    def _client_signature(cfg: dict) -> tuple:
        return (cfg.get("dest"), cfg.get("spp"),
                cfg.get("wb_transport", "auto"),
                bool(cfg.get("wb_browser_fallback", True)),
                str(cfg.get("wb_browser_path") or ""),
                int(cfg.get("wb_browser_port", 9229) or 9229),
                bool(cfg.get("wb_browser_headless", False)),
                float(cfg.get("wb_browser_wait_sec", 25) or 25))

    def _client(self) -> WBClient:
        """Cached scan-thread client (keeps session cookies and the
        endpoint that last worked between scans)."""
        cfg = self.settings.get()
        sig = self._client_signature(cfg)
        if self._client_cache is None or sig != self._client_sig:
            if self._client_cache is not None:
                self._client_cache.close()
            self._client_cache = self._new_client()
            self._client_sig = sig
        return self._client_cache

    def open_browser(self) -> dict:
        """Open the dedicated visible Chrome/Edge window used for WB."""
        return self._client().open_browser_session()

    def close(self, close_browser: bool = True) -> None:
        """Release the cached WB client; optionally terminate its browser.

        CLI diagnostics pass ``close_browser=False`` so the user can keep
        the opened WB window, finish its check there, and rerun the check.
        """
        if self._client_cache is not None:
            if close_browser:
                try:
                    self._client_cache.close_browser()
                except Exception:
                    pass
            self._client_cache.close()
            self._client_cache = None
            self._client_sig = None

    def reset_mode(self) -> None:
        """Forget the live/demo verdict so the next scan re-probes WB."""
        self.effective_mode = "—"
        self.mode_reason = None
        self.next_live_probe_ts = None
        self.last_probe_ts = None

    def _probe_query(self) -> str:
        queries = self.settings.get().get("queries") or []
        return str(queries[0]) if queries else "наушники"

    def _apply_probe(self, probe: dict) -> str:
        cfg = self.settings.get()
        requested = cfg.get("mode", "auto")
        now = int(time.time())
        self.last_probe = probe
        self.last_probe_ts = now
        if probe.get("ok"):
            self.live_failures = 0
            self.next_live_probe_ts = None
            if requested == "demo":
                self.effective_mode = "demo"
                self.mode_reason = ("Выбран DEMO вручную: настоящие товары "
                                    "не ищутся. Проверка показала, что WB "
                                    "доступен — переключайся на auto/live.")
            else:
                self.effective_mode = "live"
                self.mode_reason = probe.get("message") or "WB доступен"
                self.last_error = None
        else:
            self.live_failures += 1
            self.mode_reason = probe.get("message") or "WB недоступен"
            self.last_error = self.mode_reason
            if requested == "auto":
                self.effective_mode = "demo"
                self.next_live_probe_ts = now + int(
                    cfg.get("live_probe_interval_sec", 300))
            elif requested == "live":
                self.effective_mode = "live"
                self.next_live_probe_ts = None
            else:
                self.effective_mode = "demo"
        return self.effective_mode

    def diagnose(self, query: str | None = None) -> dict:
        """User-triggered WB check. Updates the mode verdict and wakes
        the scan loop when LIVE just became available.

        For transports that may use the dedicated browser profile, the
        cached client is reused: two clients must not launch competing
        Chrome processes against one profile/port.  The browser session
        itself serializes tab operations between scan and diagnostics.
        """
        transport = str(self.settings.get().get(
            "wb_transport", "auto") or "auto").lower()
        use_cached = transport in ("auto", "chrome", "dom", "cdp", "edge")
        with self._probe_lock:
            client = self._client() if use_cached else self._new_client()
            try:
                probe = client.probe(query or self._probe_query())
            finally:
                if not use_cached:
                    client.close()
            before = self.effective_mode
            effective = self._apply_probe(probe)
        if probe.get("ok") and before != "live" and effective == "live" \
                and self.running:
            self._scan_now.set()
        return {**probe,
                "requested_mode": self.settings.get().get("mode", "auto"),
                "effective_mode": self.effective_mode,
                "mode_reason": self.mode_reason}

    def resolve_mode(self, force: bool = False) -> str:
        requested = self.settings.get().get("mode", "auto")
        if requested == "demo":
            self.effective_mode = "demo"
            self.mode_reason = ("Выбран DEMO вручную: настоящие товары WB "
                                "не ищутся, находки сгенерированы.")
            return "demo"
        if requested == "live":
            self.effective_mode = "live"
            if force or self.last_probe_ts is None:
                # Record *why* live may fail, but stay live regardless.
                self._apply_probe(self._client().probe(self._probe_query()))
                self.effective_mode = "live"
            return "live"
        # auto
        now = int(time.time())
        if not force and self.effective_mode == "live":
            return "live"
        if not force and self.effective_mode == "demo" \
                and self.next_live_probe_ts and now < self.next_live_probe_ts:
            return "demo"  # fallback demo, re-probe is scheduled
        probe = self._client().probe(self._probe_query())
        effective = self._apply_probe(probe)
        log.info("auto mode -> %s (%s)", effective, self.mode_reason)
        return effective

    # ------------------------------------------------------------------ #
    def scan_once(self) -> dict:
        cfg = self.settings.get()
        mode = self.resolve_mode()
        run_id = self.db.start_run(mode, len(cfg["queries"]))
        scanned = new_products = alerts_made = 0
        error = None
        request_failures: list[str] = []
        self.scanning = True
        try:
            products: list[dict[str, Any]] = []
            wb = None
            if mode == "live":
                wb = self._client()
                seen: set[int] = set()
                delay = float(cfg.get("wb_request_delay_sec", 1.2))
                consecutive_failures = 0
                stop_fetching = False
                for q in cfg["queries"]:
                    if self._stop.is_set() or stop_fetching:
                        break
                    for sort in cfg.get("sorts", ["popular"]):
                        if stop_fetching:
                            break
                        first_nm_page1: int | None = None
                        for page in range(1, cfg["pages_per_query"] + 1):
                            if self._stop.is_set():
                                break
                            try:
                                batch = wb.search(q, page=page, sort=sort)
                            except Exception as e:
                                # One dead request must not discard the
                                # products other queries already delivered…
                                msg = f"{q!r}/{sort}/стр.{page}: {str(e)[:240]}"
                                request_failures.append(msg)
                                log.warning("WB search failed (%s)", msg)
                                consecutive_failures += 1
                                # …but a dead WB must not turn one scan
                                # into an endless retry loop either.
                                if consecutive_failures >= 3 and not products:
                                    stop_fetching = True
                                elif consecutive_failures >= 5:
                                    stop_fetching = True
                                break
                            consecutive_failures = 0
                            if not batch:
                                break
                            if page == 1:
                                first_nm_page1 = batch[0]["nm"]
                            elif batch[0]["nm"] == first_nm_page1:
                                break  # WB wrapped around to page 1
                            for p in batch:
                                if p["nm"] not in seen:
                                    seen.add(p["nm"])
                                    products.append(p)
                            time.sleep(delay)  # be polite to WB
                if not products and request_failures:
                    raise RuntimeError(
                        "WB не отдал ни одного товара. "
                        + " | ".join(request_failures[:3]))
                if request_failures:
                    error = (f"Частично: WB не ответил на "
                             f"{len(request_failures)} запрос(ов): "
                             + " | ".join(request_failures[:2]))
                    self.last_error = error[:500]
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
            if mode == "live" and scanned > 0:
                self.live_failures = 0
                self.effective_mode = "live"
                self.mode_reason = "WB доступен, идёт живой поиск"
                if not request_failures:
                    self.last_error = None
        except Exception as e:
            error = str(e)[:500]
            self.last_error = error
            log.warning("scan failed: %s", e)
            if mode == "live":
                self.live_failures += 1
                if self.settings.get().get("mode") == "auto":
                    # Temporary, *visible* fallback: re-probe LIVE on a
                    # timer instead of sticking to demo forever.
                    self.effective_mode = "demo"
                    self.mode_reason = (
                        "WB недоступен — временный DEMO, настоящие товары "
                        f"не ищутся. Причина: {error[:220]}")
                    self.next_live_probe_ts = int(time.time()) + int(
                        cfg.get("live_probe_interval_sec", 300))
                    log.warning("falling back to DEMO until %s",
                                self.next_live_probe_ts)
                else:
                    self.mode_reason = f"LIVE: последний скан упал: {error[:220]}"
        finally:
            self.scanning = False
            self.last_scan_ts = int(time.time())
            self.db.finish_run(run_id, scanned, new_products, alerts_made, error)
        return {"scanned": scanned, "new": new_products,
                "alerts": alerts_made, "error": error, "mode": mode,
                "effective_mode": self.effective_mode,
                "mode_reason": self.mode_reason,
                "request_failures": len(request_failures)}

    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        self.resolve_mode()
        while not self._stop.is_set():
            self.scan_once()
            cfg = self.settings.get()
            interval = int(cfg["interval_sec"])
            if self.effective_mode == "demo":
                if cfg.get("mode") == "demo":
                    # Manually chosen demo: scan often so the UI feels alive.
                    interval = min(interval, 45)
                else:
                    # Auto fallback: wake up in time for the LIVE re-probe.
                    interval = min(
                        interval, int(cfg.get("live_probe_interval_sec", 300)))
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
        cfg = self.settings.get()
        requested = cfg.get("mode", "auto")
        return {
            "running": self.running,
            "scanning": self.scanning,
            "mode": requested,
            "requested_mode": requested,
            "effective_mode": self.effective_mode,
            "mode_reason": self.mode_reason,
            "last_probe": self.last_probe,
            "last_probe_ts": self.last_probe_ts,
            "next_live_probe_ts": self.next_live_probe_ts,
            "live_failures": self.live_failures,
            "wb_transport": cfg.get("wb_transport", "auto"),
            "browser_transport_available": curl_cffi_available(),
            "chrome_transport": browser_info(str(cfg.get("wb_browser_path") or "")),
            "last_scan_ts": self.last_scan_ts,
            "next_scan_ts": self.next_scan_ts if self.running else None,
            "last_error": self.last_error,
        }
