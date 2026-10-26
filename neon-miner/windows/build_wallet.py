#!/usr/bin/env python3
"""Собирает автономный офлайн-генератор XMR-кошелька dist/NeonMiner-wallet.html.

    python3 windows/build_wallet.py

Склеивает wallet_template.html + wallet_crypto.js + официальный wordlist
(1626 слов из monero-project/monero/src/mnemonics/english.h) в ОДИН файл.
Результат не содержит ни одного сетевого запроса и жёстко ограничен CSP.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST = ROOT / "dist"
TEMPLATE = HERE / "web" / "wallet_template.html"
CRYPTO = HERE / "web" / "wallet_crypto.js"
WORDLIST = HERE / "wordlist_en.json"
OUT = DIST / "NeonMiner-wallet.html"
ZIP = DIST / "NeonMiner-wallet.zip"

FORBIDDEN = (
    "fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "importScripts",
    "<script src", "<link href", "http://", "https://",
)


def build() -> str:
    words = json.loads(WORDLIST.read_text(encoding="utf-8"))
    if len(words) != 1626 or words[0] != "abbey" or words[-1] != "zoom":
        raise SystemExit("wordlist_en.json повреждён: ожидается 1626 слов Monero English")
    if words[1083] != "plus":
        raise SystemExit("wordlist_en.json: не тот порядок слов (проверка words[1083])")

    template = TEMPLATE.read_text(encoding="utf-8")
    crypto = CRYPTO.read_text(encoding="utf-8")
    word_js = "window.NM_WORDS = " + json.dumps(words, ensure_ascii=False, separators=(",", ":")) + ";"

    html = template.replace("/*__NM_WORDS__*/", word_js).replace("/*__NM_CRYPTO__*/", crypto)
    if "__NM_" in html:
        raise SystemExit("в шаблоне остались незаполненные плейсхолдеры")

    # Гарантия офлайн-режима: в файле не должно быть ни одного сетевого вызова.
    found = [token for token in FORBIDDEN if token in html]
    if found:
        raise SystemExit(f"ОБНАРУЖЕНЫ сетевые вызовы/ссылки: {found}")
    if "Content-Security-Policy" not in html or "connect-src 'none'" not in html:
        raise SystemExit("нет жёсткого CSP")
    if "getRandomValues" not in html:
        raise SystemExit("нет генерации случайных чисел")

    DIST.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")

    # Архив нужен потому, что GitHub raw / jsDelivr отдают .html как text/plain —
    # браузер показывает исходник вместо страницы. .zip скачивается одним кликом.
    fixed = zipfile.ZipInfo("NeonMiner-wallet.html", date_time=(2026, 10, 26, 12, 0, 0))
    fixed.compress_type = zipfile.ZIP_DEFLATED
    fixed.external_attr = 0o644 << 16
    readme = zipfile.ZipInfo("КАК-ОТКРЫТЬ.txt", date_time=(2026, 10, 26, 12, 0, 0))
    readme.compress_type = zipfile.ZIP_DEFLATED
    readme.external_attr = 0o644 << 16
    with zipfile.ZipFile(ZIP, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(fixed, html)
        z.writestr(readme,
                   "NeonMiner Wallet — офлайн-генератор кошелька Monero\n"
                   "=================================================\n\n"
                   "1. Распакуй NeonMiner-wallet.html в любую папку.\n"
                   "2. Открой его двойным кликом — страница заработает в браузере.\n"
                   "3. Для максимальной безопасности отключи интернет перед генерацией.\n"
                   "4. Нажми «Создать кошелёк», ЗАПИШИ 25 СЛОВ НА БУМАГУ.\n"
                   "5. XMR-адрес вставь в NeonMiner.exe.\n\n"
                   "В файле стоит CSP default-src 'none' — сетевых запросов нет.\n"
                   "Никому не отправляй сид-фразу и приватные ключи.\n"
                   "Не используй депозитный адрес биржи.\n")
    return html


def main() -> int:
    html = build()
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    zip_digest = hashlib.sha256(ZIP.read_bytes()).hexdigest()
    print(f"✓ {OUT.relative_to(ROOT)} — {len(html.encode('utf-8')):,} байт")
    print(f"✓ {ZIP.relative_to(ROOT)} — {ZIP.stat().st_size:,} байт (один клик → скачать)")
    print(f"  SHA256: {digest}")
    print("  CSP: default-src 'none' · connect-src 'none' · сетевых вызовов нет")
    print("  Вектор: 25×'plus' → 44bUDLb1…87C6VR (проверяется и в браузере, и в тестах)")
    sums = DIST / "SHA256SUMS.txt"
    old = sums.read_text(encoding="utf-8") if sums.exists() else (
        "# Контрольные суммы файлов NeonMiner\n")
    old = "\n".join(line for line in old.splitlines()
                    if line.strip() and "NeonMiner-wallet" not in line)
    sums.write_text(old.rstrip() + "\n"
                    + f"{digest}  NeonMiner-wallet.html\n"
                    + f"{zip_digest}  NeonMiner-wallet.zip\n",
                    encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
