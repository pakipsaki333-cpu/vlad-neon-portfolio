"""Запуск бота Doss на бесплатном PaaS (Render и т.п.).

PaaS требует, чтобы сервис слушал HTTP-порт → поднимаем крошечный health-сервер
и параллельно запускаем Telegram-бота (long polling). Так бот живёт без VPS
и без карты.

Переменные: BOT_TOKEN, DOSS_LINK, PORT, ADMIN... (см. doss/bot/.env.example)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from dossbot.bot import run as run_bot  # noqa: E402  (пакет рядом)


class Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"doss bot alive\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # тишина в логах PaaS
        return


def serve_health() -> None:
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("doss.paas")

    # health-сервер живёт всегда, даже если Telegram недоступен —
    # иначе PaaS сочтёт сервис упавшим и будет перезапускать контейнер.
    threading.Thread(target=serve_health, daemon=True).start()
    log.info("health-сервер слушает :%s", os.environ.get("PORT", "8080"))

    backoff = 10
    while True:
        try:
            asyncio.run(run_bot())
            log.warning("бот остановился штатно — перезапуск через %ss", backoff)
        except SystemExit as e:            # нет BOT_TOKEN — это ошибка конфигурации
            log.error("конфигурация бота: %s", e)
            time.sleep(60)
            continue
        except Exception:                  # сеть, Telegram 5xx, падение хэндлера
            log.exception("бот упал — перезапуск через %ss", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
