"""Вероятностный решатель: куда стрелять, чтобы попасть с максимальным шансом.

Идея: перебираем (сэмплируем) расстановки ВСЕХ оставшихся кораблей соперника, которые
согласуются с тем, что видно на поле: промахи пустые, «ранил» накрыт кораблём, вокруг
убитых пусто, корабли не касаются (по выбранному правилу). Доля расстановок, в которых
клетка занята кораблём, — это честная вероятность попадания в неё. Бьём в максимум.

Сэмплирование — цепь Маркова (Гиббс): по очереди «вынимаем» корабль и ставим его заново
равновероятно на любое допустимое место; парные ходы позволяют менять, какой корабль
накрывает раненую клетку. Стационарное распределение — равномерное по всем допустимым
расстановкам, т.е. ровно то, что нужно. Поле хранится битовыми масками (int на 100 бит).
"""
from __future__ import annotations

import random
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .board import N4, N8, Prepared, prepare
from .rules import (HIT, MISS, SUNK, TOUCH_ANY, TOUCH_CORNERS, TOUCH_NONE, UNKNOWN,
                    Rules)


def lowbit_index(x: int) -> int:
    return (x & -x).bit_length() - 1


def mask_from_bool(arr: np.ndarray) -> int:
    flat = np.ascontiguousarray(arr, dtype=bool).ravel()
    return int.from_bytes(np.packbits(flat, bitorder="little").tobytes(), "little")


def masks_to_counts(masks, nn: int) -> np.ndarray:
    if not masks:
        return np.zeros(nn, dtype=np.int64)
    nbytes = (nn + 7) // 8
    buf = b"".join(m.to_bytes(nbytes, "little") for m in masks)
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(-1, nbytes)
    bits = np.unpackbits(arr, axis=1, bitorder="little")[:, :nn]
    return bits.sum(axis=0, dtype=np.int64)


class Geometry:
    """Все положения кораблей каждой длины на поле n×n + их «ореолы» для правила касания."""
    _cache: dict = {}

    def __init__(self, n: int, touch: str):
        self.n = n
        self.nn = n * n
        self.touch = touch
        offs = N8 if touch == TOUCH_NONE else N4 if touch == TOUCH_CORNERS else ()
        self.nbr = []
        for r in range(n):
            for c in range(n):
                m = 0
                for dr, dc in offs:
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < n and 0 <= cc < n:
                        m |= 1 << (rr * n + cc)
                self.nbr.append(m)
        self._pl = {}

    @classmethod
    def get(cls, n: int, touch: str) -> "Geometry":
        key = (n, touch)
        g = cls._cache.get(key)
        if g is None:
            g = cls._cache[key] = Geometry(n, touch)
        return g

    def placements(self, L: int):
        """(cells, halo, idx, cover): маски клеток, маски ореолов, индексы клеток, индекс покрытия."""
        got = self._pl.get(L)
        if got is not None:
            return got
        n = self.n
        cells, halo, idxs = [], [], []
        dirs = ((0, 1),) if L == 1 else ((0, 1), (1, 0))
        for dr, dc in dirs:
            for r in range(n - dr * (L - 1)):
                for c in range(n - dc * (L - 1)):
                    ii = tuple((r + dr * k) * n + (c + dc * k) for k in range(L))
                    m = 0
                    h = 0
                    for i in ii:
                        m |= 1 << i
                        h |= self.nbr[i]
                    cells.append(m)
                    halo.append(h | m)
                    idxs.append(ii)
        cover = [[] for _ in range(self.nn)]
        for p, ii in enumerate(idxs):
            for i in ii:
                cover[i].append(p)
        got = self._pl[L] = (cells, halo, idxs, cover)
        return got


class _Budget(Exception):
    pass


class Problem:
    """Статическая часть задачи: допустимые положения кораблей при данных наблюдениях."""

    def __init__(self, geo: Geometry, lengths, forbidden: int, required: int, unknown: int,
                 need_unknown: bool):
        self.geo = geo
        self.lengths = sorted(lengths, reverse=True)
        self.R = required
        self.C, self.H, self.valid, self.vcover, self.idx = {}, {}, {}, {}, {}
        for L in set(self.lengths):
            cells, halo, idxs, cover = geo.placements(L)
            ok = [False] * len(cells)
            valid = []
            for p, c in enumerate(cells):
                if c & forbidden:
                    continue
                if (halo[p] & ~c) & required:      # к раненому кораблю впритык нельзя
                    continue
                if need_unknown and (c & required) and not (c & unknown):
                    continue                       # весь корабль подбит → был бы «убит»
                ok[p] = True
                valid.append(p)
            self.C[L] = cells
            self.H[L] = halo
            self.idx[L] = idxs
            self.valid[L] = valid
            self.vcover[L] = [[p for p in cover[i] if ok[p]] for i in range(geo.nn)]
        m = len(self.lengths)
        self.diff_pairs = [(a, b) for a in range(m) for b in range(a + 1, m)
                           if self.lengths[a] != self.lengths[b]]


class Chain:
    """Одна цепь Маркова по расстановкам."""

    def __init__(self, prob: Problem, rng: random.Random):
        self.P = prob
        self.rng = rng
        m = len(prob.lengths)
        self.m = m
        self.pl = [0] * m
        self.cells = [0] * m
        self.halo = [0] * m

    # ---------- поиск стартовой расстановки (рандомизированный перебор с возвратами)
    def initialize(self, max_nodes: int = 40000, restarts: int = 6):
        """True — нашли; False — доказано, что расстановок нет; None — не уложились в бюджет."""
        P = self.P
        if not P.lengths:
            return True
        for _ in range(restarts):
            self._nodes = 0
            self._max_nodes = max_nodes
            counts = Counter(P.lengths)
            placed = []
            try:
                ok = self._dfs(P.R, 0, counts, placed, {})
            except _Budget:
                continue
            if not ok:
                return False
            # раскладываем найденное по «корабельным слотам» (длины по убыванию)
            slots = {}
            for k, L in enumerate(P.lengths):
                slots.setdefault(L, []).append(k)
            for L, p in placed:
                k = slots[L].pop()
                self.pl[k] = p
                self.cells[k] = P.C[L][p]
                self.halo[k] = P.H[L][p]
            return True
        return None

    def _dfs(self, need, halo_acc, counts, placed, last):
        self._nodes += 1
        if self._nodes > self._max_nodes:
            raise _Budget
        P, rng = self.P, self.rng
        if need:
            c0 = lowbit_index(need)
            cands = []
            for L, k in counts.items():
                if k <= 0:
                    continue
                C = P.C[L]
                for p in P.vcover[L][c0]:
                    if not (C[p] & halo_acc):
                        cands.append((L, p))
            rng.shuffle(cands)
            for L, p in cands:
                counts[L] -= 1
                placed.append((L, p))
                if self._dfs(need & ~P.C[L][p], halo_acc | P.H[L][p], counts, placed, last):
                    return True
                counts[L] += 1
                placed.pop()
            return False
        L = max((L for L, k in counts.items() if k > 0), default=None)
        if L is None:
            return True
        C = P.C[L]
        lo = last.get(L, -1)   # одинаковые корабли ставим по возрастанию индекса (без перестановок)
        cands = [p for p in P.valid[L] if p > lo and not (C[p] & halo_acc)]
        if len(cands) < counts[L]:
            return False
        rng.shuffle(cands)
        for p in cands:
            counts[L] -= 1
            placed.append((L, p))
            prev = last.get(L)
            last[L] = p
            if self._dfs(0, halo_acc | P.H[L][p], counts, placed, last):
                return True
            if prev is None:
                del last[L]
            else:
                last[L] = prev
            counts[L] += 1
            placed.pop()
        return False

    # ---------- шаги цепи
    def sweep(self):
        P, rng = self.P, self.rng
        m = self.m
        R = P.R
        cells, halo, lens, pl = self.cells, self.halo, P.lengths, self.pl
        order = list(range(m))
        rng.shuffle(order)
        for k in order:
            oh = 0
            oc = 0
            for j in range(m):
                if j != k:
                    oh |= halo[j]
                    oc |= cells[j]
            L = lens[k]
            C = P.C[L]
            need = R & ~oc
            if need:
                c0 = lowbit_index(need)
                cands = [p for p in P.vcover[L][c0] if not (C[p] & oh) and (C[p] & need) == need]
            else:
                cands = [p for p in P.valid[L] if not (C[p] & oh)]
            if not cands:          # не должно случаться: текущее положение всегда допустимо
                continue
            p = cands[rng.randrange(len(cands))]
            pl[k] = p
            cells[k] = C[p]
            halo[k] = P.H[L][p]
        if R:
            for _ in range(6):
                self.pair_move()

    def pair_move(self):
        """Совместно переставить два корабля разной длины (Гиббс по паре).

        Пару выбираем НЕ глядя на текущее состояние — иначе нарушается баланс цепи
        (проверено тестом против полного перебора). Если пара не участвует в накрытии
        «ранил» (need пуст — это зависит только от остальных кораблей), шаг пропускаем:
        одиночные ходы и так перемешивают такие корабли.
        """
        P, rng = self.P, self.rng
        pairs_all = P.diff_pairs
        if not pairs_all:
            return
        k1, k2 = pairs_all[rng.randrange(len(pairs_all))]
        m = self.m
        R = P.R
        L1 = P.lengths[k1]
        L2 = P.lengths[k2]
        bh = 0
        bc = 0
        for j in range(m):
            if j != k1 and j != k2:
                bh |= self.halo[j]
                bc |= self.cells[j]
        need = R & ~bc
        if not need:
            return
        c0 = lowbit_index(need)
        pairs = []
        self._pairs(L1, L2, c0, need, bh, pairs, False)
        self._pairs(L2, L1, c0, need, bh, pairs, True)
        if not pairs:
            return
        a, b = pairs[rng.randrange(len(pairs))]
        self.pl[k1], self.cells[k1], self.halo[k1] = a, P.C[L1][a], P.H[L1][a]
        self.pl[k2], self.cells[k2], self.halo[k2] = b, P.C[L2][b], P.H[L2][b]

    def _pairs(self, La, Lb, c0, need, bh, out, swap):
        P = self.P
        Ca, Ha, Cb = P.C[La], P.H[La], P.C[Lb]
        for pa in P.vcover[La][c0]:
            ca = Ca[pa]
            if ca & bh:
                continue
            need2 = need & ~ca
            h = bh | Ha[pa]
            if need2:
                c1 = lowbit_index(need2)
                for pb in P.vcover[Lb][c1]:
                    cb = Cb[pb]
                    if not (cb & h) and (cb & need2) == need2:
                        out.append((pb, pa) if swap else (pa, pb))
            else:
                for pb in P.valid[Lb]:
                    if not (Cb[pb] & h):
                        out.append((pb, pa) if swap else (pa, pb))

    def union(self) -> int:
        u = 0
        for c in self.cells:
            u |= c
        return u

    def ships(self):
        """Текущая расстановка: список (длина, кортеж индексов клеток)."""
        return [(L, self.P.idx[L][p]) for L, p in zip(self.P.lengths, self.pl)]


@dataclass
class SampleResult:
    counts: np.ndarray
    samples: int
    focus_lengths: Counter


def run_mcmc(prob: Problem, rng: random.Random, budget: float, *, chains: int = 4,
             burn: int = 10, max_samples: int = 40000, min_samples: int = 200,
             cancel=None):
    """None — расстановок нет (противоречие); иначе SampleResult."""
    nn = prob.geo.nn
    if not prob.lengths:
        return SampleResult(np.zeros(nn, dtype=np.int64), 1, Counter())
    t0 = time.perf_counter()
    cs = []
    for i in range(chains):
        ch = Chain(prob, random.Random(rng.random()))
        st = ch.initialize()
        if st is False and i == 0:
            return None
        if st:
            cs.append(ch)
        if time.perf_counter() - t0 > budget * 0.5 and cs:
            break
    if not cs:
        return None
    for ch in cs:
        for _ in range(burn):
            ch.sweep()
    masks = []
    focus = Counter()
    fcell = lowbit_index(prob.R) if prob.R else -1
    fbit = (1 << fcell) if fcell >= 0 else 0
    i = 0
    while True:
        ch = cs[i % len(cs)]
        ch.sweep()
        masks.append(ch.union())
        if fbit:
            for L, c in zip(prob.lengths, ch.cells):
                if c & fbit:
                    focus[L] += 1
                    break
        i += 1
        if i % 16 == 0:
            if cancel is not None and cancel():
                break
            el = time.perf_counter() - t0
            if (el > budget and i >= min_samples) or i >= max_samples or el > budget * 4:
                break
    return SampleResult(masks_to_counts(masks, nn), len(masks), focus)


def heuristic_counts(geo: Geometry, lengths, forbidden: int, required: int, unknown: int,
                     need_unknown: bool) -> np.ndarray:
    """Запасной быстрый способ: каждый корабль отдельно (без учёта взаимного влияния)."""
    nn = geo.nn
    acc = np.zeros(nn)
    for L in lengths:
        cells, halo, idxs, _ = geo.placements(L)
        w_cells = np.zeros(nn)
        total = 0.0
        for p, c in enumerate(cells):
            if c & forbidden or (halo[p] & ~c) & required:
                continue
            if need_unknown and (c & required) and not (c & unknown):
                continue
            w = 1.0
            hits = bin(c & required).count("1")
            if hits:
                w = 40.0 ** hits
            w_cells[list(idxs[p])] += w
            total += w
        if total > 0:
            acc += w_cells / total
    return np.clip(acc, 0, 1)


@dataclass
class Analysis:
    probs: np.ndarray                      # вероятность корабля в каждой клетке
    best: tuple | None                     # (r, c) куда бить
    top: list                              # [(r, c, p), ...] лучшие варианты
    mode: str                              # 'hunt' | 'target' | 'done'
    remaining: list                        # длины не потопленных кораблей
    sunk_ships: list
    states: np.ndarray                     # состояния после логического вывода
    known_empty: np.ndarray                # bool: тут точно пусто
    samples: int = 0
    method: str = "mcmc"
    relaxed: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    focus_lengths: dict = field(default_factory=dict)  # длина корабля под «ранил» → вероятность
    hit_groups: list = field(default_factory=list)
    elapsed: float = 0.0
    fleet_issue: str = ""                  # поле противоречит составу флота (см. board.prepare)
    by_length: list = field(default_factory=list)  # группы, «убитые» только по длине (при строгих правилах)

    @property
    def best_prob(self) -> float:
        if self.best is None:
            return 0.0
        return float(self.probs[self.best])


def board_seed(states: np.ndarray, rules: Rules) -> int:
    return zlib.crc32(np.asarray(states, dtype=np.int8).tobytes() + repr(rules).encode())


def analyze(states, rules: Rules, *, budget: float = 0.6, seed=None, cancel=None,
            top_k: int = 5) -> Analysis:
    t0 = time.perf_counter()
    n = rules.size
    st_in = np.array(states, dtype=np.int8).reshape(n, n)
    rng = random.Random(board_seed(st_in, rules) if seed is None else seed)

    ladder = [({}, None)]
    if rules.sunk_marked:
        ladder.append(({"sunk_marked": False},
                       "не все «убит» распознаны — считаю, что часть «ранил» может быть убитыми"))
    if rules.touch != TOUCH_ANY:
        ladder.append(({"sunk_marked": False, "touch": TOUCH_ANY},
                       "расстановка не сходится с правилом касания — проверяю без него"))
    ladder.append(({"sunk_marked": False, "touch": TOUCH_ANY, "trust_sunk": False},
                   "игнорирую отметки «убит»"))

    prep = None
    res = None
    relaxed = []
    issues = []
    strict = None
    for opts, note in ladder:
        p = prepare(st_in, rules, **opts)
        if note is None:
            issues = list(p.issues)
            strict = p
        if p.fatal:
            if note:
                relaxed.append(note)
            continue
        geo = Geometry.get(n, p.touch)
        prob = Problem(geo, p.remaining, mask_from_bool(p.forbidden | (p.states == SUNK)),
                       mask_from_bool(p.required), mask_from_bool(p.unknown), p.sunk_marked)
        left = max(0.15, budget - (time.perf_counter() - t0))
        r = run_mcmc(prob, rng, left, cancel=cancel)
        if r is not None:
            prep, res = p, r
            if note:
                relaxed.append(note)
            break
        if note:
            relaxed.append(note)

    method = "mcmc"
    if res is None:
        # противоречие везде — эвристика по последнему варианту
        prep = prepare(st_in, rules, sunk_marked=False, touch=TOUCH_ANY, trust_sunk=False)
        geo = Geometry.get(n, prep.touch)
        probs = heuristic_counts(geo, prep.remaining, mask_from_bool(prep.forbidden),
                                 mask_from_bool(prep.required), mask_from_bool(prep.unknown),
                                 False).reshape(n, n)
        samples = 0
        method = "heuristic"
        focus = {}
        if not issues:
            issues = ["Поле противоречит правилам — проверь распознанные клетки."]
    else:
        probs = (res.counts / max(res.samples, 1)).reshape(n, n).astype(float)
        samples = res.samples
        tot = sum(res.focus_lengths.values())
        focus = {L: k / tot for L, k in sorted(res.focus_lengths.items())} if tot else {}

    st = prep.states
    probs = probs.copy()
    probs[(st == HIT) | (st == SUNK)] = 1.0
    probs[st == MISS] = 0.0
    candidates = (st == UNKNOWN)
    known_empty = candidates & prep.forbidden
    probs[known_empty] = 0.0

    if not prep.remaining and not prep.required.any():
        mode = "done"
    elif prep.required.any():
        mode = "target"
    else:
        mode = "hunt"

    top = []
    best = None
    if mode != "done":
        cand_idx = np.flatnonzero(candidates.ravel() & ~known_empty.ravel())
        if cand_idx.size:
            vals = probs.ravel()[cand_idx]
            # стабильный порядок: по убыванию вероятности, при равенстве — ближе к центру
            rr, cc = np.divmod(cand_idx, n)
            center = (n - 1) / 2
            dist = (rr - center) ** 2 + (cc - center) ** 2
            order = np.lexsort((dist, -np.round(vals, 4)))
            for j in order[:top_k]:
                top.append((int(rr[j]), int(cc[j]), float(vals[j])))
            if top and top[0][2] > 0:
                best = (top[0][0], top[0][1])

    return Analysis(probs=probs, best=best, top=top, mode=mode, remaining=list(prep.remaining),
                    sunk_ships=prep.sunk_ships, states=st, known_empty=known_empty,
                    samples=samples, method=method, relaxed=relaxed, issues=issues,
                    focus_lengths=focus, hit_groups=prep.hit_groups,
                    elapsed=time.perf_counter() - t0,
                    fleet_issue=strict.fleet_issue if strict is not None else "",
                    by_length=list(strict.by_length) if strict is not None else [])


def random_layout(rules: Rules, rng: random.Random | None = None, sweeps: int = 30):
    """Равновероятная случайная расстановка всего флота: список кораблей (списки (r, c))."""
    rng = rng or random.Random()
    n = rules.size
    geo = Geometry.get(n, rules.touch)
    prob = Problem(geo, rules.fleet, 0, 0, (1 << geo.nn) - 1, False)
    ch = Chain(prob, rng)
    if not ch.initialize(max_nodes=200000, restarts=20):
        raise RuntimeError("не удалось расставить флот при таких правилах")
    for _ in range(sweeps):
        ch.sweep()
    return [[divmod(i, n) for i in idx] for _, idx in ch.ships()]


def sample_layouts(rules: Rules, count: int, rng: random.Random | None = None,
                   burn: int = 40, thin: int = 3):
    """Много почти независимых случайных расстановок одной цепью (быстрее, чем по одной)."""
    rng = rng or random.Random()
    n = rules.size
    geo = Geometry.get(n, rules.touch)
    prob = Problem(geo, rules.fleet, 0, 0, (1 << geo.nn) - 1, False)
    ch = Chain(prob, rng)
    if not ch.initialize(max_nodes=200000, restarts=20):
        raise RuntimeError("не удалось расставить флот при таких правилах")
    for _ in range(burn):
        ch.sweep()
    out = []
    for _ in range(count):
        for _ in range(thin):
            ch.sweep()
        out.append([[divmod(i, n) for i in idx] for _, idx in ch.ships()])
    return out
