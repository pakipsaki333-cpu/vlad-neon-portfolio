# ◈ NeonMiner — клиент для майнинга XMR на свой кошелёк

Вписываешь Monero-адрес → выбираешь пул → получаешь готовый конфиг XMRig и
дашборд с балансом, хэшрейтом и выплатами. Четыре пула с проверенными API
(C3Pool, HashVault, MoneroOcean, SupportXMR), валидация адреса по контрольной
сумме Keccak, честный калькулятор доходности, загрузка официального XMRig с
проверкой SHA256. Только стандартная библиотека Python, 62 теста.

🪟 **Готовый Windows-.exe:** [`neon-miner/dist/NeonMiner.exe`](neon-miner/dist/NeonMiner.exe)
(327 КБ, один файл, без Python) · [инструкция](neon-miner/dist/README-Windows.md) ·
[SHA256](neon-miner/dist/SHA256SUMS.txt). Двойной клик → панель в браузере →
вписал кошелёк → «Начать майнинг». XMRig скачивается сам с проверкой SHA256.

```bash
cd neon-miner && ./run.sh       # Python-версия; Windows: run.bat или NeonMiner.exe
```
Панель: http://127.0.0.1:8000 · документация: [`neon-miner/README.md`](neon-miner/README.md)

> Майнинг только на своём железе и только вручную. Скрытого майнинга на
> посетителях сайта и на чужих/бесплатных серверах в модуле нет.

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
