"""Правила морского боя: размер поля, состав флота, правило касания, координаты."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace

# Состояния клетки поля соперника
UNKNOWN, MISS, HIT, SUNK = 0, 1, 2, 3
STATE_NAMES = {UNKNOWN: "не стреляли", MISS: "мимо", HIT: "ранил", SUNK: "убил"}

# Правило касания кораблей
TOUCH_NONE = "none"        # классика: нельзя касаться ни сторонами, ни углами
TOUCH_CORNERS = "corners"  # можно касаться углами (по диагонали)
TOUCH_ANY = "any"          # можно касаться как угодно, лишь бы не пересекались
TOUCH_LABELS = {
    TOUCH_NONE: "Нельзя касаться даже углами (классика)",
    TOUCH_CORNERS: "Можно касаться углами",
    TOUCH_ANY: "Можно касаться как угодно",
}

# Русские буквы столбцов: «Й» по традиции пропускают («Республика»-разметка тоже бывает,
# но А–К встречается на сайтах чаще всего).
RU_LETTERS = "АБВГДЕЖЗИКЛМНОПРСТУФ"
LAT_LETTERS = "ABCDEFGHIJKLMNOPQRST"

CLASSIC_FLEET = (4, 3, 3, 2, 2, 2, 1, 1, 1, 1)

SHIP_NAMES = {
    1: "однопалубный",
    2: "двухпалубный",
    3: "трёхпалубный",
    4: "четырёхпалубный",
    5: "пятипалубный",
    6: "шестипалубный",
}


@dataclass(frozen=True)
class Rules:
    size: int = 10
    fleet: tuple = CLASSIC_FLEET
    touch: str = TOUCH_NONE
    # Сайт отличает «убит» от «ранен» (почти все так делают).
    sunk_marked: bool = True

    def __post_init__(self):
        object.__setattr__(self, "fleet", tuple(sorted((int(x) for x in self.fleet), reverse=True)))
        if not 5 <= self.size <= 20:
            raise ValueError("размер поля должен быть от 5 до 20")
        if self.touch not in TOUCH_LABELS:
            raise ValueError(f"неизвестное правило касания: {self.touch}")
        if not self.fleet:
            raise ValueError("флот пуст")
        if min(self.fleet) < 1 or max(self.fleet) > self.size:
            raise ValueError("длина корабля должна быть от 1 до размера поля")
        if sum(self.fleet) > self.size * self.size // 2:
            raise ValueError("слишком много кораблей для такого поля")

    @property
    def total_cells(self) -> int:
        return sum(self.fleet)

    def fleet_counts(self) -> dict:
        return dict(sorted(Counter(self.fleet).items(), reverse=True))

    def with_(self, **kw) -> "Rules":
        return replace(self, **kw)


PRESETS = {
    "classic": ("Классика: 1×4, 2×3, 3×2, 4×1, без касаний", Rules()),
    "classic_corners": ("Классика, касание углами разрешено", Rules(touch=TOUCH_CORNERS)),
    "hasbro": ("Hasbro / международная: 5, 4, 3, 3, 2, касаться можно",
               Rules(fleet=(5, 4, 3, 3, 2), touch=TOUCH_ANY)),
}


def letters_for(style: str = "ru") -> str:
    return RU_LETTERS if style == "ru" else LAT_LETTERS


# Как подписаны клетки на сайте
LETTERS_TOP = "letters_top"      # классика: буквы над полем, цифры слева — «Д5» = столбец Д, строка 5
NUMBERS_TOP = "numbers_top"      # цифры над полем, буквы сбоку (Яндекс Игры) — «Д5» = строка Д, столбец 5
LAYOUT_LABELS = {
    LETTERS_TOP: "Буквы сверху, цифры слева",
    NUMBERS_TOP: "Цифры сверху, буквы сбоку (Яндекс Игры)",
}


def coord_name(r: int, c: int, style: str = "ru", layout: str = LETTERS_TOP) -> str:
    """(строка, столбец) → «Д5». Буква всегда первая — так клетки называют по-русски."""
    if layout == NUMBERS_TOP:
        return f"{letters_for(style)[r]}{c + 1}"
    return f"{letters_for(style)[c]}{r + 1}"


def parse_coord(text: str, size: int = 10) -> tuple:
    """«д5», «Д5», «E5», «5Д» → (4, 4)."""
    t = text.strip().upper().replace(" ", "")
    if not t:
        raise ValueError("пустая координата")
    letters = [ch for ch in t if not ch.isdigit()]
    digits = "".join(ch for ch in t if ch.isdigit())
    if len(letters) != 1 or not digits:
        raise ValueError(f"не понимаю координату: {text!r}")
    ch = letters[0].replace("Й", "И").replace("Ё", "Е")
    for alphabet in (RU_LETTERS, LAT_LETTERS):
        if ch in alphabet[:size]:
            c = alphabet.index(ch)
            break
    else:
        raise ValueError(f"нет такого столбца: {ch}")
    r = int(digits) - 1
    if not 0 <= r < size:
        raise ValueError(f"нет такой строки: {digits}")
    return r, c


def parse_fleet(text: str) -> tuple:
    """«4,3,3,2,2,2,1,1,1,1» или «1x4 2x3 3x2 4x1» (количество×длина) → кортеж длин."""
    t = text.lower().replace("х", "x").replace("*", "x").replace("×", "x")
    t = t.replace(";", ",").replace(" ", ",")
    out = []
    for part in filter(None, t.split(",")):
        if "x" in part:
            cnt, length = part.split("x", 1)
            out.extend([int(length)] * int(cnt))
        else:
            out.append(int(part))
    if not out:
        raise ValueError("флот пуст")
    return tuple(sorted(out, reverse=True))


def fleet_to_text(fleet) -> str:
    counts = Counter(fleet)
    return ", ".join(f"{counts[L]}×{L}" for L in sorted(counts, reverse=True))


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many
