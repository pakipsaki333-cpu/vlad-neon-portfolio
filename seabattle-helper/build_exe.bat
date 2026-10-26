@echo off
chcp 65001 >nul
cd /d %~dp0
title Сборка SeaBattleHelper.exe

set "PY="
for %%V in (3.13 3.12 3.14 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY set "PY=python"

if not exist .venv %PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --quiet --disable-pip-version-check -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

if not exist build mkdir build
python -m seabattle --make-icon build\icon.ico
if errorlevel 1 goto :fail

pyinstaller --noconfirm --clean --onefile --windowed --name SeaBattleHelper --icon build\icon.ico --exclude-module tkinter launcher.py
if errorlevel 1 goto :fail

echo.
echo Готово: dist\SeaBattleHelper.exe
echo Проверка: dist\SeaBattleHelper.exe --selftest selftest_out
pause
exit /b 0

:fail
echo [ОШИБКА] Сборка не удалась - смотри сообщения выше.
pause
exit /b 1
