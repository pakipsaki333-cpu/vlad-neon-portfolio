"""Генератор конфигурации XMRig и сопутствующих скриптов.

Мы НЕ изобретаем свой майнер: XMRig — открытый стандарт для RandomX.
Клиент лишь подставляет твой кошелёк, выбранный пул и безопасные дефолты
(CPU ≤75%, huge pages, TLS, failover-пул, локальный read-only API).
"""

from __future__ import annotations

import json
import platform
import re
from typing import Any, Dict, Optional

from .pools import POOLS, DEFAULT_POOL, DEFAULT_PORT, get

SAFE_WORKER_RE = re.compile(r"[^A-Za-z0-9._-]")


def clean_worker(value: str) -> str:
    value = SAFE_WORKER_RE.sub("-", (value or "").strip())[:32]
    return value or "rig-01"


def resolve_host(pool_id: str, port: int) -> str:
    """C3Pool держит TLS-порт 33333 на отдельном хосте (см. /pool/ports)."""
    if pool_id == "c3pool" and port == 33333:
        return "mine.c3pool.com"
    return get(pool_id).host


def resolve_tls(pool_id: str, port: int) -> bool:
    for item in get(pool_id).ports:
        if item.port == port:
            return item.tls
    return False


def pool_settings(pool_id: str, port: int) -> dict:
    pool = get(pool_id)
    host = resolve_host(pool_id, port)
    tls = resolve_tls(pool_id, port)
    entry: Dict[str, Any] = {
        "algo": None,
        "coin": None if pool_id == "moneroocean" else "monero",
        "url": f"{host}:{port}",
        "user": "WALLET_PLACEHOLDER",
        "pass": "rig-01",
        "rig-id": "rig-01",
        "nicehash": False,
        "keepalive": True,
        "enabled": True,
        "tls": tls,
        "tls-fingerprint": None,
        "daemon": False,
    }
    return entry


def build_config(
    wallet: str,
    pool_id: str = DEFAULT_POOL,
    port: Optional[int] = None,
    worker: str = "rig-01",
    threads_pct: int = 75,
    donate_level: int = 1,
    fixed_difficulty: Optional[int] = None,
    http_api_port: int = 8080,
) -> dict:
    pool = get(pool_id)
    port = port or DEFAULT_PORT[pool_id]
    worker = clean_worker(worker)
    threads_pct = max(10, min(100, int(threads_pct)))
    donate_level = max(0, min(99, int(donate_level)))

    user = wallet
    if fixed_difficulty and fixed_difficulty > 0:
        user = f"{wallet}+{int(fixed_difficulty)}"

    primary = pool_settings(pool_id, port)
    primary["user"] = user
    primary["pass"] = worker
    primary["rig-id"] = worker

    pools = [primary]
    # Резервный пул: тот же кошелёк, другой хост/домен. XMRig переключится сам.
    if pool.failover_host and pool.failover_port:
        failover = pool_settings(pool_id, pool.failover_port)
        failover["url"] = f"{pool.failover_host}:{pool.failover_port}"
        failover["tls"] = pool.failover_tls
        failover["user"] = user
        failover["pass"] = worker
        failover["rig-id"] = worker
        pools.append(failover)
    elif pool_id == "c3pool":
        backup = pool_settings("c3pool", 80)
        backup["url"] = "auto.c3pool.org:80"
        backup["user"] = user
        backup["pass"] = worker
        backup["rig-id"] = worker
        pools.append(backup)
    else:
        backup = pool_settings(pool_id, pool.ports[-1].port)
        backup["user"] = user
        backup["pass"] = worker
        backup["rig-id"] = worker
        pools.append(backup)

    return {
        "autosave": True,
        "background": False,
        "colors": True,
        "donate-level": donate_level,
        "log-file": None,
        "print-time": 60,
        "health-print-time": 60,
        "retries": 5,
        "retry-pause": 5,
        "syslog": False,
        "watch": True,
        "api": {"id": None, "worker-id": None},
        "http": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": http_api_port,
            "access-token": None,
            "restricted": True,
        },
        "randomx": {"init": -1, "numa": True},
        "cpu": {
            "enabled": True,
            "huge-pages": True,
            "hw-aes": None,
            "priority": 2,
            "memory-pool": False,
            "max-threads-hint": threads_pct,
            "asm": True,
            "yield": True,
        },
        "opencl": False,
        "cuda": False,
        "pools": pools,
    }


def config_json(config: dict) -> str:
    return json.dumps(config, indent=4, ensure_ascii=False) + "\n"


def command(config: dict, os_name: str = "linux") -> str:
    """Команда запуска без config.json — всё через CLI."""
    pool = config["pools"][0]
    wallet = pool["user"].split("+", 1)[0]
    tls = " --tls" if pool.get("tls") else ""
    binary = "xmrig.exe" if os_name == "windows" else "./xmrig"
    # У MoneroOcean coin=null, чтобы пул сам мог переключать алгоритмы.
    coin = " --coin=monero" if pool.get("coin") == "monero" else ""
    # PowerShell тоже понимает такой вызов; кавычки вокруг пароля на случай спецсимволов.
    return (
        f'{binary} -o {pool["url"]} -u {wallet} -p "{pool["pass"]}"'
        f'{coin} --cpu-max-threads-hint={config["cpu"]["max-threads-hint"]} '
        f'--donate-level={config["donate-level"]}{tls}'
    )


def systemd_unit(config_path: str, binary_path: str, user: str = "USER") -> str:
    return f"""# НЕ автозапускается сам: установи юнит вручную, когда решишь.
#   sudo cp neon-miner.service /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now neon-miner
[Unit]
Description=NeonMiner (XMRig) — майнинг на ЭТОЙ машине
After=network-online.target

[Service]
Type=simple
User={user}
ExecStart={binary_path} --config={config_path}
Restart=on-failure
RestartSec=30
Nice=10
CPUWeight=20

[Install]
WantedBy=multi-user.target
"""


def windows_bat(config: dict) -> str:
    pool = config["pools"][0]
    return (
        "@echo off\r\n"
        "REM NeonMiner — запуск вручную. Закрой окно, чтобы остановить майнер.\r\n"
        f"xmrig.exe --config=config.json\r\n"
        f"REM Пул: {pool['url']}  |  TLS: {str(pool.get('tls', False)).lower()}\r\n"
        "pause\r\n"
    )


def bundle(
    wallet: str,
    pool_id: str = DEFAULT_POOL,
    port: Optional[int] = None,
    worker: str = "rig-01",
    threads_pct: int = 75,
    donate_level: int = 1,
    fixed_difficulty: Optional[int] = None,
) -> dict:
    config = build_config(wallet, pool_id, port, worker, threads_pct,
                          donate_level, fixed_difficulty)
    system = platform.system().lower()
    return {
        "pool": pool_id,
        "pool_name": get(pool_id).name,
        "host": config["pools"][0]["url"],
        "tls": bool(config["pools"][0]["tls"]),
        "config": config,
        "config_json": config_json(config),
        "cmd_linux": command(config, "linux"),
        "cmd_windows": command(config, "windows"),
        "systemd_unit": systemd_unit("/opt/neon-miner/config.json", "/opt/neon-miner/xmrig"),
        "windows_bat": windows_bat(config),
        "detected_os": "windows" if system.startswith("win") else system,
        "notes": get(pool_id).notes,
    }
