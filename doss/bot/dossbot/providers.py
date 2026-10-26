"""Провайдеры доступа: как бот разговаривает с сервером Doss.

demo  — ничего не создаёт (для локального запуска и тестов);
local — вызывает CLI `doss` на той же машине;
ssh   — вызывает CLI `doss` на VPN-сервере по SSH (рекомендуется для прод);
link  — все получают одну заранее готовую ссылку (режим бесплатного PaaS).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import uuid
from abc import ABC, abstractmethod


class ProviderError(RuntimeError):
    pass


class Provider(ABC):
    """Единый интерфейс: бот не знает, где физически живёт сервер."""

    @abstractmethod
    async def add_user(self, name: str, *, expires: str | None, traffic_gb: int,
                       unlimited: bool = False, note: str = "") -> str:
        """Создаёт аккаунт, возвращает vless:// ссылку."""

    @abstractmethod
    async def link(self, name: str) -> str:
        """Возвращает актуальную vless:// ссылку."""

    @abstractmethod
    async def update_user(self, name: str, *, expires: str | None = None,
                          traffic_gb: int | None = None,
                          unlimited: bool | None = None,
                          enabled: bool | None = None) -> None: ...

    @abstractmethod
    async def remove_user(self, name: str) -> None: ...


class CommandProvider(Provider):
    """Общая реализация поверх CLI `doss` (локально или через ssh)."""

    def __init__(self, argv_prefix: list[str]) -> None:
        self._prefix = argv_prefix

    async def _run(self, *args: str) -> str:
        cmd = [*self._prefix, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as e:
            raise ProviderError(f"не могу запустить {' '.join(cmd[:2])}: {e}") from e
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise ProviderError(
                f"команда не удалась ({proc.returncode}): {' '.join(cmd)}\n{err.decode(errors='replace').strip()}"
            )
        return out.decode(errors="replace")

    @staticmethod
    def _last_link(output: str) -> str:
        for line in reversed(output.splitlines()):
            if line.startswith("vless://"):
                return line.strip()
        raise ProviderError("сервер не вернул vless:// ссылку")

    async def add_user(self, name: str, *, expires: str | None, traffic_gb: int,
                       unlimited: bool = False, note: str = "") -> str:
        args = ["user", "add", name, "--traffic", str(traffic_gb)]
        if expires:
            args += ["--expires", expires]
        if unlimited:
            args.append("--unlimited")
        if note:
            args += ["--note", note]
        return self._last_link(await self._run(*args))

    async def link(self, name: str) -> str:
        return self._last_link(await self._run("link", name))

    async def update_user(self, name: str, *, expires: str | None = None,
                          traffic_gb: int | None = None,
                          unlimited: bool | None = None,
                          enabled: bool | None = None) -> None:
        args = ["user", "set", name]
        if expires:
            args += ["--expires", expires]
        if traffic_gb is not None:
            args += ["--traffic", str(traffic_gb)]
        if unlimited:
            args.append("--unlimited")
        if enabled is True:
            args.append("--enable")
        elif enabled is False:
            args.append("--disable")
        await self._run(*args)

    async def remove_user(self, name: str) -> None:
        await self._run("user", "rm", name)


class LinkProvider(Provider):
    """Режим «одна ссылка на всех»: для PaaS, где нет CLI и per-user учёта.

    Ссылка берётся из DOSS_LINK. Продление/лимиты на стороне платформы
    не применяются, поэтому бот честно говорит об этом в /me.
    """

    def __init__(self, link: str) -> None:
        if not link.startswith("vless://"):
            raise ProviderError("DOSS_LINK должен начинаться с vless://")
        self._link = link.strip()

    async def add_user(self, name: str, *, expires: str | None, traffic_gb: int,
                       unlimited: bool = False, note: str = "") -> str:
        return self._link

    async def link(self, name: str) -> str:
        return self._link

    async def update_user(self, name: str, *, expires: str | None = None,
                          traffic_gb: int | None = None,
                          unlimited: bool | None = None,
                          enabled: bool | None = None) -> None:
        return None

    async def remove_user(self, name: str) -> None:
        return None


def build_provider(cfg) -> Provider:
    if cfg.provider == "local":
        return CommandProvider(shlex.split(cfg.doss_bin))
    if cfg.provider == "link":
        link = os.environ.get("DOSS_LINK", "")
        if not link:
            raise ProviderError("PROVIDER=link, но DOSS_LINK не задан")
        return LinkProvider(link)
    if cfg.provider == "ssh":
        if not cfg.ssh_host:
            raise ProviderError("PROVIDER=ssh, но SSH_HOST не задан")
        target = f"{cfg.ssh_user}@{cfg.ssh_host}"
        return CommandProvider([
            "ssh", "-i", cfg.ssh_key,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            target, cfg.doss_bin,
        ])
    return DemoProvider()


class DemoProvider(Provider):
    """Заглушка: генерирует правдоподобные ссылки, ничего не поднимает."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}

    def _link(self, name: str) -> str:
        u = self.users[name]
        return (
            f"vless://{u['uuid']}@203.0.113.10:443?encryption=none"
            "&flow=xtls-rprx-vision&security=reality&sni=www.microsoft.com"
            "&fp=chrome&pbk=DEMO_PUBKEY_000000000000000000000000000"
            f"&sid={u['sid']}&type=tcp#Doss%20{name}"
        )

    async def add_user(self, name: str, *, expires: str | None, traffic_gb: int,
                       unlimited: bool = False, note: str = "") -> str:
        if name in self.users:
            raise ProviderError(f"пользователь {name} уже есть")
        self.users[name] = {
            "uuid": str(uuid.uuid4()),
            "sid": uuid.uuid4().hex[:8],
            "expires": expires,
            "traffic_gb": traffic_gb,
            "unlimited": unlimited,
            "enabled": True,
        }
        return self._link(name)

    async def link(self, name: str) -> str:
        if name not in self.users:
            raise ProviderError(f"нет пользователя {name}")
        return self._link(name)

    async def update_user(self, name: str, *, expires: str | None = None,
                          traffic_gb: int | None = None,
                          unlimited: bool | None = None,
                          enabled: bool | None = None) -> None:
        if name not in self.users:
            raise ProviderError(f"нет пользователя {name}")
        u = self.users[name]
        if expires is not None:
            u["expires"] = expires
        if traffic_gb is not None:
            u["traffic_gb"] = traffic_gb
        if unlimited is not None:
            u["unlimited"] = unlimited
        if enabled is not None:
            u["enabled"] = enabled

    async def remove_user(self, name: str) -> None:
        self.users.pop(name, None)


def ssh_quote(value: str) -> str:
    """Для отладки ручных команд."""
    return shlex.quote(value)
