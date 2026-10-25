@echo off
chcp 65001 >nul
cd /d %~dp0
title Price Hunter - проверка WB и Ozon

if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
where python >nul 2>nul
if errorlevel 1 (
  echo Python не найден. Проверяй WB и Ozon кнопками в приложении PriceHunter.exe.
  pause
  exit /b 1
)
python -m pip install --quiet -r requirements-desktop.txt
python desktop_app.py --diagnose-all
echo.
echo Полный результат также сохранён в data\marketplace-diagnostics.json
pause
