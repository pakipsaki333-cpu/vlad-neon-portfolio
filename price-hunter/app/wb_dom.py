"""Real installed Chrome/Edge transport for Wildberries.

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
from urllib.parse import quote_plus, urlencode

import httpx

from .basket import photo_url, product_url

log = logging.getLogger(__name__)

WB_HOME = "https://www.wildberries.ru/"
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

    One session owns one page websocket.  If Price Hunter launched the
    browser process, :meth:`terminate` can close it; merely calling
    :meth:`close` leaves the visible browser open so a user can pass WB's
    browser check and immediately retry diagnostics/scanning.
    """

    def __init__(self, executable: str = "", profile_dir: str = "",
                 port: int = 9229, headless: bool = False,
                 wait_sec: float = 25, timeout: float = 30):
        self.custom_executable = executable or ""
        self.profile_dir = Path(profile_dir) if profile_dir else _DEFAULT_PROFILE
        try:
            self.port = int(port or 9229)
        except (TypeError, ValueError):
            self.port = 9229
        self.headless = bool(headless)
        self.wait_sec = max(5.0, float(wait_sec or 25))
        self.timeout = max(5.0, float(timeout or 30))
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[Any] | None = None
        self._ws: Any = None
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
        args = [
            self.executable,
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,900",
            "--window-position=40,40",
        ]
        if self.headless:
            args.append("--headless=new")
        args.append(WB_HOME)
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

    def _connect(self) -> None:
        if self._ws is not None:
            return
        self._launch()
        try:
            targets = self._http_json("/json/list")
        except Exception as e:
            raise BrowserSessionError(f"CDP браузера не отвечает: {e!r}") from e
        pages = [t for t in targets if isinstance(t, dict)
                 and t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        target = next((t for t in pages if "wildberries.ru" in str(t.get("url") or "")),
                      pages[0] if pages else None)
        if target is None:
            try:
                target = self._http_json(
                    "/json/new?" + urlencode({"url": WB_HOME}), method="PUT")
            except Exception as e:
                raise BrowserSessionError(
                    f"Не удалось открыть вкладку WB в браузере: {e!r}") from e
        ws_url = target.get("webSocketDebuggerUrl") if isinstance(target, dict) else None
        if not ws_url:
            raise BrowserSessionError("Браузер не дал адрес вкладки для CDP.")
        try:
            import websocket  # websocket-client, imported lazily
            self._ws = websocket.create_connection(
                ws_url, timeout=self.timeout + 5, suppress_origin=True)
        except Exception as e:
            raise BrowserSessionError(f"Не удалось подключиться к браузеру: {e!r}") from e
        self._send("Page.enable")
        self._send("Runtime.enable")

    def ensure_ready(self) -> dict[str, Any]:
        with self._lock:
            self._connect()
            url = ""
            try:
                url = str(self.evaluate("location.href") or "")
            except BrowserSessionError:
                self.close()
                self._connect()
                url = str(self.evaluate("location.href") or "")
            if "wildberries.ru" not in url:
                self.navigate(WB_HOME)
                url = str(self.evaluate("location.href") or "")
            return {"ok": True, "url": url, "executable": self.executable,
                    "profile_dir": str(self.profile_dir), "port": self.port}

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
                raise BrowserSessionError(f"Ошибка на странице WB: {text}")
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
        if "подозрительная активность" in low:
            return "suspicious"
        if ("Введите символы" in low or "введите код с картинки" in low or
                "captcha" in low or "подтвердите, что вы не робот" in low):
            return "captcha"
        if "Проверяем браузер" in low or "checking your browser" in low:
            return "checking"
        return None

    def navigate(self, url: str, wait_selector: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._connect()
            nav = self._send("Page.navigate", {"url": url})
            if nav.get("errorText"):
                raise BrowserSessionError(
                    f"Браузер не открыл WB: {nav['errorText']}")
            deadline = time.monotonic() + self.wait_sec
            last_state: dict[str, Any] = {}
            while time.monotonic() < deadline:
                time.sleep(0.4)
                try:
                    last_state = self._page_state(wait_selector)
                except BrowserSessionError:
                    continue  # document is being replaced during navigation
                text = str(last_state.get("text") or "")
                challenge = self._challenge_outcome(text)
                if challenge in {"suspicious", "captcha"}:
                    raise BrowserSessionError(
                        "WB показал проверку/подозрительную активность в окне "
                        "браузера Price Hunter. Дождись или пройди её вручную, "
                        "оставь окно открытым и нажми проверку ещё раз.",
                        outcome="blocked")
                if challenge == "checking":
                    continue
                if wait_selector and last_state.get("has"):
                    return last_state
                if not wait_selector and last_state.get("ready") in {"interactive", "complete"}:
                    return last_state
            challenge = self._challenge_outcome(str(last_state.get("text") or ""))
            if challenge == "checking":
                raise BrowserSessionError(
                    "WB долго показывает «Проверяем браузер». Оставь окно "
                    "Chrome/Edge Price Hunter открытым, дождись загрузки WB и "
                    "повтори диагностику.", outcome="blocked")
            return last_state

    # -- WB operations --------------------------------------------------- #
    def fetch(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch JSON from inside the live WB tab (real browser session)."""
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
                    f"Fetch из вкладки WB не выполнен: {result['error']}")
            return result

    def search_dom_payload(self, query: str, page: int = 1,
                           sort: str = "popular") -> dict[str, Any]:
        """Open a normal WB search page and return its rendered tile text."""
        with self._lock:
            safe_sort = sort if re.fullmatch(r"[a-z]+", sort or "") else "popular"
            url = ("https://www.wildberries.ru/catalog/0/search.aspx?" +
                   urlencode({"page": max(1, int(page or 1)), "sort": safe_sort,
                              "search": query, "meta_chars": "true"},
                             quote_via=quote_plus))
            selector = 'a[href*="/catalog/"][href*="detail.aspx"]'
            self.navigate(url, wait_selector=selector)
            # A little scrolling lets lazy tiles render.  Keep it gentle:
            # this is a visible user browser, not a high-rate crawler.
            for _ in range(3):
                try:
                    self.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.85))")
                except BrowserSessionError:
                    break
                time.sleep(0.6)
            payload = self.evaluate(_SEARCH_EXTRACT_JS)
            if not isinstance(payload, dict):
                raise BrowserSessionError(
                    "Не удалось прочитать отрисованную выдачу WB.", outcome="bad_shape")
            body = str(payload.get("bodyText") or "")
            challenge = self._challenge_outcome(body)
            if challenge:
                raise BrowserSessionError(
                    "WB показал проверку браузера на странице поиска. Оставь окно "
                    "Price Hunter открытым, пройди проверку и повтори.",
                    outcome="blocked")
            payload.setdefault("items", [])
            return payload

    def search_dom(self, query: str, page: int = 1,
                   sort: str = "popular") -> list[dict[str, Any]]:
        payload = self.search_dom_payload(query, page, sort)
        return parse_dom_search_items(payload.get("items") or [])
