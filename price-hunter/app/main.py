"""Price Hunter — WB/Ozon price-error scanner with Telegram alerts.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
UI:   http://127.0.0.1:8000
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, DB_FILE, Settings
from .db import DB
from .scanner import Scanner
from .telegram import TelegramNotifier

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

settings = Settings()
db = DB(DB_FILE)
scanner = Scanner(db, settings)

app = FastAPI(title="Price Hunter", version="1.0.0")
STATIC = BASE_DIR / "static"
_MARKETPLACES = ("wb", "ozon")


def _marketplace_or_400(marketplace: str | None,
                        default: str | None = None) -> str | None:
    value = (marketplace or default or "").lower() or None
    if value is not None and value not in _MARKETPLACES:
        raise HTTPException(400, "marketplace должен быть wb или ozon")
    return value


@app.on_event("startup")
def _startup():
    # Start hunting immediately; user can pause from the UI.
    scanner.start()


# --------------------------------------------------------------------- #
@app.get("/api/status")
def status():
    return {**scanner.status(), "stats": db.stats(),
            "settings": settings.public()}


@app.get("/api/alerts")
def alerts(limit: int = 100, marketplace: str | None = None,
           history_confirmed: bool = False):
    return {"alerts": db.alerts(
        limit=min(limit, 500),
        marketplace=_marketplace_or_400(marketplace),
        only_history_confirmed=history_confirmed)}


@app.post("/api/alerts/{alert_id}/dismiss")
def dismiss(alert_id: int):
    db.dismiss_alert(alert_id)
    return {"ok": True}


@app.get("/api/products")
def products(limit: int = 100, marketplace: str | None = None):
    return {"products": db.products(
        limit=min(limit, 500),
        marketplace=_marketplace_or_400(marketplace))}


@app.get("/api/lowest-price")
def lowest_price(query: str, marketplaces: str | None = None,
                 pages: int = 2, limit: int = 50,
                 strict_match: bool = False, record: bool = True):
    """Manual cross-marketplace search for the cheapest current offer."""
    selected = None
    if marketplaces:
        selected = [
            _marketplace_or_400(value.strip())
            for value in marketplaces.split(",") if value.strip()
        ]
    try:
        return scanner.find_lowest_prices(
            query, marketplaces=selected, pages=pages, limit=limit,
            strict_match=strict_match, record=record)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/product/{nm}/history")
def history(nm: int, marketplace: str = "wb"):
    selected = _marketplace_or_400(marketplace, "wb")
    return {"nm": nm, "marketplace": selected,
            "history": db.product_history(nm, marketplace=selected)}


@app.get("/api/product/{marketplace}/{nm}/history")
def history_by_marketplace(marketplace: str, nm: int):
    selected = _marketplace_or_400(marketplace)
    return {"nm": nm, "marketplace": selected,
            "history": db.product_history(nm, marketplace=selected)}


@app.get("/api/runs")
def runs(limit: int = 20):
    return {"runs": db.runs(limit=min(limit, 100))}


@app.post("/api/scan/start")
def scan_start():
    return {"started": scanner.start(), **scanner.status()}


@app.post("/api/scan/stop")
def scan_stop():
    return {"stopped": scanner.stop(), **scanner.status()}


@app.post("/api/scan/now")
def scan_now():
    scanner.trigger()
    return {"ok": True, **scanner.status()}


@app.post("/api/wb/diagnose")
def wb_diagnose():
    """WB connectivity verdict: outcome/message/hint + attempts."""
    return scanner.diagnose_wb()


@app.post("/api/ozon/diagnose")
def ozon_diagnose():
    """Ozon connectivity verdict: outcome/message/hint + attempts."""
    return scanner.diagnose_ozon()


@app.post("/api/marketplaces/diagnose")
def marketplaces_diagnose():
    """Check every enabled marketplace independently."""
    return scanner.diagnose_all()


@app.post("/api/diagnose/{marketplace}")
def marketplace_diagnose(marketplace: str):
    return scanner.diagnose(marketplace=_marketplace_or_400(marketplace))


@app.get("/api/settings")
def get_settings():
    return settings.public()


@app.post("/api/settings")
async def post_settings(req: Request):
    patch = await req.json()
    # Empty token field in UI means "keep existing".
    if patch.get("telegram_token") in ("", None) and settings.get()["telegram_token"]:
        patch.pop("telegram_token", None)
    updated = settings.update(patch)
    scanner.reset_mode()  # mode/marketplaces/transports may have changed
    return settings.public()


@app.post("/api/telegram/test")
def telegram_test():
    cfg = settings.get()
    n = TelegramNotifier(cfg["telegram_token"], cfg["telegram_chat_id"])
    if not n.configured:
        raise HTTPException(400, "Сначала укажи токен бота и chat_id в настройках")
    if not n.send_test():
        raise HTTPException(502, "Telegram не принял сообщение. Проверь токен, "
                                 "chat_id и что ты писал боту (/start).")
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
