"""Загрузка ОФИЦИАЛЬНОГО XMRig с GitHub Releases + проверка SHA256.

Правила:
  1. Берём только репозиторий xmrig/xmrig, только последний релиз.
  2. Скачиваем SHA256SUMS из того же релиза и проверяем архив до распаковки.
  3. Если сумму получить/проверить нельзя — НИЧЕГО не запускаем.
  4. Распаковка безопасная: без `..` и абсолютных путей в архиве.

Никаких сторонних зеркал, «статических билдов» из телеграм-каналов и т.п.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import ssl
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Optional, Tuple

API_RELEASES = "https://api.github.com/repos/xmrig/xmrig/releases/latest"
USER_AGENT = "NeonMiner/1.0 (+https://github.com/xmrig/xmrig)"
TIMEOUT = 30

# Суффиксы соответствуют реальным ассетам релиза v6.26.0 (проверено через GitHub API).
ASSET_SUFFIX = {
    ("linux", "x86_64"): "linux-static-x64.tar.gz",
    ("linux", "amd64"): "linux-static-x64.tar.gz",
    ("darwin", "arm64"): "macos-arm64.tar.gz",
    ("darwin", "x86_64"): "macos-x64.tar.gz",
    ("windows", "amd64"): "windows-x64.zip",
    ("windows", "arm64"): "windows-arm64.zip",
}


class DownloadError(RuntimeError):
    pass


def _get(url: str, *, accept: str = "application/json") -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT,
                                   context=ssl.create_default_context()) as response:
            return response.read(20_000_000)
    except Exception as exc:  # noqa: BLE001 — наружу отдаём читаемое сообщение
        raise DownloadError(f"Не удалось скачать {url}: {exc}") from exc


def latest_release() -> dict:
    data = json.loads(_get(API_RELEASES).decode("utf-8"))
    if "tag_name" not in data or "assets" not in data:
        raise DownloadError("Неожиданный ответ GitHub API.")
    return data


def platform_suffix() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    key = (system, machine)
    if key in ASSET_SUFFIX:
        return ASSET_SUFFIX[key]
    if system == "linux" and machine in ("aarch64", "armv8l", "arm64"):
        raise DownloadError(
            "У официального XMRig нет готового бинарника под Linux ARM. "
            "Собери из исходников (github.com/xmrig/xmrig) или используй x86_64-машину."
        )
    raise DownloadError(f"Платформа {system}/{machine} не поддерживается готовыми сборками.")


def pick_assets(release: dict, suffix: str) -> Tuple[dict, dict]:
    tag = release["tag_name"]
    version = tag.lstrip("v")
    wanted = f"xmrig-{version}-{suffix}"
    by_name = {asset["name"]: asset for asset in release["assets"]}
    if wanted not in by_name:
        raise DownloadError(
            f"В релизе {tag} нет ассета {wanted}. Проверь "
            "https://github.com/xmrig/xmrig/releases/latest вручную."
        )
    if "SHA256SUMS" not in by_name:
        raise DownloadError("В релизе нет SHA256SUMS — проверка невозможна, запуск отменён.")
    return by_name[wanted], by_name["SHA256SUMS"]


def parse_sums(text: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            result[parts[-1].lstrip("*")] = parts[0].lower()
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_file(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
        timeout=TIMEOUT, context=ssl.create_default_context(),
    ) as response, dest.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    return dest


def _safe_extract(archive: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()

    def safe(name: str) -> bool:
        path = (target / name).resolve()
        return path == root or root in path.parents

    if archive.suffix == ".zip" or archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if not safe(member):
                    raise DownloadError(f"Опасный путь в архиве: {member}")
            zf.extractall(target)
    else:
        with tarfile.open(archive, "r:*") as tf:
            for member in tf.getmembers():
                if not safe(member.name):
                    raise DownloadError(f"Опасный путь в архиве: {member.name}")
                if member.issym() or member.islnk() or member.isdev():
                    raise DownloadError(f"Ссылка/устройство в архиве: {member.name}")
            tf.extractall(target)

    binary_name = "xmrig.exe" if os.name == "nt" else "xmrig"
    for candidate in sorted(target.rglob(binary_name)):
        if candidate.is_file():
            candidate.chmod(candidate.stat().st_mode | 0o111)
            return candidate
    raise DownloadError("Бинарник xmrig не найден в распакованном архиве.")


def prepare_binary(dest: Path, *, release: Optional[dict] = None) -> dict:
    """Скачивает, проверяет и распаковывает XMRig. Возвращает отчёт."""
    release = release or latest_release()
    suffix = platform_suffix()
    asset, sums_asset = pick_assets(release, suffix)

    sums_text = _get(sums_asset["browser_download_url"], accept="text/plain").decode("utf-8")
    sums = parse_sums(sums_text)
    expected = sums.get(asset["name"])
    if not expected:
        raise DownloadError(f"В SHA256SUMS нет строки для {asset['name']}. Запуск отменён.")

    archive_path = dest / asset["name"]
    if not archive_path.exists() or sha256_file(archive_path) != expected:
        print(f"  ↓ {asset['browser_download_url']}", file=sys.stderr)
        _download_file(asset["browser_download_url"], archive_path)

    actual = sha256_file(archive_path)
    if actual != expected:
        archive_path.unlink(missing_ok=True)
        raise DownloadError(
            f"SHA256 НЕ совпал!\n  ожидалось: {expected}\n  получено:  {actual}\n"
            "Архив удалён. Ничего не запущено."
        )

    binary = _safe_extract(archive_path, dest / "bin")
    return {
        "version": release["tag_name"],
        "asset": asset["name"],
        "sha256": actual,
        "binary": str(binary),
        "release_url": release.get("html_url", "https://github.com/xmrig/xmrig/releases/latest"),
        "verified": True,
    }
