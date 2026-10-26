#!/usr/bin/env python3
"""Сборка NeonMiner.exe (Windows x86-64) из этого же исходника.

    python3 windows/build_windows.py

Что делает:
  1. Генерирует build/embed.h — вшивает HTML/CSS/JS, каталог пулов, демо-данные
     и параметры последнего официального релиза XMRig (api.github.com).
  2. Кросс-компилирует neonminer.c → dist/NeonMiner.exe через Zig/Clang.
  3. Собирает Linux-близнеца, прогоняет --selftest и поднимает его для проверки.
  4. Пишет dist/SHA256SUMS.txt.

Зависимости: Python 3.9+ и ziglang (ставится автоматически с pypi.org).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                     # neon-miner/
sys.path.insert(0, str(ROOT))

from neon_miner.http_client import demo_view  # noqa: E402
from neon_miner.pools import catalog, POOLS   # noqa: E402

BUILD = HERE / "build"
DIST = ROOT / "dist"
WEB = HERE / "web"


# Значения по умолчанию, если api.github.com недоступен в момент сборки.
FALLBACK_RELEASE = {
    "tag": "v6.26.0",
    "asset": "xmrig-6.26.0-windows-x64.zip",
    "size": 2_720_812,
    "html_url": "https://github.com/xmrig/xmrig/releases/tag/v6.26.0",
}


def latest_xmrig() -> dict:
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/xmrig/xmrig/releases/latest",
            headers={"User-Agent": "NeonMiner-builder/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        tag = data["tag_name"]
        asset_name = f"xmrig-{tag.lstrip('v')}-windows-x64.zip"
        assets = {a["name"]: a for a in data["assets"]}
        if asset_name not in assets or "SHA256SUMS" not in assets:
            raise ValueError("в релизе нет нужного ассета или SHA256SUMS")
        return {"tag": tag, "asset": asset_name, "size": assets[asset_name]["size"],
                "html_url": data.get("html_url", "")}
    except Exception as exc:  # noqa: BLE001
        print(f"  ! api.github.com недоступен ({exc}), использую проверенный {FALLBACK_RELEASE['tag']}")
        return dict(FALLBACK_RELEASE)


def c_lit(value: str) -> str:
    """Однострочный C-литерал."""
    out = (str(value).replace("\\", "\\\\").replace('"', '\\"')
           .replace("\t", "\\t").replace("\r", "").replace("\n", "\\n")
           .replace("?", "\\?"))
    return f'"{out}"'


def c_blob(name: str, value: str) -> str:
    """Многострочный C-литерал: режем по строкам, чтобы не разорвать escape-последовательность."""
    lines = value.split("\n")
    chunks = []
    for i, line in enumerate(lines):
        e = (line.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\t", "\\t").replace("\r", "").replace("?", "\\?"))
        chunks.append(f'    "{e}' + ("\\n" if i < len(lines) - 1 else "") + '"')
    return f"static const char {name}[] =\n" + "\n".join(chunks) + ";\n\n"


def make_embed(release: dict) -> str:
    tag = release["tag"]
    ver = tag.lstrip("v")
    base = f"https://github.com/xmrig/xmrig/releases/download/{tag}"

    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "neon_miner" / "static" / "style.css").read_text(encoding="utf-8")
    css += "\n\n/* ---- exe-only ---- */\n" + (WEB / "extra.css").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    svg = (ROOT / "neon_miner" / "static" / "favicon.svg").read_text(encoding="utf-8")

    cat = {"pools": catalog(), "checked_at": "2026-10-26", "default_pool": "c3pool"}
    cat_json = json.dumps(cat, ensure_ascii=False, separators=(",", ":"))
    rel_json = json.dumps({
        "ok": True, "version": tag, "asset": release["asset"],
        "size_mb": round(release["size"] / 1_000_000, 2),
        "url": release["html_url"],
        "source": "api.github.com/repos/xmrig/xmrig/releases/latest",
    }, ensure_ascii=False, separators=(",", ":"))

    out = ["/* СГЕНЕРИРОВАНО build_windows.py — не редактировать вручную. */\n",
           "#ifndef NEONMINER_EMBED_H\n#define NEONMINER_EMBED_H\n\n"]
    out.append(f"#define APP_BUILD_TAG {c_lit(tag)}\n")
    out.append(f"#define XMRIG_VERSION {c_lit(tag)}\n")
    out.append(f"#define XMRIG_ASSET {c_lit(release['asset'])}\n")
    out.append(f"#define XMRIG_ZIP_URL {c_lit(base + '/' + release['asset'])}\n")
    out.append(f"#define XMRIG_SUMS_URL {c_lit(base + '/SHA256SUMS')}\n")
    out.append(f"#define XMRIG_EXE_RELPATH {c_lit(f'xmrig-{ver}/xmrig.exe')}\n")
    out.append(f"#define DEFAULT_POOL_ID {c_lit('c3pool')}\n\n")

    out.append("""
typedef struct { int port; int tls; int difficulty; const char *label; } PortDef;
typedef struct {
    const char *id, *name, *tagline, *api_kind, *api_base;
    const char *stats_path, *payments_path, *pool_path;
    const char *host, *region;
    double fee_pct, min_payout, default_payout;
    PortDef ports[8]; int nports;
    const char *failover_host; int failover_port, failover_tls;
    int recommended;
} PoolDef;
typedef struct { const char *id, *json; } DemoDef;

""")

    rows = []
    for p in POOLS.values():
        ports = ", ".join(f"{{{x.port}, {1 if x.tls else 0}, {x.difficulty}, {c_lit(x.label)}}}"
                          for x in p.ports)
        fh = c_lit(p.failover_host) if p.failover_host else "NULL"
        rows.append("    {" + ", ".join([
            c_lit(p.id), c_lit(p.name), c_lit(p.tagline), c_lit(p.api_kind),
            c_lit(p.api_base), c_lit(p.stats_path), c_lit(p.payments_path),
            c_lit(p.pool_path), c_lit(p.host), c_lit(p.region),
            f"{p.fee_pct:.4f}", f"{p.min_payout_xmr:.8f}", f"{p.default_payout_xmr:.8f}",
            "{" + ports + "}", str(len(p.ports)), fh,
            str(p.failover_port or 0), "1" if p.failover_tls else "0",
            "1" if p.recommended else "0",
        ]) + "}")
    out.append("static const PoolDef POOL_DEFS[] = {\n" + ",\n".join(rows) + "\n};\n")
    out.append(f"static const int N_POOLS = {len(rows)};\n\n")

    demos = []
    for pid in POOLS:
        view = demo_view(pid)
        # Фиксируем метку времени, чтобы сборка была байт-в-байт воспроизводимой.
        view["updated_at"] = 1793040612
        demos.append("    {" + c_lit(pid) + ", " + c_lit(json.dumps(view, ensure_ascii=False, separators=(",", ":"))) + "}")
    out.append("static const DemoDef DEMO_DEFS[] = {\n" + ",\n".join(demos) + "\n};\n")
    out.append(f"static const int N_DEMOS = {len(demos)};\n\n")

    out.append(c_blob("EMBED_CATALOG_JSON", cat_json))
    out.append(c_blob("EMBED_RELEASE_JSON", rel_json))
    out.append(c_blob("EMBED_INDEX_HTML", html))
    out.append(c_blob("EMBED_STYLE_CSS", css))
    out.append(c_blob("EMBED_APP_JS", js))
    out.append(c_blob("EMBED_FAVICON_SVG", svg))
    out.append("#endif /* NEONMINER_EMBED_H */\n")
    return "".join(out)


def find_zig() -> str:
    candidates = [
        "/tmp/zigpkg/ziglang/zig",
        str(Path.home() / ".local" / "zigpkg" / "ziglang" / "zig"),
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    from shutil import which
    z = which("zig")
    if z:
        return z
    print("  · ziglang не найден, ставлю с pypi.org…")
    target = "/tmp/zigpkg"
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "--target", target, "ziglang==0.17.0"], check=True)
    return str(Path(target) / "ziglang" / "zig")


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)

    print("=== 1. Параметры релиза XMRig ===")
    release = latest_xmrig()
    print(f"    {release['tag']} · {release['asset']} · {release['size']/1e6:.2f} МБ")

    print("=== 2. Генерация embed.h ===")
    embed = make_embed(release)
    (BUILD / "embed.h").write_text(embed, encoding="utf-8")
    print(f"    {len(embed):,} байт, {embed.count(chr(10)):,} строк")

    zig = find_zig()

    src = str(HERE / "neonminer.c")
    win_out = str(DIST / "NeonMiner.exe")
    print("=== 3. Кросс-компиляция Windows x86-64 ===")
    cmd = [zig, "cc", "-target", "x86_64-windows-gnu", "-O2", "-std=c11",
           "-I", str(BUILD), "-o", win_out, src,
           "-lws2_32", "-lwinhttp", "-lshell32", "-luser32"]
    print("   ", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout[-6000:]); print(r.stderr[-6000:]); return 1
    if r.stderr.strip():
        print("    warnings:", r.stderr.strip()[:1500])

    # Отладочные символы пользователю не нужны — dist содержит только то, что нужно.
    for pdb in DIST.glob("*.pdb"):
        pdb.unlink()

    exe = Path(win_out).read_bytes()
    pe_off = int.from_bytes(exe[0x3c:0x40], "little")
    assert exe[:2] == b"MZ" and exe[pe_off:pe_off + 4] == b"PE\0\0", "не PE!"
    machine = int.from_bytes(exe[pe_off + 4:pe_off + 6], "little")
    assert machine == 0x8664, f"не x86-64: {machine:#x}"
    print(f"    OK: {len(exe):,} байт, MZ/PE, machine=x86-64")

    print("=== 4. Linux-близнец + selftest ===")
    lin_out = str(BUILD / "neonminer-test")
    cmd = ["gcc", "-O2", "-std=c11", "-Wall", "-I", str(BUILD), "-o", lin_out, src, "-lm"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout[-6000:]); print(r.stderr[-8000:]); return 1
    if r.stderr.strip():
        print("    warnings:\n", r.stderr.strip()[:3000])
    st = subprocess.run([lin_out, "--selftest"], capture_output=True, text=True)
    print(st.stdout.strip())
    if st.returncode:
        print("SELFTEST ПРОВАЛЕН", st.stderr[-2000:]); return 1

    print("=== 5. Контрольные суммы и инструкция ===")
    digest = hashlib.sha256(exe).hexdigest()
    (DIST / "SHA256SUMS.txt").write_text(
        f"# NeonMiner.exe — собран {release['tag']} из windows/neonminer.c\n"
        f"# Проверка в PowerShell: (Get-FileHash .\\NeonMiner.exe -Algorithm SHA256).Hash\n"
        f"{digest}  NeonMiner.exe\n", encoding="utf-8")
    write_windows_readme(digest, release, len(exe))
    print(f"    SHA256 {digest}")
    print(f"    dist/: NeonMiner.exe, SHA256SUMS.txt, README-Windows.md")
    return 0


def write_windows_readme(digest: str, release: dict, size: int) -> None:
    (DIST / "README-Windows.md").write_text(f"""# NeonMiner.exe — инструкция

**Файл:** `NeonMiner.exe` · {size:,} байт · Windows 10/11 x86-64 · версия 1.1.0
**SHA256:** `{digest}`

Проверить файл перед запуском (PowerShell в этой папке):

```powershell
(Get-FileHash .\\NeonMiner.exe -Algorithm SHA256).Hash.ToLower()
```

Должно получиться ` {digest} `.

---

## Запуск — два шага

1. **Двойной клик по `NeonMiner.exe`.** Откроется консольное окно с адресом
   панели и автоматически запустится браузер: `http://127.0.0.1:8765`
   (если порт занят — программа сама возьмёт следующий свободный, 8766…8790).
   Консоль можно свернуть, но **не закрывайте** её, пока майнинг идёт: закрытие
   останавливает и панель, и майнер.
2. В панели вставьте **свой Monero-адрес** (95 символов), поставьте галочку
   «Это мой компьютер…» и нажмите **«▶ Начать майнинг»**.

Первый запуск сам:
- проверит контрольную сумму адреса (Keccak-256, защита от опечатки);
- запишет `config.json` под ваш кошелёк и выбранный пул;
- скачает `{release['asset']}` с **официального** релиза
  [github.com/xmrig/xmrig/releases/{release['tag']}](https://github.com/xmrig/xmrig/releases/{release['tag']});
- скачает `SHA256SUMS` того же релиза и **сверит хэш** — при несовпадении
  архив удаляется и майнер НЕ запускается;
- распакует его (`tar.exe`, запасной вариант `Expand-Archive`) и запустит
  `xmrig.exe --config=config.json` с выводом в окно «Лог XMRig».

Повторные запуски скачивание не повторяют. Кнопка **«■ Стоп»** завершает процесс.

Всё лежит в `%LOCALAPPDATA%\\NeonMiner\\` (обычно
`C:\\Users\\<имя>\\AppData\\Local\\NeonMiner\\`): `config.json`, `xmrig.log`,
`SHA256SUMS`, архив и распакованный XMRig. Никаких записей в реестр,
автозагрузку и планировщик задач.

---

## ⚠ Что скажет Windows

- **SmartScreen «Windows защитила ваш компьютер».** Файл собран из открытого
  исходника и **не подписан** EV-сертификатом (он стоит денег). Это нормально
  для самодельной утилиты. Нажмите «Дополнительные сведения» → «Выполнить в любом случае»,
  **предварительно сверив SHA256** командой выше.
- **Антивирус ругается на XMRig.** Майнеры часто помечают как PUA/«RiskWare.Miner».
  Мы качаем только официальный бинарник с GitHub и проверяем его SHA256.
  Если антивирус удалит `xmrig.exe`, добавьте папку `%LOCALAPPDATA%\\NeonMiner`
  в исключения — но только если вы сами запускали NeonMiner и понимаете, что это.
- **Брандмауэр.** Разрешите `NeonMiner.exe` доступ к **localhost** (127.0.0.1);
  исходящие соединения нужны к пулу (порт 13333/443) и github.com (443).

## Если что-то не так

| Симптом | Что делать |
|---|---|
| Окно сразу закрылось | Запустите из `cmd`: `NeonMiner.exe --no-browser` — увидите текст ошибки |
| Браузер не открылся | Откройте `http://127.0.0.1:8765` вручную (порт указан в консоли) |
| «SHA256SUMS: …» / «загрузка XMRig: …» | Нет интернета или github.com заблокирован. Скачайте `{release['asset']}` и `SHA256SUMS` вручную в `%LOCALAPPDATA%\\NeonMiner\\` и нажмите Старт снова |
| «не удалось распаковать архив» | Нужен Windows 10 1803+ (`tar.exe`) или PowerShell 5+. Обновите Windows |
| Статистика пустая | PPLNS: первые шары появляются через 10–30 минут. Нажмите «Обновить» |
| Баланс не растёт | Проверьте в логе строку `accepted` и что адрес валиден |

## Честно про деньги

При 5 kH/s и 0.36 €/кВт·ч калькулятор в панели покажет около **−0.39 €/сутки**:
электричество дороже добытого XMR. Пул по умолчанию — **C3Pool** (0% комиссии,
порог выплаты 0.001 XMR), потому что с него монетка капает раньше всего —
примерно через 3 дня при 5 kH/s. Переключить пул можно в любой момент.

## Безопасность и границы

- Майнинг стартует **только** после вашей галочки и нажатия «Начать майнинг».
- Нет скрытых процессов, автозапуска, установки служб и майнинга на чужих
  устройствах/посетителях сайта (криптоджекинга).
- Не используйте депозитный адрес биржи — нужен свой кошелёк с приватными ключами.
- Не запускайте на бесплатном VPS/PaaS: это нарушает ToS и приводит к бану.
- Налоги на добычу крипты уточняйте отдельно.

## Из чего собран exe

Исходник: [`windows/neonminer.c`](../windows/neonminer.c), веб-часть
[`windows/web/`](../windows/web/), генератор
[`windows/build_windows.py`](../windows/build_windows.py).
Кросс-компиляция: `zig cc -target x86_64-windows-gnu` (Zig 0.17, Clang/LLD).
Внутрь зашиты HTML/CSS/JS, каталог пулов и демо-данные — внешних файлов нет.

Пересобрать из исходников (Linux/macOS/WSL):

```bash
cd neon-miner
python3 windows/build_windows.py     # сам поставит ziglang с pypi.org
```

Сборка дополнительно собирает Linux-близнеца и прогоняет 10 внутренних проверок
(SHA-256, Keccak-256, валидный/битый адрес, config.json, калькулятор).
""", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
