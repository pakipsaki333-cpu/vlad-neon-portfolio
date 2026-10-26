"""Локальный веб-сервер NeonMiner (только stdlib).

Запуск:  python3 -m neon_miner serve --host 0.0.0.0 --port 8000
Сервер слушает интерфейс, который ты указал сам; по умолчанию 127.0.0.1.
Данные кошелька никуда не отправляются, кроме публичного read-only API пула.
"""

from __future__ import annotations

import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__
from .address import validate_address
from .calc import estimate
from .configgen import bundle
from .http_client import demo_view, enrich_with_market, fetch_json, get_view
from .pools import POOLS, catalog, get

STATIC = Path(__file__).parent / "static"
MAX_BODY = 256 * 1024
_WALLET_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{90,110}(?:\.[0-9a-fA-F]{16,64})?$")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    if length > MAX_BODY:
        raise ApiError("Слишком большой запрос", 413)
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(f"Некорректный JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ApiError("Ожидается JSON-объект")
    return data


def _wallet(value: Any) -> str:
    text = str(value or "").strip()
    if not _WALLET_RE.match(text):
        raise ApiError("Некорректный формат XMR-адреса (нужно 95 или 106 символов Base58).")
    return text


def route(method: str, path: str, query: Dict[str, list], body: dict) -> Tuple[Any, int]:
    """Единая точка маршрутизации API. Возвращает (payload, http status)."""
    if path == "/api/health":
        return {"ok": True, "version": __version__, "pools": len(POOLS)}, 200

    if path == "/api/catalog" and method == "GET":
        return {"pools": catalog(), "checked_at": "2026-10-26"}, 200

    if path == "/api/validate" and method == "GET":
        address = (query.get("address") or [""])[0]
        return validate_address(address).as_dict(), 200

    if path == "/api/stats" and method == "GET":
        pool_id = (query.get("pool") or ["c3pool"])[0]
        if pool_id not in POOLS:
            raise ApiError("Неизвестный пул")
        wallet = (query.get("wallet") or [""])[0].strip()
        force_demo = (query.get("demo") or ["0"])[0] in ("1", "true", "yes")
        if wallet and not force_demo:
            info = validate_address(wallet)
            if not info.valid:
                raise ApiError(info.reason)
            wallet = info.address
        return get_view(pool_id, wallet, force_demo=force_demo), 200

    if path == "/api/normalize" and method == "POST":
        # Сюда фронтенд шлёт СЫРЫЕ ответы пула, когда сам сходил в API браузером.
        pool_id = str(body.get("pool") or "c3pool")
        if pool_id not in POOLS:
            raise ApiError("Неизвестный пул")
        from .adapters import normalize_view
        view = normalize_view(
            pool_id,
            stats=body.get("stats"),
            payments=body.get("payments"),
            pool_stats=body.get("pool_stats"),
            demo=False,
        )
        return enrich_with_market(view), 200

    if path == "/api/config" and method == "POST":
        demo_mode = bool(body.get("demo"))
        if demo_mode:
            # Пример конфигурации без кошелька: вместо адреса — явная заглушка.
            wallet = "PASTE_YOUR_XMR_ADDRESS_HERE"
        else:
            wallet = (body.get("wallet") or "").strip()
            info = validate_address(wallet)
            if not info.valid:
                raise ApiError(info.reason)
            wallet = info.address
        pool_id = str(body.get("pool") or "c3pool")
        if pool_id not in POOLS:
            raise ApiError("Неизвестный пул")
        port = body.get("port")
        try:
            port = int(port) if port not in (None, "", 0) else None
        except (TypeError, ValueError) as exc:
            raise ApiError("port должен быть числом") from exc
        try:
            result = bundle(
                wallet=wallet,
                pool_id=pool_id,
                port=port,
                worker=str(body.get("worker") or "rig-01"),
                threads_pct=int(body.get("threads_pct") or 75),
                donate_level=int(body.get("donate_level") or 1),
                fixed_difficulty=(int(body["fixed_difficulty"])
                                  if body.get("fixed_difficulty") else None),
            )
            result["demo"] = demo_mode
            return result, 200
        except (TypeError, ValueError, KeyError) as exc:
            raise ApiError(f"Ошибка генерации конфига: {exc}") from exc

    if path == "/api/calc" and method == "POST":
        try:
            hashrate = float(body.get("hashrate_hs") or 0)
            result = estimate(
                hashrate,
                network_hashrate_hs=(float(body["network_hashrate_hs"])
                                     if body.get("network_hashrate_hs") else None),
                block_reward_xmr=(float(body["block_reward_xmr"])
                                  if body.get("block_reward_xmr") else None),
                fee_pct=float(body.get("fee_pct") or 0),
                threshold_xmr=float(body.get("threshold_xmr") or 0.001),
                price_eur=(float(body["price_eur"]) if body.get("price_eur") else None),
                power_watts=float(body.get("power_watts") or 65),
                eur_per_kwh=(float(body["eur_per_kwh"])
                             if body.get("eur_per_kwh") not in (None, "") else None),
            )
        except (TypeError, ValueError) as exc:
            raise ApiError(f"Ошибка расчёта: {exc}") from exc
        return result, 200

    if path == "/api/release" and method == "GET":
        from .downloader import latest_release, pick_assets, platform_suffix, DownloadError
        try:
            release = latest_release()
            suffix = platform_suffix()
            asset, _ = pick_assets(release, suffix)
            return {
                "ok": True,
                "version": release["tag_name"],
                "asset": asset["name"],
                "size_mb": round(asset["size"] / 1_000_000, 2),
                "url": release.get("html_url"),
                "source": "api.github.com/repos/xmrig/xmrig/releases/latest",
            }, 200
        except DownloadError as exc:
            return {"ok": False, "error": str(exc)}, 200
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}, 200

    if path == "/api/demo" and method == "GET":
        pool_id = (query.get("pool") or ["c3pool"])[0]
        if pool_id not in POOLS:
            raise ApiError("Неизвестный пул")
        return demo_view(pool_id), 200

    raise ApiError("Неизвестный endpoint", 404)


class Handler(BaseHTTPRequestHandler):
    server_version = f"NeonMiner/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Не пишем в логи адрес кошелька: обрезаем query-строку.
        safe = str(args[0]).split("?", 1)[0] if args else ""
        print(f"  {self.address_string()} {safe}", flush=True)

    def _send(self, status: int, payload: bytes, content_type: str,
              *, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "public, max-age=300" if cache else "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _serve_static(self, path: str) -> None:
        relative = path[len("/static/"):] if path.startswith("/static/") else ""
        if not relative or ".." in relative or "/" in relative:
            raise ApiError("Нет такого файла", 404)
        target = STATIC / relative
        if not target.is_file():
            raise ApiError("Нет такого файла", 404)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type, cache=False)

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == "/" or path == "/index.html":
                self._serve_static("/static/index.html")
                return
            if path.startswith("/static/"):
                self._serve_static(path)
                return
            if path.startswith("/api/"):
                body = _read_json(self) if method == "POST" else {}
                payload, status = route(method, path, query, body)
                self._send(status, _json_bytes(payload), "application/json; charset=utf-8")
                return
            raise ApiError("Страница не найдена", 404)
        except ApiError as exc:
            self._send(exc.status, _json_bytes({"error": str(exc)}),
                       "application/json; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._send(500, _json_bytes({"error": f"Внутренняя ошибка: {exc}"}),
                       "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("GET")


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
    return Server((host, port), Handler)
