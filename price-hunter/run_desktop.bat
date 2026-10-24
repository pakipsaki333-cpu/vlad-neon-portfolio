@echo off
chcp 65001 >nul
cd /d %~dp0
title Price Hunter - приложение

where python >nul 2>nul
if errorlevel 1 (
  echo [ОШИБКА] Python не найден. Либо поставь Python 3.11+ с python.org, либо скачай готовый PriceHunter.exe из GitHub Actions (см. README).
  pause
  exit /b 1
)
if not exist .venv (
  echo Создаю виртуальное окружение...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --quiet -r requirements-desktop.txt
start "" /b pythonw desktop_app.py
exit
