"""Помощь с расстановкой своих кораблей."""
from __future__ import annotations

import random
from functools import lru_cache

import numpy as np

from .board import comp_shape, ring_cells, validate_layout
from .rules import LETTERS_TOP, SHIP_NAMES, Rules, coord_name
from .solver import analyze, random_layout, sample_layouts

STRATEGIES = {
    "edges": (
        "У бортов",
        "Большие корабли — вдоль краёв поля, однопалубники разбросаны по центру. "
        "Люди чаще стреляют в середину, а у борта корабль «съедает» меньше места — "
        "остаётся много воды, где трудно найти одиночки. Хорошо против живых игроков.",
    ),
    "antibot": (
        "Против алгоритмов",
        "Корабли стоят в клетках, куда умные программы (и этот помощник) стреляют реже всего "
        "в начале партии. Подбираем из тысяч случайных расстановок самые «холодные» "
        "и берём одну из них наугад — чтобы не было шаблона.",
    ),
    "random": (
        "Случайно",
        "Полностью случайная расстановка — у соперника нет зацепок. "
        "Надёжный вариант, если не знаете, против кого играете.",
    ),
}


@lru_cache(maxsize=8)
def prior_heatmap(rules: Rules) -> np.ndarray:
    """Где корабли стоят чаще при случайной расстановке (= куда сильный бот бьёт первым делом)."""
    st = np.zeros((rules.size, rules.size), np.int8)
    a = analyze(st, rules, budget=0.8, seed=12345)
    return a.probs.copy()


def _valid_with(ships, cand, rules: Rules) -> bool:
    n = rules.size
    taken = set()
    for s in ships:
        taken |= set(s)
        taken |= ring_cells(n, s, rules.touch)
    return not any(c in taken for c in cand)


def _border_placements(L, n):
    out = []
    for c in range(n - L + 1):
        out.append([(0, c + i) for i in range(L)])
        out.append([(n - 1, c + i) for i in range(L)])
    for r in range(n - L + 1):
        out.append([(r + i, 0) for i in range(L)])
        out.append([(r + i, n - 1) for i in range(L)])
    return out


def _edges(rules: Rules, rng: random.Random):
    n = rules.size
    for _ in range(400):
        ships = []
        ok = True
        for L in rules.fleet:          # от длинных к коротким
            if L >= 2:
                cands = [p for p in _border_placements(L, n) if _valid_with(ships, p, rules)]
                if not cands:
                    ok = False
                    break
                ships.append(rng.choice(cands))
            else:
                inner = [[(r, c)] for r in range(1, n - 1) for c in range(1, n - 1)]
                cands = [p for p in inner if _valid_with(ships, p, rules)]
                if not cands:
                    cands = [[(r, c)] for r in range(n) for c in range(n)
                             if _valid_with(ships, [(r, c)], rules)]
                if not cands:
                    ok = False
                    break
                # чем дальше от остальных кораблей — тем лучше (берём лучший из нескольких)
                occupied = [x for s in ships for x in s]
                pick = rng.sample(cands, min(6, len(cands)))
                pick.sort(key=lambda p: -min((abs(p[0][0] - r) + abs(p[0][1] - c)
                                              for r, c in occupied), default=0))
                ships.append(pick[0])
        if ok and not validate_layout(ships, rules):
            return ships
    return random_layout(rules, rng)


def layout_heat(ships, heat: np.ndarray) -> float:
    return float(sum(heat[r, c] for s in ships for r, c in s))


def _antibot(rules: Rules, rng: random.Random, tries: int = 400):
    heat = prior_heatmap(rules)
    pool = [(layout_heat(s, heat), s) for s in sample_layouts(rules, tries, rng)]
    pool.sort(key=lambda t: t[0])
    best = pool[: max(1, len(pool) // 12)]
    return rng.choice(best)[1]


def generate(strategy: str, rules: Rules, rng: random.Random | None = None):
    rng = rng or random.Random()
    if strategy == "edges":
        ships = _edges(rules, rng)
    elif strategy == "antibot":
        ships = _antibot(rules, rng)
    else:
        ships = random_layout(rules, rng)
    return [sorted(s) for s in sorted(ships, key=lambda s: (-len(s), sorted(s)))]


@lru_cache(maxsize=8)
def _random_heat_stats(rules: Rules):
    heat = prior_heatmap(rules)
    vals = [layout_heat(s, heat) for s in sample_layouts(rules, 400, random.Random(777))]
    return float(np.mean(vals)), float(np.std(vals) + 1e-9)


def predictability(ships, rules: Rules) -> float:
    """0..100: насколько «очевидна» расстановка для сильного бота. 50 — как у случайной."""
    heat = prior_heatmap(rules)
    mean, std = _random_heat_stats(rules)
    z = (layout_heat(ships, heat) - mean) / std
    return float(np.clip(50.0 + 50.0 * z / 2.5, 0, 100))


def _ship_pos(s, letters, layout):
    s = sorted(s)
    if len(s) == 1:
        return coord_name(*s[0], letters, layout)
    return f"{coord_name(*s[0], letters, layout)}–{coord_name(*s[-1], letters, layout)}"


def describe(ships, letters: str = "ru", layout: str = LETTERS_TOP):
    """Человеческое описание: «четырёхпалубный: А1–Г1»."""
    lines = []
    for s in ships:
        name = SHIP_NAMES.get(len(s), f"{len(s)}-палубный")
        lines.append(f"{name}: {_ship_pos(s, letters, layout)}")
    return lines


def describe_short(ships, letters: str = "ru", layout: str = LETTERS_TOP) -> str:
    """Компактно: «4: К7–К10 · 3: К1–К3, А4–А6 · …»."""
    by_len = {}
    for s in ships:
        by_len.setdefault(len(s), []).append(_ship_pos(s, letters, layout))
    return "  ·  ".join(f"<b>{L}</b>: " + ", ".join(v) for L, v in sorted(by_len.items(), reverse=True))


def ships_from_cells(cells_mask: np.ndarray):
    """Клетки кораблей (маска) → список кораблей (компоненты по сторонам)."""
    from .board import components
    comps = components(cells_mask.astype(bool))
    return [sorted(c) for c in comps if comp_shape(sorted(c)) != "bad"], \
        [sorted(c) for c in comps if comp_shape(sorted(c)) == "bad"]
