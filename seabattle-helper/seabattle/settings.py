"""Настройки: JSON в %APPDATA%\\SeaBattleHelper (на Linux/macOS — ~/.config/SeaBattleHelper)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field

from .rules import CLASSIC_FLEET, TOUCH_NONE, Rules

APP_DIR_NAME = "SeaBattleHelper"


def config_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


@dataclass
class Settings:
    # правила
    size: int = 10
    fleet: list = field(default_factory=lambda: list(CLASSIC_FLEET))
    touch: str = TOUCH_NONE
    sunk_marked: bool = True
    letters: str = "ru"
    labels: str = "auto"                 # auto | letters_top | numbers_top — как подписаны клетки на сайте
    # кто выбрал правила: default — никто (стоят исходные); user — сам пользователь в «Настройках»;
    # auto_site — помощник узнал игру по виду поля; auto_board — поле не сходилось с флотом
    rules_source: str = "default"
    # распознавание
    screen_name: str = ""
    enemy_geom: dict | None = None
    my_geom: dict | None = None
    sensitivity: float = 1.0
    interval_ms: int = 350
    detect_sunk_look: bool = True
    examples: list = field(default_factory=list)
    # подсказки
    budget: float = 0.7
    overlay: bool = True
    overlay_heat: bool = False
    # автоигра
    auto_delay: float = 0.8              # пауза перед каждым выстрелом, с
    # окно
    opacity: float = 0.96
    on_top: bool = True
    hide_from_capture: bool = True
    window: list | None = None
    mini: bool = False
    first_run: bool = True
    placement_strategy: str = "edges"

    def rules(self) -> Rules:
        try:
            return Rules(size=self.size, fleet=tuple(self.fleet), touch=self.touch,
                         sunk_marked=self.sunk_marked)
        except ValueError:
            return Rules()

    # ------------------------------------------------------------------ файл
    @staticmethod
    def path() -> str:
        return os.path.join(config_dir(), "settings.json")

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            with open(cls.path(), encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        return s

    def save(self) -> None:
        tmp = self.path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path())
        except OSError:
            pass
