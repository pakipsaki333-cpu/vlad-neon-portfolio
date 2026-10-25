@echo off
chcp 65001 >nul
cd /d %~dp0
title Price Hunter - проверка WB

if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
where python >nul 2>nul
if errorlevel 1 (
  echo Python не найден. Проверяй WB кнопкой в приложении PriceHunter.exe.
  pause
  exit /b 1
)
python -m pip install --quiet -r requirements-desktop.txt
python desktop_app.py --diagnose-wb
echo.
echo Полный результат также сохранён в data\wb-diagnostics.json
pause
