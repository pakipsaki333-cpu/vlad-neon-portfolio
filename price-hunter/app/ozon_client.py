"""Ozon — phase 2 (planned).

Why not now: Ozon aggressively blocks non-browser traffic (anti-bot
cookies, TLS fingerprinting, 403/Antibot challenge). The public
composer-API endpoint below works only from a real browser session
with fresh cookies, and breaks often. WB's open API gets us hunting
today; Ozon will be added behind this same interface once the WB
pipeline (history + detector + Telegram) is proven in the field.

Known starting point for phase 2:
  GET https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/product/<sku>/
  + cookies from a logged-in browser session, low request rate,
  or an official Ozon Seller/Performance API key if the user has one.
"""
from __future__ import annotations


class OzonNotImplemented(NotImplementedError):
    pass


class OzonClient:
    def search(self, query: str, page: int = 1):
        raise OzonNotImplemented(
            "Ozon — этап 2. Сейчас работает WB. См. docstring модуля: "
            "нужна браузерная сессия с куками или официальный API-ключ.")
