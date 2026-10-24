@echo off
chcp 65001 >nul
cd /d %~dp0
title Сборка PriceHunter.exe

where python >nul 2>nul
if errorlevel 1 (
  echo [ОШИБКА] Python не найден. Поставь Python 3.11+ с python.org, отметь "Add python.exe to PATH", запусти снова.
  echo Либо просто скачай готовый PriceHunter.exe: GitHub - Actions - последний запуск Build Windows - Artifacts.
  pause
  exit /b 1
)

if not exist .venv-build (
  echo Создаю окружение сборки...
  python -m venv .venv-build
)
call .venv-build\Scripts\activate.bat
echo Ставлю зависимости и PyInstaller...
python -m pip install --quiet -r requirements-desktop.txt pyinstaller

echo Прогоняю тесты и самотест движка...
python -m unittest discover -s tests
if errorlevel 1 ( echo [ОШИБКА] Тесты упали - сборка отменена. & pause & exit /b 1 )
python desktop_app.py --self-test
if errorlevel 1 ( echo [ОШИБКА] Самотест движка упал - сборка отменена. & pause & exit /b 1 )

echo Собираю PriceHunter.exe (пара минут)...
pyinstaller --clean --noconfirm PriceHunter.spec
if errorlevel 1 ( echo [ОШИБКА] PyInstaller не справился. & pause & exit /b 1 )

echo.
echo ============================================
echo  ГОТОВО: dist\PriceHunter.exe
echo  Это один файл - можно копировать куда угодно.
echo  Настройки и база цен будут жить в %%APPDATA%%\PriceHunter
echo ============================================
start "" explorer dist
pause
