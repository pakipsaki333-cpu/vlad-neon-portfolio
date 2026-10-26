"""Тесты панели: лендинг, подписка, QR, админка, реальные действия через CLI.

Запуск:  python3 doss/panel/tests/test_panel.py   (нужны fastapi, httpx, qrcode)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]        # .../doss
CLI = ROOT / "server" / "doss"

tmp = Path(tempfile.mkdtemp())
etc = tmp / "etc"
etc.mkdir()
os.environ.update({
    "DOSS_ETC": str(etc),
    "SUB_DIR": str(tmp / "sub"),
    "XRAY_BIN": str(tmp / "no-xray"),
    "XRAY_CONFIG": str(tmp / "xray" / "config.json"),
    "DOSS_NO_SERVICE": "1",
    "DOSS_BIN": str(CLI),
    "ADMIN_TOKEN": "secret-token",
    "PANEL_DEMO": "0",
})

sys.path.insert(0, str(ROOT / "panel"))

from fastapi.testclient import TestClient  # noqa: E402
import app as panel  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        print(f"  \033[31m✗\033[0m {name} {extra}")


# --- состояние как после install.sh + один пользователь
(etc / "state.json").write_text(json.dumps({
    "version": 1, "created": "2026-01-01T00:00:00Z",
    "server": {"host": "198.51.100.7", "port": 443, "dest_domain": "www.microsoft.com",
               "private_key": "P", "public_key": "PUBKEY", "short_ids": [""],
               "xhttp": {"enabled": True, "port": 8443, "path": "/doss-xh"},
               "awg": {"enabled": False}},
    "users": {},
}))
subprocess.run([str(CLI), "build-base"], check=True, capture_output=True)
subprocess.run([str(CLI), "user", "add", "anna", "--traffic", "200",
                "--expires", "2030-01-01"], check=True, capture_output=True)

state = json.loads((etc / "state.json").read_text())
token = state["users"]["anna"]["token"]

client = TestClient(panel.app)

print("== публичные страницы")
r = client.get("/")
check("лендинг 200", r.status_code == 200)
check("в лендинге есть цена и бренд", "119 ₽" in r.text and "Doss" in r.text)
check("нет незаполненных плейсхолдеров", "$" not in r.text)
check("healthz", client.get("/healthz").json()["ok"] is True)

print("== подписка")
r = client.get(f"/s/{token}")
check("подписка 200", r.status_code == 200)
check("два профиля (tcp + xhttp)", r.text.count("vless://") == 2)
check("адрес узла верный", "@198.51.100.7:443" in r.text)
check("комментарий со сроком", "# Doss" in r.text and "2030-01-01" in r.text)
check("чужой токен → 404", client.get("/s/deadbeef").status_code == 404)

print("== QR")
r = client.get(f"/qr/{token}.png")
check("QR отдаётся как png", r.status_code == 200 and r.headers["content-type"] == "image/png")

print("== админка")
check("без ключа — форма входа", "ADMIN_TOKEN" in client.get("/admin").text)
check("неверный ключ — 403-текст", "доступ только по токену" in client.get("/admin?key=nope").text)
r = client.get("/admin?key=secret-token")
check("с ключом — таблица", r.status_code == 200 and "anna" in r.text)

print("== действия через админку")
r = client.post("/admin/user/add?key=secret-token",
                data={"name": "boris", "traffic": "50", "expires": "2031-05-05"},
                follow_redirects=False)
check("создание редиректит", r.status_code == 303)
state = json.loads((etc / "state.json").read_text())
check("boris на сервере", "boris" in state["users"])
check("лимит 50 ГБ", state["users"]["boris"]["traffic_gb"] == 50)
check("срок сохранён", state["users"]["boris"]["expires"] == "2031-05-05")
check("uuid попал в конфиг Xray",
      "boris" in (tmp / "xray" / "config.json").read_text())

client.post("/admin/user/boris/toggle?key=secret-token", follow_redirects=False)
state = json.loads((etc / "state.json").read_text())
check("toggle выключил", state["users"]["boris"]["enabled"] is False)
check("выключенный скрыт из подписки",
      client.get(f"/s/{state['users']['boris']['token']}").status_code == 404)

client.post("/admin/user/boris/rm?key=secret-token", follow_redirects=False)
state = json.loads((etc / "state.json").read_text())
check("удаление сработало", "boris" not in state["users"])

print("== защита от мусора")
r = client.post("/admin/user/add?key=secret-token", data={"name": "плохое имя"}, follow_redirects=False)
check("нелатинское имя отклонено", "плохое" not in (etc / "state.json").read_text())
r = client.post("/admin/user/add?key=wrong", data={"name": "x"}, follow_redirects=False)
check("без ключа действие запрещено", r.status_code == 403)

print(f"\nитог: {PASS} ок, {FAIL} провал(ов)")
raise SystemExit(0 if FAIL == 0 else 1)
