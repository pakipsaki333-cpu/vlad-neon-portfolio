"""
Doss Panel — лендинг + выдача подписок + админка поверх state.json сервера.

Запуск (прод):
    uvicorn app:app --host 127.0.0.1 --port 8080
Запуск (демо для показа):
    PANEL_DEMO=1 uvicorn app:app --host 0.0.0.0 --port 8000

Переменные окружения:
    DOSS_ETC       путь к каталогу состояния (по умолчанию /etc/doss)
    DOSS_BIN       CLI (по умолчанию doss)
    PANEL_DEMO=1   демо-режим: пример данных, без вызовов CLI
    ADMIN_TOKEN    пароль админки (в демо по умолчанию "doss-demo")
    SUB_BASE       публичный адрес панели для ссылок-подписок
    BRAND          название сервиса (по умолчанию Doss)
"""

from __future__ import annotations

import html
import io
import json
import os
import secrets
import shlex
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from string import Template

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, RedirectResponse

# ----------------------------------------------------------------- конфиг
DOSS_ETC = Path(os.environ.get("DOSS_ETC", "/etc/doss"))
STATE_PATH = DOSS_ETC / "state.json"
DOSS_CMD = shlex.split(os.environ.get("DOSS_BIN", "doss"))
DEMO = os.environ.get("PANEL_DEMO", "0") == "1"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "doss-demo" if DEMO else "")
SUB_BASE = os.environ.get("SUB_BASE", "").rstrip("/")
BRAND = os.environ.get("BRAND", "Doss")

app = FastAPI(title=f"{BRAND} panel", docs_url=None, redoc_url=None)

PRICE_RUB = 119
PRICE_STARS = 150
TRIAL_DAYS = 3
PLAN_DAYS = 30
PLAN_GB = 200


# ------------------------------------------------------------ состояние
def _demo_state() -> dict:
    today = date.today()
    def user(name, uuid_s, sid, token, days, used_gb, unlimited=False, enabled=True):
        return {
            "uuid": uuid_s, "short_id": sid, "token": token,
            "created": "2026-09-14T10:12:00Z",
            "enabled": enabled,
            "expires": None if unlimited else (today + timedelta(days=days)).isoformat(),
            "traffic_gb": PLAN_GB if not unlimited else 0,
            "unlimited": unlimited, "note": "", "used_gb": used_gb,
            "used_bytes": int(used_gb * 1_000_000_000),
        }
    return {
        "version": 1,
        "created": "2026-09-01T09:00:00Z",
        "server": {
            "host": "203.0.113.10", "port": 443, "dest_domain": "www.microsoft.com",
            "private_key": "DEMO_PRIVATE_KEY_DO_NOT_USE",
            "public_key": "q1w2E3r4T5y6U7i8O9p0AsDfGhJkLzXcVbNm12345",
            "short_ids": ["", "a1b2c3d4"],
            "xhttp": {"enabled": True, "port": 8443, "path": "/doss-xh"},
            "awg": {"enabled": False},
        },
        "users": {
            "vlad":   user("vlad", "11111111-1111-4111-8111-111111111111", "a1b2c3d4", "t0ken0vlad", 0, 42.7, unlimited=True),
            "anna":   user("anna", "22222222-2222-4222-8222-222222222222", "b2c3d4e5", "t0ken0anna", 24, 61.4),
            "boris":  user("boris", "33333333-3333-4333-8333-333333333333", "c3d4e5f6", "t0ken0boris", 11, 188.2),
            "old":    user("old", "44444444-4444-4444-8444-444444444444", "d4e5f6a7", "t0ken0old", -3, 200.0, enabled=False),
        },
    }


def load_state() -> dict:
    """Демо → пример; иначе — через CLI `doss export` (работает без прав на /etc/doss),
    с откатом на прямое чтение файла."""
    if DEMO:
        return _demo_state()
    try:
        out = subprocess.run([*DOSS_CMD, "export"], capture_output=True,
                             text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            raise HTTPException(500, "state.json повреждён")
    raise HTTPException(503, "состояние сервера недоступно (DOSS_BIN/export)")


def run_cli(*args: str) -> str:
    if DEMO:
        return "демо-режим: изменения не применяются"
    proc = subprocess.run([*DOSS_CMD, *args], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise HTTPException(500, f"ошибка CLI: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def link_for(state: dict, name: str) -> str:
    u = state["users"][name]
    s = state["server"]
    return (
        f"vless://{u['uuid']}@{s['host']}:{s['port']}"
        "?encryption=none&flow=xtls-rprx-vision&security=reality"
        f"&sni={s['dest_domain']}&fp=chrome&pbk={s['public_key']}"
        f"&sid={u['short_id']}&type=tcp#Doss%20{name}"
    )


def used_gb(u: dict) -> float:
    return round((u.get("used_bytes") or 0) / 1_000_000_000, 1)


def days_left(expires: str | None) -> int | None:
    if not expires:
        return None
    try:
        return (date.fromisoformat(expires) - date.today()).days
    except ValueError:
        return None


# ------------------------------------------------------------------- QR
def qr_png(data: str) -> Response | None:
    try:
        import qrcode  # необязательная зависимость
    except ImportError:
        return None
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


# --------------------------------------------------------------- шаблоны
BASE = Template(r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
  :root{
    --bg:#07070c; --panel:#0f1019; --line:#232538; --text:#e9e9f2; --muted:#9498b3;
    --neon:#8b5cf6; --neon2:#22d3ee; --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
  }
  *{box-sizing:border-box}
  body{margin:0;background:
      radial-gradient(900px 500px at 12% -10%, rgba(139,92,246,.28), transparent 60%),
      radial-gradient(700px 420px at 92% 0%, rgba(34,211,238,.18), transparent 60%),
      var(--bg);
    color:var(--text);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
    min-height:100vh}
  a{color:#c4b5fd}
  .wrap{max-width:1080px;margin:0 auto;padding:28px 20px 72px}
  .nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:44px}
  .logo{font-weight:800;letter-spacing:.5px;font-size:20px}
  .logo b{background:linear-gradient(92deg,var(--neon),var(--neon2));-webkit-background-clip:text;background-clip:text;color:transparent}
  .nav a{color:var(--muted);text-decoration:none;font-size:14px}
  h1{font-size:clamp(30px,5.2vw,54px);line-height:1.06;margin:0 0 18px;letter-spacing:-1.2px}
  h1 span{background:linear-gradient(92deg,#c4b5fd,#22d3ee);-webkit-background-clip:text;background-clip:text;color:transparent}
  h2{font-size:24px;margin:44px 0 14px;letter-spacing:-.4px}
  p.lead{color:var(--muted);font-size:18px;max-width:62ch}
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));margin-top:26px}
  .card{background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.015));
    border:1px solid var(--line);border-radius:16px;padding:18px}
  .card h3{margin:0 0 6px;font-size:16px}
  .card p{margin:0;color:var(--muted);font-size:14px}
  .price{display:flex;gap:14px;flex-wrap:wrap;margin-top:22px}
  .plan{flex:1 1 240px;border:1px solid var(--line);border-radius:18px;padding:22px;background:rgba(255,255,255,.03)}
  .plan.hot{border-color:rgba(139,92,246,.65);box-shadow:0 0 0 1px rgba(139,92,246,.25),0 18px 60px -30px rgba(139,92,246,.9)}
  .plan .sum{font-size:34px;font-weight:800;letter-spacing:-1px}
  .plan ul{padding-left:18px;color:var(--muted);font-size:14px;margin:12px 0 0}
  .btn{display:inline-block;margin-top:16px;padding:12px 18px;border-radius:12px;font-weight:700;
    text-decoration:none;color:#0b0b12;background:linear-gradient(92deg,#c4b5fd,#22d3ee);border:0;cursor:pointer;font-size:15px}
  .btn.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
  code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
  pre{background:#0a0b12;border:1px solid var(--line);border-radius:12px;padding:14px;overflow:auto}
  table{width:100%;border-collapse:collapse;font-size:14px;margin-top:12px}
  th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
  th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;border:1px solid var(--line)}
  .pill.ok{color:var(--ok);border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.08)}
  .pill.off{color:var(--bad);border-color:rgba(248,113,113,.35);background:rgba(248,113,113,.08)}
  .pill.warn{color:var(--warn);border-color:rgba(251,191,36,.35);background:rgba(251,191,36,.08)}
  .mono{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--muted)}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  input[type=text],input[type=password],input[type=date],input[type=number]{background:#0a0b12;border:1px solid var(--line);
    color:var(--text);border-radius:10px;padding:10px 12px;font-size:14px}
  .muted{color:var(--muted)}
  footer{margin-top:56px;color:#6b708c;font-size:13px;border-top:1px solid var(--line);padding-top:18px}
  .badge{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 10px}
</style></head><body><div class="wrap">
<div class="nav"><div class="logo">🛡 <b>$brand</b></div><div>$nav</div></div>
$body
<footer>
  $brand · личный VPN на своём железе · без рекламы, без логов трафика<br>
  Оплата в Telegram Stars · поддержка: @doss_support
</footer>
</div></body></html>""")


def page(title: str, body: str, nav: str = "") -> HTMLResponse:
    if not nav:
        nav = '<a href="/">главная</a> · <a href="/admin">админка</a>'
    return HTMLResponse(BASE.substitute(title=html.escape(title), brand=html.escape(BRAND),
                                        nav=nav, body=body))


# ------------------------------------------------------------- лендинг
@app.get("/", response_class=HTMLResponse)
def landing() -> HTMLResponse:
    state = None
    try:
        state = load_state()
    except HTTPException:
        pass
    server_line = ""
    if state:
        s = state["server"]
        server_line = (f'<div class="badge">узел {s["host"]} · REALITY {s["dest_domain"]} · '
                       f'{"XHTTP " + str(s["xhttp"]["port"]) if s["xhttp"]["enabled"] else "TCP 443"}</div>')

    body = f"""
<h1>Твой интернет.<br><span>Без рекламы и слежки.</span></h1>
<p class="lead">{html.escape(BRAND)} — личный VPN на собственных серверах: один узел в Европе,
VLESS+REALITY вместо «бесплатных» приложений из стора. Скорость как у домашнего интернета,
никаких логов трафика и продажи твоих данных.</p>
{server_line}

<div class="price">
  <div class="plan">
    <div class="muted">Пробный доступ</div>
    <div class="sum">0 ₽</div>
    <ul><li>{TRIAL_DAYS} дня</li><li>10 ГБ трафика</li><li>без карты и регистрации</li></ul>
    <a class="btn ghost" href="https://t.me/doss_vpn_bot">Попробовать</a>
  </div>
  <div class="plan hot">
    <div class="muted">Подписка</div>
    <div class="sum">{PRICE_RUB} ₽ <span style="font-size:14px;color:var(--muted)">/ {PLAN_DAYS} дней</span></div>
    <ul><li>{PLAN_GB} ГБ трафика</li><li>все локации</li><li>оплата в Telegram Stars</li><li>для «своих» — бесплатно</li></ul>
    <a class="btn" href="https://t.me/doss_vpn_bot">Оформить</a>
  </div>
</div>

<h2>Почему это работает там, где падает обычный VPN</h2>
<div class="grid">
  <div class="card"><h3>VLESS + REALITY</h3><p>Трафик неотличим от обычного HTTPS к крупному сайту —
    DPI не видит сигнатуру WireGuard, которой ловят 90% VPN в мобильных сетях.</p></div>
  <div class="card"><h3>XHTTP как запасной путь</h3><p>Второй транспорт в той же подписке: когда
    REALITY начинают резать в конкретной сети, клиент переключается на HTTP-мимикрию.</p></div>
  <div class="card"><h3>Своё железо</h3><p>Один узел, ноль «партнёрских» серверов и перепродажи
    трафика. Логи не пишутся вообще — только срок подписки и остаток лимита.</p></div>
</div>

<h2>Подключение за минуту</h2>
<div class="grid">
  <div class="card"><h3>1. Клиент</h3><p>Hiddify (Android/iOS/Windows) или v2rayNG. Оба бесплатные
    и открытые — ссылка из бота подходит к ним.</p></div>
  <div class="card"><h3>2. Ссылка</h3><p>Бот присылает <span class="mono">vless://…</span>.
    Копируешь, в клиенте «+» → «Импорт из буфера обмена».</p></div>
  <div class="card"><h3>3. Готово</h3><p>Включаешь туннель. Если сеть капризничает —
    переключаешь профиль (REALITY ↔ XHTTP) в том же клиенте.</p></div>
</div>

<h2>Честные ограничения</h2>
<div class="card"><p>Один бесплатный узел = общий канал 10 ТБ/мес. Поэтому лимит {PLAN_GB} ГБ
на человека и блокировка торрентов: иначе узел закроют по abuse-жалобе и доступ пропадёт у всех.
Локация одна (Европа) — для стриминговых каталогов других стран нужен второй узел, он появится
после ~6 платящих подписчиков.</p></div>
"""
    return page(f"{BRAND} — личный VPN", body)


# ------------------------------------------------------------ подписка
@app.get("/s/{token}", response_class=PlainTextResponse)
def subscription(token: str) -> PlainTextResponse:
    state = load_state()
    for name, u in state["users"].items():
        if secrets.compare_digest(u.get("token", ""), token) and u.get("enabled"):
            u_x = dict(u)
            u_x["token"] = token
            links = [link_for(state, name)]
            if state["server"]["xhttp"]["enabled"]:
                s = state["server"]
                links.append(
                    f"vless://{u['uuid']}@{s['host']}:{s['xhttp']['port']}"
                    "?encryption=none&security=reality"
                    f"&sni={s['dest_domain']}&fp=chrome&pbk={s['public_key']}"
                    f"&sid={u['short_id']}&type=xhttp&path={s['xhttp']['path']}&mode=auto"
                    f"#Doss%20{name}%20XHTTP"
                )
            head = (f"# {BRAND} • {name} • "
                    f"{'бессрочно' if not u.get('expires') else u['expires']}\n")
            return PlainTextResponse(head + "\n".join(links) + "\n")
    raise HTTPException(404, "подписка не найдена или отключена")


@app.get("/qr/{token}.png")
def qr_sub(token: str):
    state = load_state()
    for name, u in state["users"].items():
        if secrets.compare_digest(u.get("token", ""), token):
            resp = qr_png(link_for(state, name))
            if resp is None:
                raise HTTPException(501, "qrcode не установлен")
            return resp
    raise HTTPException(404, "нет такой подписки")


# --------------------------------------------------------------- админка
def _check_admin(key: str | None) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN не задан")
    if not key or not secrets.compare_digest(key, ADMIN_TOKEN):
        raise HTTPException(403, "доступ только по токену: /admin?key=...")


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, key: str = "") -> HTMLResponse:
    try:
        _check_admin(key or None)
    except HTTPException as e:
        return page("вход в админку", f"""
<h1>Админка {html.escape(BRAND)}</h1>
<p class="lead">{html.escape(str(e.detail))}</p>
<form method="get" action="/admin" class="row">
  <input type="password" name="key" placeholder="ADMIN_TOKEN" autofocus>
  <button class="btn" type="submit">Войти</button>
</form>""", nav='<a href="/">главная</a>')

    state = load_state()
    s = state["server"]
    users = state["users"]
    active = [u for u in users.values() if u.get("enabled")]
    traffic = sum(used_gb(u) for u in users.values())

    rows = ""
    for name, u in sorted(users.items()):
        left = days_left(u.get("expires"))
        if not u.get("enabled"):
            st = '<span class="pill off">выключен</span>'
        elif u.get("unlimited"):
            st = '<span class="pill ok">безлимит</span>'
        elif left is not None and left < 0:
            st = '<span class="pill off">истёк</span>'
        elif left is not None and left <= 3:
            st = '<span class="pill warn">истекает</span>'
        else:
            st = '<span class="pill ok">активен</span>'
        limit = "∞" if u.get("unlimited") else f'{u.get("traffic_gb", 0)} ГБ'
        rows += f"""<tr>
  <td><b>{html.escape(name)}</b><div class="mono">{u['uuid'][:8]}…</div></td>
  <td>{st}</td>
  <td>{html.escape(u.get('expires') or '—')}<div class="mono">{('осталось %d дн.' % left) if left is not None else ''}</div></td>
  <td>{used_gb(u)} / {limit}</td>
  <td class="mono">{html.escape(link_for(state, name)[:46])}…
      <div class="row" style="margin-top:6px">
        <a href="/qr/{u.get('token','')}.png">QR</a>
        <a href="/s/{u.get('token','')}">подписка</a>
      </div></td>
  <td>
    <form method="post" action="/admin/user/{html.escape(name)}/toggle?key={html.escape(key)}" style="display:inline">
      <button class="btn ghost" style="padding:6px 10px;margin:0;font-size:13px">
        {'выключить' if u.get('enabled') else 'включить'}</button>
    </form>
    <form method="post" action="/admin/user/{html.escape(name)}/rm?key={html.escape(key)}" style="display:inline">
      <button class="btn ghost" style="padding:6px 10px;margin:0;font-size:13px;color:var(--bad)">удалить</button>
    </form>
  </td>
</tr>"""

    flash = request.query_params.get("msg", "")
    body = f"""
<h1>Админка <span>{html.escape(BRAND)}</span></h1>
{'<div class="card" style="border-color:rgba(52,211,153,.4)">' + html.escape(flash) + '</div>' if flash else ''}
<div class="grid">
  <div class="card"><h3> {len(users)} аккаунтов</h3><p>{len(active)} активных</p></div>
  <div class="card"><h3>{traffic:.1f} ГБ</h3><p>суммарный учтённый трафик</p></div>
  <div class="card"><h3>{html.escape(s['dest_domain'])}</h3><p>маска REALITY · порт {s['port']}</p></div>
</div>

<h2>Новый аккаунт</h2>
<form method="post" action="/admin/user/add?key={html.escape(key)}" class="row">
  <input type="text" name="name" placeholder="имя (латиницей)" required>
  <input type="number" name="traffic" placeholder="ГБ, 200" value="200" min="1" style="width:130px">
  <input type="date" name="expires">
  <label class="muted"><input type="checkbox" name="unlimited" value="1"> безлимит</label>
  <button class="btn" type="submit">Создать</button>
</form>

<h2>Пользователи</h2>
<table><thead><tr>
  <th>имя</th><th>статус</th><th>срок</th><th>трафик</th><th>конфиг</th><th></th>
</tr></thead><tbody>{rows}</tbody></table>

<p class="muted" style="margin-top:18px">Трафик обновляется таймером <span class="mono">doss-traffic</span>
раз в час. Выключение пользователя сразу убирает его uuid из конфига Xray.</p>
"""
    return page("админка", body)


@app.post("/admin/user/add")
async def admin_add(request: Request, key: str = ""):
    _check_admin(key or None)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        return RedirectResponse(f"/admin?key={key}&msg=имя+только+латиница%2C+цифры%2C+-+и+_", 303)
    traffic = str(form.get("traffic") or "200")
    args = ["user", "add", name, "--traffic", traffic]
    expires = str(form.get("expires") or "").strip()
    if expires:
        args += ["--expires", expires]
    if form.get("unlimited"):
        args.append("--unlimited")
    try:
        run_cli(*args)
        msg = f"аккаунт {name} создан"
    except HTTPException as e:
        msg = str(e.detail)
    return RedirectResponse(f"/admin?key={key}&msg={msg}", 303)


@app.post("/admin/user/{name}/toggle")
def admin_toggle(name: str, key: str = ""):
    _check_admin(key or None)
    state = load_state()
    u = state["users"].get(name)
    if not u:
        raise HTTPException(404, "нет пользователя")
    flag = "--disable" if u.get("enabled") else "--enable"
    run_cli("user", "set", name, flag)
    return RedirectResponse(f"/admin?key={key}&msg={name}:{flag}", 303)


@app.post("/admin/user/{name}/rm")
def admin_rm(name: str, key: str = ""):
    _check_admin(key or None)
    run_cli("user", "rm", name)
    return RedirectResponse(f"/admin?key={key}&msg={name}+удалён", 303)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "demo": DEMO, "ts": int(time.time()),
            "state_exists": STATE_PATH.exists()}
