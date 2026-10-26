@echo off
REM NeonMiner — запуск веб-клиента на Windows. Нужен только Python 3.9+.
cd /d "%~dp0"
set HOST=127.0.0.1
set PORT=8000
echo NeonMiner ^-^> http://127.0.0.1:8000
python -m neon_miner serve --host %HOST% --port %PORT%
pause
