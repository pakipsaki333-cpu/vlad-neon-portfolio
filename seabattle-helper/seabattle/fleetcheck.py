"""Тот ли состав флота выбран, что на сайте? Если нет — с какими готовыми правилами поле сходится.

Зачем это нужно. Решатель доверяет правилам из «Настроек». Если на сайте флот больше (например,
«Морской Бой классический» на Яндекс Играх и Пикабу: есть пятипалубный, всего 24 палубы вместо
20), помощник считает четыре попадания в ряд потопленным четырёхпалубным, не добивает пятую палубу
и ищет корабли, которых нет, — партия проигрывается.

Признаки, что флот не тот:
  * «железные» (board.prepare → fleet_issue): в ряд больше попаданий, чем палуб у самого длинного
    корабля; попаданий больше, чем палуб во всём флоте; раненый корабль длиннее всех оставшихся,
    хотя какой-то корабль раньше уже «потопили» только по длине;
  * «мягкий»: сайт сам обводит потопленные корабли точками (ореол), а вокруг корабля, который по
    правилам уже потоплен (только потому, что длиннее кораблей не осталось), точек нет — значит,
    он не потоплен, а длиннее.
Тогда ищем среди готовых правил (того же размера и с тем же правилом касания) те, с которыми поле
сходится. Здесь только логика, без Qt — её легко проверить тестами.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np

from .board import N8, prepare
from .rules import HIT, MISS, PRESETS, SUNK, Rules

# Столько секунд даём сайту обвести потопленный корабль точками (анимация, медленный компьютер),
# прежде чем решить, что корабль на самом деле не потоплен.
SOFT_WAIT = 4.0


@dataclass
class FleetCheck:
    hard: str = ""                                 # противоречие составу флота (объяснение) или ""
    suspects: list = field(default_factory=list)   # группы, «потопленные» только по длине


def inspect(states, rules: Rules) -> FleetCheck:
    """Одиночное «ранил» тоже бывает подозрительным: с классикой на сайте Яндекса первое попадание
    в пятипалубный под конец партии принимается за «последний однопалубный», и помощник объявляет
    победу, хотя пятипалубный ещё плавает."""
    p = prepare(states, rules)
    return FleetCheck(hard=p.fleet_issue, suspects=list(p.by_length))


def _near_ship(st, r, c) -> bool:
    n = st.shape[0]
    for dr, dc in N8:
        rr, cc = r + dr, c + dc
        if 0 <= rr < n and 0 <= cc < n and st[rr, cc] in (HIT, SUNK):
            return True
    return False


def auto_halo_marks(new_marks, states) -> bool:
    """Сайт сам ставит точки вокруг потопленного корабля? Признак — за один кадр появилось
    несколько промахов рядом с попаданиями: сам игрок за ход ставит одну отметку, а после
    промаха ход переходит к сопернику, так что два промаха подряд — это сайт."""
    st = np.asarray(states)
    near = sum(1 for r, c, v in (new_marks or ()) if v == MISS and _near_ship(st, r, c))
    return near >= 2


class HaloWatch:
    """Следит, ставит ли сайт сам точки вокруг потопленных кораблей. Точки могут появляться и по
    одной (анимация), поэтому кроме «несколько за кадр» считаем и «три за полторы секунды» —
    столько промахов подряд сам игрок сделать не может."""

    def __init__(self, window: float = 1.5, count: int = 3):
        self.window = window
        self.count = count
        self.recent = []
        self.seen = False

    def reset(self):
        self.recent = []
        self.seen = False

    def feed(self, now: float, new_marks, states) -> bool:
        if self.seen or not new_marks:
            return self.seen
        if auto_halo_marks(new_marks, states):
            self.seen = True
            return True
        st = np.asarray(states)
        self.recent += [now for r, c, v in new_marks if v == MISS and _near_ship(st, r, c)]
        self.recent = [t for t in self.recent if now - t <= self.window]
        self.seen = len(self.recent) >= self.count
        return self.seen


def consistent(states, rules: Rules, max_nodes: int = 60000):
    """Есть ли расстановка оставшихся кораблей, согласная с полем при этих правилах?
    → True / False / None (не успели проверить — считаем, что скорее есть)."""
    from .solver import Chain, Geometry, Problem, mask_from_bool   # solver сам импортирует board
    p = prepare(states, rules)
    if p.fatal:
        return False
    geo = Geometry.get(rules.size, p.touch)
    prob = Problem(geo, p.remaining, mask_from_bool(p.forbidden | (p.states == SUNK)),
                   mask_from_bool(p.required), mask_from_bool(p.unknown), p.sunk_marked)
    return Chain(prob, random.Random(0)).initialize(max_nodes=max_nodes, restarts=3)


def _key(group):
    return frozenset(tuple(x) for x in group)


def suggest(states, rules: Rules, suspects=()):
    """Готовые правила того же размера и с тем же правилом касания, при которых поле сходится,
    а «подозрительные» корабли не обязаны быть потопленными. → (ключ, Rules) или None."""
    want = {_key(g) for g in suspects}
    for key, (_, r) in PRESETS.items():
        if r.size != rules.size or r.touch != rules.touch or r.fleet == tuple(rules.fleet):
            continue
        r = r.with_(sunk_marked=rules.sunk_marked)
        p = prepare(states, r)
        if p.fatal or p.fleet_issue:
            continue
        if want & {_key(g) for g in p.by_length}:
            continue                  # и с этим флотом корабль пришлось бы считать потопленным
        if consistent(states, r) is False:
            continue
        return key, r
    return None
