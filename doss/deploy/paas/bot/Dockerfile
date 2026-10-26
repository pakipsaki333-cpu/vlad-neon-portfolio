# Doss · Telegram-бот для бесплатного PaaS (Render free web service)
FROM python:3.12-slim

# Контекст сборки — каталог doss/bot (там пакет dossbot и paas_run.py)
WORKDIR /app

COPY dossbot /app/dossbot
COPY paas_run.py /app/paas_run.py

RUN pip install --no-cache-dir "aiogram>=3.13,<4" "aiosqlite>=0.20"

ENV PYTHONUNBUFFERED=1
ENV PROVIDER=link
ENV DB_PATH=/tmp/doss.sqlite3
EXPOSE 8080

CMD ["python", "/app/paas_run.py"]
