"""CLI NeonMiner.

    python3 -m neon_miner                       # веб-клиент на 127.0.0.1:8000
    python3 -m neon_miner serve --host 0.0.0.0 # открыть в локальной сети/превью
    python3 -m neon_miner pools                 # каталог пулов
    python3 -m neon_miner check <XMR-адрес>     # проверка контрольной суммы
    python3 -m neon_miner config --wallet ... --out config.json
    python3 -m neon_miner start --wallet ... --download --yes
    python3 -m neon_miner profit --hashrate 5000
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .address import validate_address
from .calc import estimate
from .configgen import bundle, config_json
from .pools import DEFAULT_POOL, POOLS, catalog


def _confirm(message: str) -> bool:
    try:
        return input(f"{message} [y/N]: ").strip().lower() in ("y", "yes", "д", "да")
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_pools(args: argparse.Namespace) -> int:
    for pool in catalog():
        mark = "★" if pool["recommended"] else " "
        ports = ", ".join(f"{p['port']}{' TLS' if p['tls'] else ''}" for p in pool["ports"])
        print(f"{mark} {pool['id']:<13} {pool['name']:<12} fee {pool['fee_pct']:.2f}%  "
              f"min {pool['min_payout_xmr']:.4f} XMR  [{pool['region']}]")
        print(f"    {pool['host']} — порты: {ports}")
        print(f"    {pool['tagline']}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    info = validate_address(args.address)
    print(json.dumps(info.as_dict(), ensure_ascii=False, indent=2))
    return 0 if info.valid else 1


def cmd_config(args: argparse.Namespace) -> int:
    info = validate_address(args.wallet)
    if not info.valid:
        print(f"ОШИБКА: {info.reason}", file=sys.stderr)
        return 2
    data = bundle(info.address, args.pool, args.port, args.worker,
                  args.threads, args.donate, args.difficulty)
    text = data["config_json"]
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"✓ config.json записан: {path.resolve()}")
    else:
        print(text)
    print(f"Кошелёк : {info.address}")
    print(f"Пул     : {data['pool_name']} → {data['host']} (TLS: {data['tls']})")
    print(f"Запуск  : {data['cmd_windows'] if data['detected_os'] == 'windows' else data['cmd_linux']}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    info = validate_address(args.wallet)
    if not info.valid:
        print(f"ОШИБКА: {info.reason}", file=sys.stderr)
        return 2

    print("=" * 68)
    print(" NeonMiner — запуск майнера")
    print("=" * 68)
    print(" • Майнинг идёт ТОЛЬКО на этой машине, в открытом процессе.")
    print(" • Никаких скрытых автозапусков, служб и чужого железа.")
    print(" • XMRig часто ругается у антивирусов — это ложное срабатывание,")
    print("   но качаем мы только официальный релиз и проверяем SHA256.")
    print(" • На бесплатных VPS/PaaS майнить нельзя по ToS — аккаунт заблокируют.")
    print(" • Считай электричество: калькулятор в веб-клиенте покажет минус.")
    print("=" * 68)
    if not args.yes and not _confirm("Это твоя машина и ты понимаешь условия?"):
        print("Отменено.")
        return 1

    workdir = Path(args.dir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    data = bundle(info.address, args.pool, args.port, args.worker,
                  args.threads, args.donate, args.difficulty)
    config_path = workdir / "config.json"
    config_path.write_text(data["config_json"], encoding="utf-8")
    print(f"✓ Конфиг: {config_path}")

    if args.download:
        from .downloader import DownloadError, prepare_binary
        try:
            report = prepare_binary(workdir)
        except DownloadError as exc:
            print(f"ОШИБКА: {exc}", file=sys.stderr)
            print("Скачай вручную: https://github.com/xmrig/xmrig/releases/latest",
                  file=sys.stderr)
            return 3
        print(f"✓ XMRig {report['version']} ({report['asset']})")
        print(f"✓ SHA256 совпал: {report['sha256']}")
        binary = report["binary"]
    else:
        binary = args.binary
        if not binary:
            print("Укажи --binary <путь к xmrig> или добавь --download.", file=sys.stderr)
            return 2

    command = [str(binary), f"--config={config_path}"]
    print("→ " + " ".join(command))
    print("  Остановить: Ctrl+C\n")
    if args.dry_run:
        print("(dry-run: процесс не запускался)")
        return 0
    try:
        return subprocess.call(command, cwd=str(workdir))
    except FileNotFoundError:
        print(f"Не найден бинарник: {binary}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 0


def cmd_profit(args: argparse.Namespace) -> int:
    result = estimate(
        args.hashrate,
        fee_pct=args.fee,
        threshold_xmr=args.threshold,
        price_eur=args.price,
        power_watts=args.watts,
        eur_per_kwh=args.kwh,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print()
    print(f"Итого: {result['xmr_per_day']:.8f} XMR/сутки ≈ {result['eur_per_day']:.3f} €/сутки")
    print(f"Электричество: −{result['power_cost_eur_per_day']:.3f} €/сутки")
    print(f"Чистыми: {result['net_eur_per_day']:+.3f} €/сутки "
          f"({'плюс' if result['profitable'] else 'МИНУС'})")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import make_server
    server = make_server(args.host, args.port)
    print("=" * 68)
    print(f" NeonMiner {__version__} — локальный клиент майнинга XMR")
    print("=" * 68)
    print(f"  Открой в браузере: http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}")
    print("  Только своё железо. Скрытого майнинга нет.")
    print("  Остановить: Ctrl+C")
    print("=" * 68, flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neon-miner", description="Локальный клиент для майнинга Monero (XMR) на своём железе")
    parser.add_argument("--version", action="version", version=f"NeonMiner {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="веб-клиент (по умолчанию)")
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("pools", help="проверенный каталог пулов")
    p.set_defaults(func=cmd_pools)

    p = sub.add_parser("check", help="проверить XMR-адрес")
    p.add_argument("address")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("config", help="сгенерировать config.json для XMRig")
    p.add_argument("--wallet", required=True)
    p.add_argument("--pool", default=DEFAULT_POOL, choices=list(POOLS))
    p.add_argument("--port", type=int)
    p.add_argument("--worker", default="rig-01")
    p.add_argument("--threads", type=int, default=75, help="%% потоков CPU (по умолчанию 75)")
    p.add_argument("--donate", type=int, default=1)
    p.add_argument("--difficulty", type=int, default=None)
    p.add_argument("--out")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("start", help="скачать/проверить XMRig и запустить его")
    p.add_argument("--wallet", required=True)
    p.add_argument("--pool", default=DEFAULT_POOL, choices=list(POOLS))
    p.add_argument("--port", type=int)
    p.add_argument("--worker", default="rig-01")
    p.add_argument("--threads", type=int, default=75)
    p.add_argument("--donate", type=int, default=1)
    p.add_argument("--difficulty", type=int, default=None)
    p.add_argument("--dir", default="./neon-miner-workdir")
    p.add_argument("--download", action="store_true", help="скачать официальный XMRig с GitHub + проверить SHA256")
    p.add_argument("--binary", help="путь к уже скачанному xmrig")
    p.add_argument("--yes", action="store_true", help="пропустить подтверждение")
    p.add_argument("--dry-run", action="store_true", help="всё подготовить, но не запускать")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("profit", help="честный калькулятор доходности")
    p.add_argument("--hashrate", type=float, required=True, help="H/s (например 5000)")
    p.add_argument("--fee", type=float, default=0.0, help="комиссия пула в %%")
    p.add_argument("--threshold", type=float, default=0.001, help="порог выплаты в XMR")
    p.add_argument("--price", type=float, default=None, help="цена XMR в EUR")
    p.add_argument("--watts", type=float, default=65.0)
    p.add_argument("--kwh", type=float, default=None, help="цена кВт·ч в EUR")
    p.set_defaults(func=cmd_profit)

    return parser


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Без подкоманды — сразу веб-клиент (HOST/PORT из окружения).
        return cmd_serve(argparse.Namespace(
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000"))))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
