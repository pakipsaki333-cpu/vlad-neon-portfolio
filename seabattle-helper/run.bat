@echo off
chcp 65001 >nul
cd /d %~dp0
title Морской бой — помощник

rem Ищем подходящий Python (PySide6 6.11 работает с Python 3.10–3.14)
set "PY="
for %%V in (3.13 3.12 3.14 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ОШИБКА] Не найден Python 3.10-3.14.
  echo Поставь Python 3.13 с python.org ^(галочка "Add python.exe to PATH"^) и запусти этот файл снова.
  echo Или скачай готовый SeaBattleHelper.exe - см. README.md.
  pause
  exit /b 1
)

if not exist .venv (
  echo Создаю виртуальное окружение...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ОШИБКА] Не удалось создать .venv
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate.bat
echo Устанавливаю/обновляю зависимости ^(в первый раз - пара минут^)...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo [ОШИБКА] Не удалось установить зависимости. Удали папку .venv и попробуй снова.
  pause
  exit /b 1
)

echo Запускаю помощника...
start "" ".venv\Scripts\pythonw.exe" -m seabattle
