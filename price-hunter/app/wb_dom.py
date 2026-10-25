"""Real installed Chrome/Edge transport for marketplace pages.

This module started as the Wildberries fallback, but the CDP browser
session itself is marketplace-neutral.  WB-specific rendered-tile parsing
also lives here for compatibility; Ozon reuses :class:`ChromeBrowserSession`
with its own home page and parsers.

On some networks WB/WBAAS rejects every request made by the application
process itself (both httpx and the curl_cffi Chrome impersonation get 403),
while the Wildberries site works normally in the user's browser.  This
module is the fallback for exactly that case: it starts the installed
Chrome/Edge with a **dedicated local Price Hunter profile**, talks to it over
the Chrome DevTools Protocol, and then either

* issues WB JSON requests with ``fetch()`` from inside a wildberries.ru tab, or
* if WB still rejects those API calls, reads the already rendered search
  result tiles from the page DOM.

No cookies or session tokens are exported by this module.  Everything stays
inside the dedicated browser profile on the user's computer.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus, urlencode, urlparse

import httpx

from .basket import photo_url, product_url

log = logging.getLogger(__name__)

WB_HOME = "https://www.wildberries.ru/"
OZON_HOME = "https://www.ozon.ru/"
_DEFAULT_PROFILE = Path(tempfile.gettempdir()) / "price-hunter-browser-profile"

_SEARCH_EXTRACT_JS = r"""
(() => {
  const seen = new Map();
  const anchors = Array.from(document.querySelectorAll('a[href*="/catalog/"]'));
  for (const a of anchors) {
    const href = a.href || '';
    const match = href.match(/\/catalog\/(\d+)\/detail\.aspx/);
    if (!match || seen.has(match[1])) continue;

    let card = document.querySelector(`[data-nm-id="${match[1]}"]`) ||
      a.closest('[data-nm-id], article, .product-card, [class*="product-card"]') || a;
    let candidate = card;
    for (let i = 0; i < 6 && candidate; i++) {
      const txt = candidate.innerText || '';
      if (txt.includes('₽') && txt.length < 1500) { card = candidate; break; }
      candidate = candidate.parentElement;
    }
    const text = (card && card.innerText ? card.innerText : (a.innerText || '')).trim();
    if (!text.includes('₽')) continue;

    const img = card ? card.querySelector('img') : null;
    const delText = card
      ? Array.from(card.querySelectorAll('del')).map(e => (e.innerText || '').trim()).join(' ')
      : '';
    seen.set(match[1], {
      id: Number(match[1]),
      href: href,
      title: (a.getAttribute('aria-label') || (img && img.alt) || '').trim(),
      text: text,
      delText: delText,
      photo: img ? (img.currentSrc || img.src || '') : ''
    });
  }
  return {
    url: location.href,
    title: document.title,
    bodyText: (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 2000),
    items: Array.from(seen.values())
  };
})()
"""

_MONEY_RE = re.compile(
    r"(\d[\d\s\u00a0\u202f]*(?:[,.]\d{1,2})?)\s*(?:₽|руб\.?)", re.I)
_RATING_RE = re.compile(r"(?<![\d,.])([0-5][,.]\d)(?![\d,.])")
_FEEDBACK_RE = re.compile(
    r"(\d[\d\s\u00a0\u202f]*)\s*(?:оценок|оценки|оценк[аиу]|отзывов|отзыва|отзыв)",
    re.I,
)
_NAME_STOP_PREFIXES = (
    "с wb кошельком", "wb кошел", "кошелёк wb", "кошелек wb", "распродажа",
    "оригинал", "реклама", "доставка", "завтра", "сегодня", "осталось",
    "есть в наличии", "нет в наличии", "быстрая доставка", "похожие",
    "смотреть все", "в корзину", "купить",
)


class BrowserSessionError(RuntimeError):
    """A real-browser transport failure with a WB diagnostic outcome."""

    def __init__(self, message: str, outcome: str = "network_error"):
        super().__init__(message)
        self.outcome = outcome


def websocket_available() -> bool:
    return importlib.util.find_spec("websocket") is not None


def _candidate_browser_paths() -> Iterable[Path]:
    # PATH lookup covers Linux/macOS packages and some Windows installs.
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "msedge", "microsoft-edge", "brave"):
        found = shutil.which(name)
        if found:
            yield Path(found)

    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]))
    for root in roots:
        yield root / "Google" / "Chrome" / "Application" / "chrome.exe"
        yield root / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        yield root / "Chromium" / "Application" / "chrome.exe"
        yield root / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"

    # Usual macOS application bundles (harmless on other platforms).
    yield Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    yield Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    yield Path("/Applications/Chromium.app/Contents/MacOS/Chromium")


def find_browser_executable(custom: str = "") -> str | None:
    """Find installed Chrome/Edge/Chromium; never downloads anything."""
    custom = (custom or os.environ.get("PRICE_HUNTER_BROWSER") or
              os.environ.get("WB_BROWSER_PATH") or "").strip().strip('"')
    if custom:
        p = Path(custom)
        if p.is_dir():
            for name in ("chrome.exe", "msedge.exe", "chrome", "msedge"):
                if (p / name).exists():
                    return str(p / name)
        if p.exists():
            return str(p)
        found = shutil.which(custom)
        if found:
            return found
        return None
    seen: set[str] = set()
    for path in _candidate_browser_paths():
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.exists():
                return str(path)
        except OSError:
            continue
    return None


def browser_info(custom: str = "") -> dict[str, Any]:
    exe = find_browser_executable(custom)
    return {
        "available": bool(exe and websocket_available()),
        "executable_found": bool(exe),
        "executable": Path(exe).name if exe else None,
        "websocket_available": websocket_available(),
    }


def chrome_available(custom: str = "") -> bool:
    return bool(browser_info(custom)["available"])


def _money_values(text: Any) -> list[float]:
    values: list[float] = []
    for line in str(text or "").splitlines() or [str(text or "")]:
        low = line.lower()
        if "/мес" in low or "в месяц" in low or "рассроч" in low:
            continue  # an installment amount is not the product price
        for match in _MONEY_RE.finditer(line):
            raw = re.sub(r"[\s\u00a0\u202f]", "", match.group(1)).replace(",", ".")
            try:
                value = float(raw)
            except ValueError:
                continue
            if value > 0:
                values.append(value)
    return values


def _is_name_line(line: str) -> bool:
    low = line.lower().strip()
    if len(low) < 3 or "₽" in low:
        return False
    if re.fullmatch(r"[-−]?\d+\s*%", low) or re.match(r"^\d+[,.]\d\b", low):
        return False
    if any(low.startswith(prefix) for prefix in _NAME_STOP_PREFIXES):
        return False
    if low in {"₽", "%", "•", "·"}:
        return False
    return True


def _split_brand_name(candidate: str) -> tuple[str, str]:
    candidate = re.sub(r"\s+", " ", str(candidate or "")).strip(" /")
    if " / " in candidate:
        brand, name = candidate.split(" / ", 1)
        return brand.strip(), name.strip() or candidate
    if "/" in candidate:
        left, right = candidate.split("/", 1)
        if left.strip() and right.strip() and len(left.strip()) <= 40:
            return left.strip(), right.strip()
    return "", candidate


def parse_dom_search_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Interpret rendered WB tile text in pure Python (unit-tested offline)."""
    products: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in items or []:
        try:
            nm = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not nm or nm in seen:
            continue
        text = str(item.get("text") or "")
        amounts = _money_values(text)
        if not amounts:
            continue
        seen.add(nm)

        # WB search tiles usually expose the WB Wallet price and a struck
        # reference price.  The payable hunt price is therefore the smallest
        # product amount; <del> is the most reliable “before” price.
        sale = min(amounts)
        struck = _money_values(item.get("delText") or item.get("del") or "")
        basic = max(struck) if struck else max(amounts)
        if basic < sale:
            basic = sale

        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        candidate = str(item.get("title") or "").strip()
        if not candidate or "₽" in candidate:
            candidate = next((line for line in lines if _is_name_line(line)), "")
        brand, name = _split_brand_name(candidate)
        if not name:
            name = f"Товар WB {nm}"

        rating = 0.0
        m = _RATING_RE.search(text)
        if m:
            try:
                rating = float(m.group(1).replace(",", "."))
            except ValueError:
                rating = 0.0
        feedbacks = 0
        m = _FEEDBACK_RE.search(text)
        if m:
            feedbacks = int(re.sub(r"[\s\u00a0\u202f]", "", m.group(1)) or 0)

        photo = str(item.get("photo") or "")
        if not photo.startswith("http"):
            photo = photo_url(nm)
        href = str(item.get("href") or "")
        products.append({
            "nm": nm,
            "name": name,
            "brand": brand,
            "supplier": "",
            "supplier_id": None,
            "price_rub": float(basic),
            "sale_price_rub": float(sale),
            "rating": rating,
            "feedbacks": feedbacks,
            "photo": photo,
            "url": href if href.startswith("http") else product_url(nm),
            "size": "",
            "total_quantity": None,
            "source_transport": "chrome-dom",
            "price_kind": "wallet_or_card_lowest",
        })
    return products


class ChromeBrowserSession:
    """A small synchronous Chrome DevTools Protocol client.

    One session owns one page websocket.  WB and Ozon intentionally use
    separate session objects **and separate tabs** over the same dedicated
    profile/port, so a WB navigation can never yank the user's Ozon tab
    away (and vice versa).  All sessions for one physical browser endpoint
    still share a process-wide RLock: a scan, diagnostics and the other
    marketplace can never drive that window at the same time.  If Price Hunter launched the browser
    process, :meth:`terminate` can close it; merely calling :meth:`close`
    leaves the visible browser open so a user can pass a marketplace
    browser check and immediately retry diagnostics/scanning.
    """

    _locks_guard = threading.Lock()
    _locks_by_endpoint: dict[tuple[str, int], threading.RLock] = {}

    @classmethod
    def _shared_lock(cls, profile_dir: Path, port: int) -> threading.RLock:
        key = (os.path.normcase(str(profile_dir)), int(port))
        with cls._locks_guard:
            lock = cls._locks_by_endpoint.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._locks_by_endpoint[key] = lock
            return lock

    def __init__(self, executable: str = "", profile_dir: str = "",
                 port: int = 9229, headless: bool = False,
                 wait_sec: float = 25, timeout: float = 30,
                 home_url: str = WB_HOME):
        self.home_url = home_url or WB_HOME
        self.home_host = urlparse(self.home_url).netloc.lower()
        self.home_domain = self.home_host[4:] \
            if self.home_host.startswith("www.") else self.home_host
        self.custom_executable = executable or ""
        self.profile_dir = Path(profile_dir) if profile_dir else _DEFAULT_PROFILE
        try:
            self.port = int(port or 9229)
        except (TypeError, ValueError):
            self.port = 9229
        self.headless = bool(headless)
        self.wait_sec = max(5.0, float(wait_sec or 25))
        self.timeout = max(5.0, float(timeout or 30))
        self._lock = self._shared_lock(self.profile_dir, self.port)
        self._proc: subprocess.Popen[Any] | None = None
        self._ws: Any = None
        self._target_id: str | None = None
        self._next_id = 0
        self.executable: str | None = None
        self.launched_by_app = False

    @property
    def debug_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- lifecycle ----------------------------------------------------- #
    def _http_json(self, path: str, method: str = "GET") -> Any:
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            response = client.request(method, self.debug_base + path)
            response.raise_for_status()
            return response.json()

    def _cdp_ready(self) -> bool:
        try:
            info = self._http_json("/json/version")
            return bool(isinstance(info, dict) and info.get("Browser"))
        except Exception:
            return False

    def _launch_args(self) -> list[str]:
        args = [
            self.executable,
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            # WB and Ozon live in separate tabs of this one window, so at
            # any moment one marketplace's tab is in the background.
            # Without these flags Chrome throttles timers/rendering in
            # hidden tabs and the background marketplace's search grid
            # may not render within the wait window at all — the scan
            # then grinds through full timeouts and looks frozen.
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=IntensiveWakeUpThrottling,"
            "CalculateNativeWinOcclusion",
            "--window-size=1280,900",
            "--window-position=40,40",
        ]
        if self.headless:
            args.append("--headless=new")
        args.append(self.home_url)
        return args

    def _launch(self) -> None:
        if self._proc is not None and self._proc.poll() is None and self._cdp_ready():
            return
        if self._cdp_ready():
            self.launched_by_app = False
            return
        self.executable = find_browser_executable(self.custom_executable)
        if not self.executable:
            raise BrowserSessionError(
                "Chrome/Edge не найден. Установи Chrome или Edge либо укажи путь "
                "к браузеру в настройках Price Hunter.")
        if not websocket_available():
            raise BrowserSessionError(
                "В сборке отсутствует websocket-client, поэтому настоящий "
                "браузерный транспорт недоступен.")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = self._launch_args()
        kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL,
                                  "stdin": subprocess.DEVNULL,
                                  "close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self._proc = subprocess.Popen(args, **kwargs)
        except Exception as e:
            raise BrowserSessionError(f"Не удалось запустить браузер: {e!r}") from e
        self.launched_by_app = True

        deadline = time.monotonic() + self.wait_sec
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise BrowserSessionError(
                    "Браузер завершился сразу после запуска. Проверь путь к "
                    "Chrome/Edge и что отдельный профиль Price Hunter не занят.")
            if self._cdp_ready():
                return
            time.sleep(0.25)
        raise BrowserSessionError(
            f"Браузер запустился, но CDP на порту {self.port} не отвечает. "
            "Закрой окно браузера Price Hunter и повтори проверку.")

    def _is_home_url(self, url: Any) -> bool:
        host = (urlparse(str(url or "")).hostname or "").lower()
        if not host or not self.home_domain:
            return False
        return host == self.home_domain or host == self.home_host or \
            host.endswith("." + self.home_domain)

    @staticmethod
    def _is_blank_url(url: Any) -> bool:
        return str(url or "").strip().lower() in (
            "", "about:blank", "chrome://newtab/", "chrome://new-tab-page/",
            "edge://newtab/", "edge://new-tab-page/")

    def _select_target(self, targets: Any) -> dict[str, Any] | None:
        """Pick *this marketplace's* tab — never the other one's.

        Preference: the tab this session already owned, then any tab on
        this marketplace's domain, then a blank tab we may adopt.  If none
        qualifies the caller creates a new tab, so WB work can never
        navigate the Ozon tab away (the old single-tab behaviour).
        """
        pages = [t for t in targets if isinstance(t, dict)
                 and t.get("type") == "page" and t.get("webSocketDebuggerUrl")] \
            if isinstance(targets, list) else []
        if self._target_id:
            owned = next((t for t in pages
                          if str(t.get("id") or "") == self._target_id), None)
            if owned is not None:
                return owned
        home = next((t for t in pages if self._is_home_url(t.get("url"))), None)
        if home is not None:
            return home
        return next((t for t in pages if self._is_blank_url(t.get("url"))), None)

    def _create_target(self) -> dict[str, Any]:
        errors: list[str] = []
        for method in ("PUT", "GET"):
            try:
                target = self._http_json(
                    "/json/new?" + urlencode({"url": self.home_url}),
                    method=method)
            except Exception as e:  # modern Chrome: PUT only, older: GET
                errors.append(f"{method}: {e!r}")
                continue
            if isinstance(target, dict) and target.get("webSocketDebuggerUrl"):
                return target
            errors.append(f"{method}: нет webSocketDebuggerUrl")
        raise BrowserSessionError(
            "Не удалось открыть отдельную вкладку маркетплейса в браузере: "
            + "; ".join(errors))

    def _connect(self) -> None:
        if self._ws is not None:
            return
        self._launch()
        try:
            targets = self._http_json("/json/list")
        except Exception as e:
            raise BrowserSessionError(f"CDP браузера не отвечает: {e!r}") from e
        target = self._select_target(targets)
        if target is None:
            target = self._create_target()
        ws_url = target.get("webSocketDebuggerUrl") if isinstance(target, dict) else None
        if not ws_url:
            raise BrowserSessionError("Браузер не дал адрес вкладки для CDP.")
        self._target_id = str(target.get("id") or "") or None
        try:
            import websocket  # websocket-client, imported lazily
            self._ws = websocket.create_connection(
                ws_url, timeout=self.timeout + 5, suppress_origin=True)
        except Exception as e:
            raise BrowserSessionError(f"Не удалось подключиться к браузеру: {e!r}") from e
        self._send("Page.enable")
        self._send("Runtime.enable")
        try:
            # The tab may sit in the background while the user looks at
            # the other marketplace; focus emulation keeps focus-gated
            # page logic (lazy hydration, deferred rendering) running.
            self._send("Emulation.setFocusEmulationEnabled",
                       {"enabled": True})
        except BrowserSessionError:
            pass  # older engines: launch-time anti-throttling still applies

    def ensure_ready(self, home_url: str | None = None) -> dict[str, Any]:
        with self._lock:
            if home_url:
                new_host = urlparse(home_url).netloc.lower()
                new_domain = new_host[4:] if new_host.startswith("www.") \
                    else new_host
                if new_domain != self.home_domain:
                    # The session was retargeted at the other marketplace:
                    # drop the old tab binding so _connect() adopts/creates
                    # that marketplace's own tab instead of hijacking this one.
                    self.close()
                    self._target_id = None
                self.home_url = home_url
                self.home_host = new_host
                self.home_domain = new_domain
            self._connect()
            url = ""
            try:
                url = str(self.evaluate("location.href") or "")
            except BrowserSessionError:
                self.close()
                self._connect()
                url = str(self.evaluate("location.href") or "")
            if not self._is_home_url(url):
                self.navigate(self.home_url)
                url = str(self.evaluate("location.href") or "")
            return {"ok": True, "url": url, "home_url": self.home_url,
                    "target_id": self._target_id,
                    "executable": self.executable,
                    "profile_dir": str(self.profile_dir), "port": self.port}

    def bring_to_front(self, home_url: str | None = None) -> dict[str, Any]:
        """Ensure this marketplace's tab exists and make it the active one.

        Only for explicit user actions («Открыть браузер WB/Ozon»): routine
        fetches/DOM reads must not steal the active tab while scanning.
        """
        with self._lock:
            info = self.ensure_ready(home_url)
            try:
                self._send("Page.bringToFront")
                info = {**info, "url": str(
                    self.evaluate("location.href") or info.get("url") or "")}
            except BrowserSessionError:
                pass  # activation is cosmetic; the tab itself is ready
            return info

    def close(self) -> None:
        """Close only the CDP connection; keep the visible browser alive."""
        with self._lock:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None

    def terminate(self) -> None:
        """Close the browser too, but only if this app launched it."""
        with self._lock:
            self.close()
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None
            self._target_id = None  # browser gone: forget the owned tab

    # -- CDP primitives ------------------------------------------------- #
    def _send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._ws is None:
                raise BrowserSessionError("Соединение с браузером не открыто.")
            self._next_id += 1
            msg_id = self._next_id
            payload = {"id": msg_id, "method": method, "params": params or {}}
            try:
                self._ws.settimeout(self.timeout + 5)
                self._ws.send(json.dumps(payload))
                deadline = time.monotonic() + self.timeout + 5
                while time.monotonic() < deadline:
                    raw = self._ws.recv()
                    if not raw:
                        continue
                    data = json.loads(raw)
                    if data.get("id") != msg_id:
                        continue  # a CDP event, not our response
                    if data.get("error"):
                        raise BrowserSessionError(
                            f"Ошибка CDP {method}: {data['error']}")
                    return data.get("result") or {}
            except BrowserSessionError:
                raise
            except Exception as e:
                self._ws = None
                raise BrowserSessionError(
                    f"Связь с окном браузера потеряна ({method}): {e!r}. "
                    "Не закрывай окно Chrome/Edge Price Hunter во время охоты.") from e
            raise BrowserSessionError(f"Браузер не ответил на {method} вовремя.")

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        with self._lock:
            self._connect()
            result = self._send("Runtime.evaluate", {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": bool(await_promise),
                "timeout": int(self.timeout * 1000),
            })
            details = result.get("exceptionDetails") if isinstance(result, dict) else None
            if details:
                exc = details.get("exception") or {}
                text = (exc.get("description") or exc.get("value") or
                        details.get("text") or "JavaScript error")
                raise BrowserSessionError(f"Ошибка на странице маркетплейса: {text}")
            value = (result.get("result") or {}) if isinstance(result, dict) else {}
            if value.get("type") == "undefined":
                return None
            return value.get("value")

    def _page_state(self, selector: str | None = None) -> dict[str, Any]:
        selector_js = json.dumps(selector) if selector else "null"
        state = self.evaluate(
            "(() => { const sel = " + selector_js + "; return {"
            "ready: document.readyState, url: location.href, title: document.title,"
            "text: (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 2500),"
            "has: sel ? !!document.querySelector(sel) : false}; })()")
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _challenge_outcome(text: str) -> str | None:
        low = (text or "").lower()
        if ("подозрительная активность" in low or "доступ ограничен" in low or
                "похожи на автоматические" in low or
                "автоматические запросы" in low or "antibot" in low or
                "anti-bot" in low):
            return "suspicious"
        if ("Введите символы" in low or "введите код с картинки" in low or
                "captcha" in low or "подтвердите, что вы не робот" in low or
                "подтвердите, что вы человек" in low or "вы не робот" in low):
            return "captcha"
        if ("Проверяем браузер" in low or "checking your browser" in low or
                "проверка безопасности" in low or "проверяем ваш браузер" in low):
            return "checking"
        return None

    @classmethod
    def _state_challenge(cls, state: dict[str, Any]) -> str | None:
        outcome = cls._challenge_outcome(
            f"{state.get('title') or ''}\n{state.get('text') or ''}")
        if outcome:
            return outcome
        url = str(state.get("url") or "").lower()
        # Ozon's anti-bot can sit on a nearly blank redirect/check page.
        # Treat it as an in-progress browser check, not parse drift.  A
        # fully rendered document can retain a harmless __rr parameter,
        # so the URL alone is not enough once real page text exists.
        if "__rr=" in url or "antibot" in url or "captcha" in url:
            if state.get("ready") == "complete" and len(
                    str(state.get("text") or "")) > 500:
                return None
            return "checking"
        return None

    def navigate(self, url: str, wait_selector: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._connect()
            nav = self._send("Page.navigate", {"url": url})
            if nav.get("errorText"):
                raise BrowserSessionError(
                    f"Браузер не открыл страницу маркетплейса: {nav['errorText']}")
            deadline = time.monotonic() + self.wait_sec
            last_state: dict[str, Any] = {}
            while time.monotonic() < deadline:
                time.sleep(0.4)
                try:
                    last_state = self._page_state(wait_selector)
                except BrowserSessionError:
                    continue  # document is being replaced during navigation
                challenge = self._state_challenge(last_state)
                if challenge in {"suspicious", "captcha"}:
                    raise BrowserSessionError(
                        "Маркетплейс показал проверку/подозрительную активность в окне "
                        "браузера Price Hunter. Дождись или пройди её вручную, "
                        "оставь окно открытым и нажми проверку ещё раз.",
                        outcome="blocked")
                if wait_selector and last_state.get("has"):
                    return last_state
                if challenge == "checking":
                    continue
                if not wait_selector and last_state.get("ready") in {"interactive", "complete"}:
                    return last_state
            challenge = self._state_challenge(last_state)
            if challenge == "checking":
                raise BrowserSessionError(
                    "Маркетплейс долго показывает проверку браузера. Оставь окно "
                    "Chrome/Edge Price Hunter открытым, дождись загрузки сайта и "
                    "повтори диагностику.", outcome="blocked")
            return last_state

    # -- Marketplace operations ------------------------------------------- #
    def fetch(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch JSON from inside the live marketplace tab."""
        with self._lock:
            self.ensure_ready()
            full_url = url
            if params:
                separator = "&" if "?" in full_url else "?"
                full_url += separator + urlencode(params, doseq=True)
            expression = (
                "(async () => { try {"
                f" const r = await fetch({json.dumps(full_url)}, "
                "{credentials: 'include', headers: {Accept: 'application/json, text/plain, */*'}});"
                " return {status: r.status, url: r.url, text: await r.text(),"
                " headers: Object.fromEntries(r.headers.entries())};"
                "} catch (e) { return {status: 0, url: '', text: '', "
                "headers: {}, error: String(e)}; } })()")
            result = self.evaluate(expression, await_promise=True)
            if not isinstance(result, dict):
                raise BrowserSessionError("Браузер вернул пустой результат fetch().")
            if result.get("status") == 0 and result.get("error"):
                raise BrowserSessionError(
                    f"Fetch из вкладки маркетплейса не выполнен: {result['error']}")
            return result

    def rendered_page_payload(self, url: str, wait_selector: str,
                              extractor_js: str,
                              scrolls: int = 3) -> dict[str, Any]:
        """Open a rendered marketplace page and evaluate a tile extractor."""
        with self._lock:
            self.navigate(url, wait_selector=wait_selector)
            # A little scrolling lets lazy tiles render.  Keep it gentle:
            # this is a visible user browser, not a high-rate crawler.
            for _ in range(max(0, int(scrolls or 0))):
                try:
                    self.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.85))")
                except BrowserSessionError:
                    break
                time.sleep(0.6)
            payload = self.evaluate(extractor_js)
            if not isinstance(payload, dict):
                raise BrowserSessionError(
                    "Не удалось прочитать отрисованную выдачу маркетплейса.",
                    outcome="bad_shape")
            challenge = self._state_challenge({
                "title": payload.get("title"),
                "text": payload.get("bodyText"),
                "url": payload.get("url"),
            })
            if challenge and not payload.get("items"):
                raise BrowserSessionError(
                    "Маркетплейс показал проверку браузера на странице поиска. "
                    "Оставь окно Price Hunter открытым, пройди проверку и повтори.",
                    outcome="blocked")
            payload.setdefault("items", [])
            return payload

    def search_dom_payload(self, query: str, page: int = 1,
                           sort: str = "popular") -> dict[str, Any]:
        """Open a normal WB search page and return its rendered tile text."""
        safe_sort = sort if re.fullmatch(r"[a-z]+", sort or "") else "popular"
        url = ("https://www.wildberries.ru/catalog/0/search.aspx?" +
               urlencode({"page": max(1, int(page or 1)), "sort": safe_sort,
                          "search": query, "meta_chars": "true"},
                         quote_via=quote_plus))
        selector = 'a[href*="/catalog/"][href*="detail.aspx"]'
        return self.rendered_page_payload(url, selector, _SEARCH_EXTRACT_JS)

    def search_dom(self, query: str, page: int = 1,
                   sort: str = "popular") -> list[dict[str, Any]]:
        payload = self.search_dom_payload(query, page, sort)
        return parse_dom_search_items(payload.get("items") or [])
