@echo off
chcp 65001 >nul
cd /d %~dp0
title Price Hunter - WB

where python >nul 2>nul
if errorlevel 1 (
  echo [ОШИБКА] Python не найден. Поставь Python 3.11+ с python.org, при установке отметь "Add python.exe to PATH", затем запусти этот файл снова.
  pause
  exit /b 1
)

if not exist .venv (
  echo Создаю виртуальное окружение...
  python -m venv .venv
)

call .venv\Scripts\activate.bat
echo Устанавливаю/обновляю зависимости...
python -m pip install --quiet -r requirements.txt

echo.
echo ============================================
echo  Price Hunter запущен: http://127.0.0.1:8000
echo  Не закрывай это окно, пока идёт охота.
echo  Остановка: Ctrl+C
echo ============================================
start "" http://127.0.0.1:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
