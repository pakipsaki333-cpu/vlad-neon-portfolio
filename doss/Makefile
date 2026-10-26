# Doss — частые команды. Все тесты работают без сервера и без Telegram-токена.
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
PYTEST_DEPS = fastapi uvicorn qrcode[pil] python-multipart httpx aiogram aiosqlite

.PHONY: help test test-server test-bot test-panel panel bot venv clean

help:
	@echo "make test         — все тесты (server + bot + panel)"
	@echo "make test-server  — CLI doss (33 проверки)"
	@echo "make test-bot     — логика бота (26) + интеграция с CLI (10)"
	@echo "make test-panel   — панель/подписки/админка (23)"
	@echo "make panel        — демо-панель на http://0.0.0.0:8000"
	@echo "make bot          — запуск бота (нужен BOT_TOKEN в doss/bot/.env)"
	@echo "make venv         — окружение .venv с зависимостями"

test: test-server test-bot test-panel

test-server:
	bash server/tests/test_cli.sh

test-bot:
	$(PY) bot/tests/selftest.py
	$(PY) bot/tests/test_provider_cli.py

test-panel:
	@$(PY) -c "import fastapi" 2>/dev/null || (echo "нужен venv: make venv" && exit 1)
	$(PY) panel/tests/test_panel.py

venv:
	$(PY) -m venv .venv
	./.venv/bin/pip install --quiet --upgrade pip
	./.venv/bin/pip install --quiet -r panel/requirements.txt -r bot/requirements.txt httpx

panel:
	PANEL_DEMO=1 ADMIN_TOKEN=doss-demo ./.venv/bin/uvicorn app:app --app-dir panel --host 0.0.0.0 --port 8000

bot:
	cd bot && ../.venv/bin/python -m dossbot

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .venv

