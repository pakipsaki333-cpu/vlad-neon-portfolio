"""Логика поля соперника: группы попаданий, вывод «убит», заведомо пустые клетки.

Здесь нет вероятностей — только то, что следует из правил железно:
  * вокруг убитого корабля кораблей нет (ореол зависит от правила касания);
  * при классике по диагонали от любого попадания кораблей нет;
  * группа попаданий, которую некуда продлить, — это убитый корабль;
  * группа длиной с самый большой оставшийся корабль — тоже убита.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .rules import (HIT, MISS, SUNK, TOUCH_ANY, TOUCH_CORNERS, TOUCH_NONE, UNKNOWN,
                    Rules)

N4 = ((-1, 0), (1, 0), (0, -1), (0, 1))
DIAG = ((-1, -1), (-1, 1), (1, -1), (1, 1))
N8 = N4 + DIAG


def neighbor_offsets(touch: str):
    """Какие соседи корабля запрещены для других кораблей."""
    if touch == TOUCH_NONE:
        return N8
    if touch == TOUCH_CORNERS:
        return N4
    return ()


def components(mask: np.ndarray) -> list:
    """Связные (по сторонам) группы клеток. Возвращает списки (r, c), отсортированные."""
    n = mask.shape[0]
    seen = np.zeros_like(mask, dtype=bool)
    comps = []
    for r in range(n):
        for c in range(n):
            if not mask[r, c] or seen[r, c]:
                continue
            stack = [(r, c)]
            seen[r, c] = True
            comp = []
            while stack:
                y, x = stack.pop()
                comp.append((y, x))
                for dy, dx in N4:
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < n and 0 <= xx < n and mask[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True
                        stack.append((yy, xx))
            comps.append(sorted(comp))
    return comps


def comp_shape(comp) -> str:
    """'single' | 'h' | 'v' | 'bad' (не прямая линия)."""
    if len(comp) == 1:
        return "single"
    rows = {r for r, _ in comp}
    cols = {c for _, c in comp}
    if len(rows) == 1 and max(cols) - min(cols) + 1 == len(comp):
        return "h"
    if len(cols) == 1 and max(rows) - min(rows) + 1 == len(comp):
        return "v"
    return "bad"


def ring_cells(n: int, cells, touch: str) -> set:
    """Клетки вокруг корабля, где не может быть других кораблей (без самих клеток)."""
    cs = set(cells)
    out = set()
    for r, c in cs:
        for dr, dc in neighbor_offsets(touch):
            rr, cc = r + dr, c + dc
            if 0 <= rr < n and 0 <= cc < n and (rr, cc) not in cs:
                out.add((rr, cc))
    return out


def extension_cells(n: int, comp, shape: str) -> list:
    """Куда группа попаданий может «продлиться», если это ещё не весь корабль."""
    if shape == "single":
        (r, c), = comp
        cand = [(r + dr, c + dc) for dr, dc in N4]
    elif shape == "h":
        r = comp[0][0]
        cmin = min(c for _, c in comp)
        cmax = max(c for _, c in comp)
        cand = [(r, cmin - 1), (r, cmax + 1)]
    elif shape == "v":
        c = comp[0][1]
        rmin = min(r for r, _ in comp)
        rmax = max(r for r, _ in comp)
        cand = [(rmin - 1, c), (rmax + 1, c)]
    else:
        return []
    return [(r, c) for r, c in cand if 0 <= r < n and 0 <= c < n]


def split_straight(cells, available: Counter):
    """Разбить группу убитых клеток на прямые корабли из доступных длин (для «можно касаться»)."""
    cells = sorted(cells)
    if not cells:
        return []
    r, c = cells[0]  # самая верхняя-левая клетка начинает какой-то корабль
    rest_set = set(cells)
    for L in sorted((L for L, k in available.items() if k > 0), reverse=True):
        for dr, dc in ((0, 1), (1, 0)) if L > 1 else ((0, 1),):
            seg = [(r + dr * i, c + dc * i) for i in range(L)]
            if all(p in rest_set for p in seg):
                available[L] -= 1
                sub = split_straight([p for p in cells if p not in seg], available)
                if sub is not None:
                    return [seg] + sub
                available[L] += 1
    return None


@dataclass
class Prepared:
    rules: Rules
    touch: str
    sunk_marked: bool
    states: np.ndarray                 # состояния после вывода «убит»
    sunk_ships: list                   # убитые корабли (списки клеток)
    remaining: list                    # длины ещё не потопленных кораблей
    forbidden: np.ndarray              # bool: тут точно нет корабля
    required: np.ndarray               # bool: тут ранен корабль (не убит)
    unknown: np.ndarray                # bool: не стреляли и не исключено
    hit_groups: list = field(default_factory=list)
    inferred: list = field(default_factory=list)   # группы, признанные убитыми логически
    issues: list = field(default_factory=list)
    fatal: bool = False                # противоречие, при котором решать нельзя


def _forbidden(n, st, sunk_ships, hit_groups, touch):
    forb = st == MISS
    for ship in sunk_ships:
        for r, c in ring_cells(n, ship, touch):
            forb[r, c] = True
    if touch == TOUCH_NONE:
        # корабли прямые и не касаются углами → по диагонали от попадания пусто
        for r, c in zip(*np.nonzero(st == HIT)):
            for dr, dc in DIAG:
                rr, cc = r + dr, c + dc
                if 0 <= rr < n and 0 <= cc < n:
                    forb[rr, cc] = True
    if touch in (TOUCH_NONE, TOUCH_CORNERS):
        # у «линии» попаданий борта известны: сбоку от неё кораблей нет
        for comp in hit_groups:
            shape = comp_shape(comp)
            if shape not in ("h", "v"):
                continue
            side = ((-1, 0), (1, 0)) if shape == "h" else ((0, -1), (0, 1))
            for r, c in comp:
                for dr, dc in side:
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < n and 0 <= cc < n:
                        forb[rr, cc] = True
    forb &= (st != HIT) & (st != SUNK)
    return forb


def prepare(states, rules: Rules, *, sunk_marked=None, touch=None, trust_sunk=True) -> Prepared:
    n = rules.size
    st = np.array(states, dtype=np.int8).reshape(n, n).copy()
    touch = touch or rules.touch
    sunk_marked = rules.sunk_marked if sunk_marked is None else sunk_marked
    issues = []
    fatal = False
    if not trust_sunk:
        st[st == SUNK] = HIT
    fleet = Counter(rules.fleet)
    inferred = []

    if touch != TOUCH_ANY:
        # Клетки «ранил» рядом с «убил» — это один и тот же убитый корабль.
        for comp in components((st == HIT) | (st == SUNK)):
            if any(st[r, c] == SUNK for r, c in comp):
                for r, c in comp:
                    st[r, c] = SUNK
        while True:
            comps = components((st == HIT) | (st == SUNK))
            sunk_ships = [cp for cp in comps if st[cp[0]] == SUNK]
            hit_groups = [cp for cp in comps if st[cp[0]] == HIT]
            used = Counter(len(cp) for cp in sunk_ships)
            remaining_c = Counter({L: fleet[L] - used.get(L, 0) for L in fleet})
            forb = _forbidden(n, st, sunk_ships, hit_groups, touch)
            if not sunk_marked:
                break
            rem_lengths = [L for L, k in remaining_c.items() if k > 0]
            changed = False
            for comp in hit_groups:
                shape = comp_shape(comp)
                if shape == "bad":
                    continue
                L = len(comp)
                if remaining_c.get(L, 0) <= 0:
                    continue
                can_extend = any(st[r, c] == UNKNOWN and not forb[r, c]
                                 for r, c in extension_cells(n, comp, shape))
                longer_exists = any(l > L for l in rem_lengths)
                if not can_extend or not longer_exists:
                    for r, c in comp:
                        st[r, c] = SUNK
                    inferred.append(comp)
                    changed = True
                    break  # пересчитать остаток флота и ореолы
            if not changed:
                break
        for cp in sunk_ships + hit_groups:
            if comp_shape(cp) == "bad":
                issues.append("Корабль не прямой — проверь отметки на поле или правило касания.")
                fatal = True
                break
        for L, k in used.items():
            if fleet.get(L, 0) < k:
                issues.append(f"Убитых {L}-палубных больше, чем есть во флоте.")
                fatal = True
    else:
        hit_groups = components(st == HIT)
        sunk_ships = []
        avail = Counter(fleet)
        for comp in components(st == SUNK):
            parts = split_straight(comp, avail)
            if parts is None:
                issues.append("Не получается разложить убитые клетки на корабли из флота.")
                fatal = True
                for r, c in comp:
                    st[r, c] = HIT
            else:
                sunk_ships.extend(parts)
        hit_groups = components(st == HIT)
        used = Counter(len(s) for s in sunk_ships)
        remaining_c = Counter({L: fleet[L] - used.get(L, 0) for L in fleet})
        forb = _forbidden(n, st, sunk_ships, hit_groups, touch)

    remaining = sorted((L for L, k in remaining_c.items() for _ in range(max(k, 0))), reverse=True)
    required = st == HIT
    unknown = (st == UNKNOWN) & ~forb
    ship_cells_seen = int(((st == HIT) | (st == SUNK)).sum())
    if ship_cells_seen > rules.total_cells:
        issues.append("Попаданий больше, чем палуб во всём флоте.")
        fatal = True
    if required.any() and not remaining:
        issues.append("Все корабли уже убиты, но на поле есть «ранил».")
        fatal = True
    return Prepared(rules=rules, touch=touch, sunk_marked=sunk_marked, states=st,
                    sunk_ships=sunk_ships, remaining=remaining, forbidden=forb,
                    required=required, unknown=unknown, hit_groups=hit_groups,
                    inferred=inferred, issues=issues, fatal=fatal)


def validate_layout(ships, rules: Rules) -> list:
    """Проверить собственную расстановку: список проблем (пустой — всё ок)."""
    n = rules.size
    problems = []
    lengths = sorted((len(s) for s in ships), reverse=True)
    if tuple(lengths) != rules.fleet:
        problems.append("Состав флота не совпадает с правилами.")
    occ = {}
    for i, ship in enumerate(ships):
        if comp_shape(sorted(ship)) == "bad":
            problems.append(f"Корабль №{i + 1} не прямой.")
        for r, c in ship:
            if not (0 <= r < n and 0 <= c < n):
                problems.append(f"Корабль №{i + 1} выходит за поле.")
            if (r, c) in occ:
                problems.append("Корабли пересекаются.")
            occ[(r, c)] = i
    for i, ship in enumerate(ships):
        for r, c in ring_cells(n, ship, rules.touch):
            j = occ.get((r, c))
            if j is not None and j != i:
                problems.append("Корабли касаются друг друга — по правилам так нельзя.")
                return sorted(set(problems))
    return sorted(set(problems))
