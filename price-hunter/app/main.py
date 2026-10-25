"""Price Hunter — WB price-error scanner with Telegram alerts.

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
def alerts(limit: int = 100):
    return {"alerts": db.alerts(limit=min(limit, 500))}


@app.post("/api/alerts/{alert_id}/dismiss")
def dismiss(alert_id: int):
    db.dismiss_alert(alert_id)
    return {"ok": True}


@app.get("/api/products")
def products(limit: int = 100):
    return {"products": db.products(limit=min(limit, 500))}


@app.get("/api/product/{nm}/history")
def history(nm: int):
    return {"nm": nm, "history": db.product_history(nm)}


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
    """Connectivity verdict: outcome/message/hint + per-endpoint attempts."""
    return scanner.diagnose()


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
    scanner.reset_mode()  # mode/dest/transport may have changed
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
