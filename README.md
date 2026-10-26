# ⚓ Морской бой — помощник

Маленькое окно поверх браузера: смотрит на экран, само видит «мимо / ранил / убил»
на поле соперника на любом сайте и подсказывает, куда бить, — с вероятностью попадания
для каждой клетки. Помогает расставить свои корабли, учится на исправлениях, есть
рамка-подсказка прямо поверх игры и встроенная тренировка. Windows-exe собирается в Actions.

➡ **Скачать, запустить и как это работает: [`seabattle-helper/`](seabattle-helper/README.md)**

```bat
cd seabattle-helper
run.bat
```

---

# 🎯 Price Hunter

Охотник за ценовыми ошибками и резкими падениями цен на Wildberries —
мониторинг выдачи, собственная история цен на SQLite, скоринг аномалий,
мгновенные алерты в Telegram и веб-панель находок.

➡ **Код, запуск для Windows и вся документация: [`price-hunter/`](price-hunter/README.md)**

```bat
cd price-hunter
run.bat
```
Панель: http://127.0.0.1:8000

---

# 🛡 Doss — свой VPN (бесплатный сервер + символическая подписка)

Собственная VPN-инфраструктура: бесплатный узел на Oracle Cloud Always Free,
VLESS + REALITY (+ XHTTP / AmneziaWG), CLI для доступа, Telegram-бот с подпиской
за Stars и веб-панель (лендинг + админка). Работает и **без карты**:
бесплатный PaaS (VLESS+WebSocket) или VPS с оплатой по СБП. 126 автотестов.

➡ **План, установка и ограничения: [`doss/README.md`](doss/README.md)**
➡ Где брать бесплатные сервера в 2026: [`doss/docs/free-servers.md`](doss/docs/free-servers.md)
➡ **Запустить за 3 клика (бесплатно, без карты): [`DEPLOY.md`](DEPLOY.md)**
➡ Нет карты? Четыре схемы запуска: [`doss/docs/no-card.md`](doss/docs/no-card.md)
➡ Telegram-бот: [`doss/bot/`](doss/bot/README.md) · панель: [`doss/panel/`](doss/panel/app.py) · Android: [`doss/android/`](doss/android/README.md)
