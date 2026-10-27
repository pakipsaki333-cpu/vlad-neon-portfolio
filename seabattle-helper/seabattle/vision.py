"""Распознавание поля с экрана — универсально, без привязки к конкретному сайту.

Что умеет:
  * find_grids()   — сам находит на скриншоте сетки N×N (по длинным равноотстоящим линиям);
  * refine_grid()  — уточняет примерно выделенную рамку до точной сетки;
  * extract_patches() — нарезает клетки в маленькие «патчи» S×S (без линий сетки);
  * BoardTracker   — следит за полем соперника кадр за кадром:
      - помнит, как выглядит каждая клетка пустой (работает на любом фоне и текстуре);
      - новая отметка = клетка заметно изменилась; красное/оранжевое → «ранил», иначе «мимо»;
      - «ранил» сменил вид и стабилен → «убил»;
      - затемнение поля (чужой ход) компенсирует, мышь над клеткой не путает с отметкой;
      - массовая перемена → новая партия/перекрытие окна; поле пропало → сообщит;
      - учится на исправлениях пользователя (ближайший сосед по образцам).
Модуль чисто numpy — без Qt, легко тестируется.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import numpy as np

from .rules import HIT, LETTERS_TOP, MISS, NUMBERS_TOP, SUNK, UNKNOWN

PATCH = 16           # размер патча клетки
MARGIN = 0.16        # отступ от линий сетки внутри клетки (доля размера клетки)


# ----------------------------------------------------------------------------- геометрия
@dataclass
class GridGeom:
    x0: float
    y0: float
    cw: float
    ch: float
    n: int = 10

    @property
    def width(self) -> float:
        return self.cw * self.n

    @property
    def height(self) -> float:
        return self.ch * self.n

    def cell_center(self, r: int, c: int):
        return self.x0 + (c + 0.5) * self.cw, self.y0 + (r + 0.5) * self.ch

    def cell_at(self, x: float, y: float):
        c = int(np.floor((x - self.x0) / self.cw))
        r = int(np.floor((y - self.y0) / self.ch))
        if 0 <= r < self.n and 0 <= c < self.n:
            return r, c
        return None

    def shifted(self, dx: float, dy: float) -> "GridGeom":
        return GridGeom(self.x0 + dx, self.y0 + dy, self.cw, self.ch, self.n)

    def scaled(self, k: float) -> "GridGeom":
        return GridGeom(self.x0 * k, self.y0 * k, self.cw * k, self.ch * k, self.n)

    def to_dict(self) -> dict:
        return {"x0": self.x0, "y0": self.y0, "cw": self.cw, "ch": self.ch, "n": self.n}

    @classmethod
    def from_dict(cls, d) -> "GridGeom":
        return cls(float(d["x0"]), float(d["y0"]), float(d["cw"]), float(d["ch"]), int(d.get("n", 10)))


def _f32(img) -> np.ndarray:
    a = np.asarray(img)
    if a.ndim == 2:
        a = np.repeat(a[..., None], 3, axis=2)
    return a[..., :3].astype(np.float32, copy=False)


# ----------------------------------------------------------------------------- нарезка клеток
def extract_patches(img, geom: GridGeom, S: int = PATCH, margin: float = MARGIN) -> np.ndarray:
    """(n, n, S, S, 3) float32: усреднённое содержимое внутренней части каждой клетки."""
    n = geom.n
    a = np.asarray(img)
    H, W = a.shape[:2]
    # вырезаем только область поля (+запас) — интегральное изображение по ней дешёвое
    bx0 = int(max(0, np.floor(geom.x0) - 2))
    by0 = int(max(0, np.floor(geom.y0) - 2))
    bx1 = int(min(W, np.ceil(geom.x0 + geom.width) + 2))
    by1 = int(min(H, np.ceil(geom.y0 + geom.height) + 2))
    if bx1 - bx0 < n or by1 - by0 < n:
        return np.zeros((n, n, S, S, 3), np.float32)
    sub = _f32(a[by0:by1, bx0:bx1])
    h, w = sub.shape[:2]
    ii = np.zeros((h + 1, w + 1, 3), np.float64)
    ii[1:, 1:] = sub.cumsum(0, dtype=np.float64).cumsum(1)
    t = np.linspace(0.0, 1.0, S + 1)
    k = np.arange(n)

    def edges(origin, size, limit):
        s = origin + (k + margin) * size
        e = origin + (k + 1 - margin) * size
        ed = s[:, None] + (e - s)[:, None] * t[None, :]
        ei = np.clip(np.round(ed).astype(np.int64), 0, limit - 1)
        lo = ei[:, :-1]
        hi = np.maximum(ei[:, 1:], lo + 1)
        return lo, np.minimum(hi, limit)

    x1, x2 = edges(geom.x0 - bx0, geom.cw, w)
    y1, y2 = edges(geom.y0 - by0, geom.ch, h)
    Y1 = y1[:, None, :, None]
    Y2 = y2[:, None, :, None]
    X1 = x1[None, :, None, :]
    X2 = x2[None, :, None, :]
    sums = ii[Y2, X2] - ii[Y1, X2] - ii[Y2, X1] + ii[Y1, X1]
    area = ((Y2 - Y1) * (X2 - X1)).astype(np.float64)
    return (sums / area[..., None]).astype(np.float32)


def downsample(p: np.ndarray, k: int) -> np.ndarray:
    """(..., S, S, 3) → (..., k, k, 3) усреднением блоков (S кратно k)."""
    S = p.shape[-2]
    f = S // k
    sh = p.shape[:-3] + (k, f, k, f, 3)
    return p.reshape(sh).mean(axis=(-4, -2))


# ----------------------------------------------------------------------------- цвета
def warm_mask(p: np.ndarray) -> np.ndarray:
    """Красные/оранжевые/жёлтые пиксели — так почти везде рисуют попадание (крест, огонь)."""
    r, g, b = p[..., 0], p[..., 1], p[..., 2]
    red = (r >= 105) & (r - g >= 45) & (r - b >= 45)
    fire = (r >= 170) & (g >= 70) & (r - b >= 90) & (g - b >= 15)
    return red | fire


def estimate_background(patches: np.ndarray, mask=None) -> np.ndarray:
    n = patches.shape[0]
    med = np.median(patches.reshape(n, n, -1, 3), axis=2)
    cand = med[mask] if mask is not None and mask.any() else med.reshape(-1, 3)
    bg = np.median(cand, axis=0)
    close = cand[np.abs(cand - bg).max(axis=1) < 30]
    if len(close) >= 3:
        bg = np.median(close, axis=0)
    return bg.astype(np.float32)


def static_classify(patches: np.ndarray, bg=None, sensitivity: float = 1.0):
    """Классификация без «памяти» (по отличию от фона поля). Нужна при старте с середины партии."""
    if bg is None:
        bg = estimate_background(patches)
    dev = np.abs(patches - bg).max(axis=-1)
    ink = dev > 42.0 / sensitivity
    ink_frac = ink.mean(axis=(2, 3))
    warm = warm_mask(patches) & ink
    warm_frac = warm.sum(axis=(2, 3)) / np.maximum(ink.sum(axis=(2, 3)), 1)
    n = patches.shape[0]
    st = np.full((n, n), UNKNOWN, np.int8)
    marked = ink_frac >= 0.018 / sensitivity
    st[marked] = MISS
    st[marked & (warm_frac >= 0.3)] = HIT
    return st


# ----------------------------------------------------------------------------- поиск сетки
def _edge_maps(img):
    a = _f32(img)
    eh = np.abs(np.diff(a, axis=0)).max(axis=-1)   # (H-1, W): горизонтальные линии
    ev = np.abs(np.diff(a, axis=1)).max(axis=-1)   # (H, W-1): вертикальные линии
    return eh, ev


def _runs(binary: np.ndarray, min_len: int):
    """Горизонтальные отрезки True длиной ≥ min_len: массивы (row, x_start, x_end)."""
    a = binary.astype(np.int8)
    d = np.diff(np.pad(a, ((0, 0), (1, 1))), axis=1)
    rs, xs = np.nonzero(d == 1)
    re_, xe = np.nonzero(d == -1)
    ln = xe - xs
    keep = ln >= min_len
    return rs[keep], xs[keep], xe[keep]


def _close1d(b: np.ndarray, gap: int) -> np.ndarray:
    """Склеиваем разрывы вдоль оси 1 до gap пикселей (дилатация на gap/2 в обе стороны)."""
    if gap <= 0:
        return b
    dil = b.copy()
    h = max(1, gap // 2)
    for s in range(1, h + 1):
        dil[:, s:] |= b[:, :-s]
        dil[:, :-s] |= b[:, s:]
    return dil


def _line_families(edge: np.ndarray, thr: float, min_len: int, gap: int, merge: float = 6.0):
    """Длинные линии одного направления, сгруппированные по общей протяжённости.

    Края, лежащие ближе merge пикселей, сводим в одну линию: так толстая линия, двойной
    край тонкой линии и зазор между «плитками» дают одну линию по центру.
    """
    b = _close1d(edge > thr, gap)
    rows, xs, xe = _runs(b, min_len)
    if rows.size == 0:
        return []
    order = np.lexsort((rows, xe, xs))
    rows, xs, xe = rows[order], xs[order], xe[order]
    fams = []
    used = np.zeros(rows.size, bool)
    tol = 8
    for i in range(rows.size):
        if used[i]:
            continue
        sel = (~used) & (np.abs(xs - xs[i]) <= tol) & (np.abs(xe - xe[i]) <= tol)
        used |= sel
        ys = np.sort(np.unique(rows[sel]))
        centers = []
        start = prev = ys[0]
        for y in ys[1:]:
            if y - prev <= merge and y - start <= merge + 2:
                prev = y
                continue
            centers.append((start + prev) / 2.0 + 0.5)
            start = prev = y
        centers.append((start + prev) / 2.0 + 0.5)
        if len(centers) >= 8:
            fams.append((float(np.median(xs[sel])), float(np.median(xe[sel])), np.array(centers)))
    return fams


def _best_progression(centers: np.ndarray, n: int, min_step: float, max_step: float):
    """Лучшие n+1 равноотстоящих линий (допускаем одну пропущенную внутри).

    → (start, step, matched_mask) или None.
    """
    best = None
    m = len(centers)
    for i in range(m):
        for j in range(i + 1, m):
            step = (centers[j] - centers[i]) / n
            if not min_step <= step <= max_step:
                continue
            tol = max(2.0, 0.12 * step)
            exp = centers[i] + step * np.arange(n + 1)
            dist = np.abs(centers[None, :] - exp[:, None])
            hit = dist.min(axis=1) <= tol
            matched = int(hit.sum())
            if matched < n:
                continue
            # уточняем шаг и начало по найденным линиям (МНК)
            ks = np.nonzero(hit)[0]
            ys = centers[dist.argmin(axis=1)[ks]]
            k_mean = ks.mean()
            step2 = float(((ks - k_mean) * (ys - ys.mean())).sum() / max(((ks - k_mean) ** 2).sum(), 1e-9))
            start2 = float(ys.mean() - step2 * k_mean)
            score = matched + 0.001 * step
            if best is None or score > best[3]:
                used = np.zeros(m, bool)
                used[dist.argmin(axis=1)[ks]] = True
                best = (start2, step2, used, score)
    return best


def _progressions(centers: np.ndarray, n: int, min_step: float, max_step: float):
    """Все непересекающиеся сетки в наборе линий (например, два поля на одной высоте)."""
    out = []
    rest = np.array(centers, dtype=float)
    for _ in range(4):
        if len(rest) < n:
            break
        pr = _best_progression(rest, n, min_step, max_step)
        if pr is None:
            break
        start, step, used, _ = pr
        out.append((start, step))
        lo, hi = start - step * 0.5, start + step * (n + 0.5)
        rest = rest[~used & ((rest < lo) | (rest > hi))]
    return out


def _same_grid(a: GridGeom, b: GridGeom) -> bool:
    return (abs(a.x0 - b.x0) < 0.5 * a.cw and abs(a.y0 - b.y0) < 0.5 * a.ch
            and abs(a.cw - b.cw) < 0.15 * a.cw)


def find_grids(img, n: int = 10, min_cell: int = 12, max_cell: int = 140):
    """Найти все сетки n×n на изображении. → список GridGeom, слева направо.

    Два способа, результаты объединяются:
      1) n+1 равноотстоящих линий одной длины — поле нарисовано своими линиями;
      2) замкнутая жирная рамка, внутри которой есть сетка с шагом сторона/n — так рисуют
         поля «на тетрадном листе» (Яндекс Игры и др.): линии клеток идут по всей странице,
         и только рамка показывает, где поле.
    Каждое найденное поле «прилипает» к самой сильной рамке (сдвиг на клетку-две), чтобы на
    клетчатом фоне не перепутать поле с соседним квадратом 10×10.
    """
    a = _f32(img)
    eh, ev = _edge_maps(a)
    min_len = int(min_cell * n * 0.9)
    found = []
    for thr in (40.0, 12.0):
        hf = _line_families(eh, thr, min_len, gap=12)
        vf = _line_families(ev.T, thr, min_len, gap=12)
        hcands = [(x1, x2, pr) for x1, x2, ys in hf for pr in _progressions(ys, n, min_cell, max_cell)]
        vcands = [(y1, y2, pr) for y1, y2, xs in vf for pr in _progressions(xs, n, min_cell, max_cell)]
        for hx1, hx2, (y0, sy) in hcands:
            for vy1, vy2, (x0, sx) in vcands:
                if abs(sx - sy) > 0.12 * max(sx, sy):
                    continue
                tol = max(8.0, 0.35 * sx)
                if abs(x0 - hx1) > tol or abs(x0 + n * sx - hx2) > tol:
                    continue
                if abs(y0 - vy1) > tol or abs(y0 + n * sy - vy2) > tol:
                    continue
                g = GridGeom(float(x0), float(y0), float(sx), float(sy), n)
                if not any(_same_grid(g, f) for f in found):
                    found.append(g)
    for g in _frame_grids(a, eh, ev, n, min_cell, max_cell):
        if not any(_same_grid(g, f) for f in found):
            found.append(g)
    out = []
    for g in found:
        g = _snap_to_frame(eh, ev, g)
        if not any(_same_grid(g, f) for f in out):
            out.append(g)
    out.sort(key=lambda g: (g.x0, g.y0))
    return out


# ----------------------------------------------------------------------------- рамка поля
def _strong_segments(edge: np.ndarray, thr: float, min_len: int, gap: int = 8, merge: int = 6):
    """Длинные прямые отрезки сильных краёв (по строкам edge). Края одной толстой линии
    (верхний и нижний) сводим в одну линию по центру. → [(позиция, начало, конец)]."""
    b = _close1d(edge > thr, gap)
    rows, xs, xe = _runs(b, min_len)
    h = max(1, gap // 2)                     # _close1d расширил отрезки на h с каждой стороны
    order = np.lexsort((xs, rows))
    lines = []
    for i in order:
        r, s, e = int(rows[i]), int(xs[i]) + h, int(xe[i]) - h
        for L in lines:
            if r - L[1] <= merge and r - L[0] <= merge + 4 and abs(s - L[2]) <= 8 and abs(e - L[3]) <= 8:
                L[1] = r
                L[2] = min(L[2], s)
                L[3] = max(L[3], e)
                break
        else:
            lines.append([r, r, s, e])
    return [((r0 + r1) / 2.0 + 0.5, float(s), float(e)) for r0, r1, s, e in lines]


def _fill_gaps(v: np.ndarray, gap: int) -> np.ndarray:
    """1D: закрываем разрывы в True короче gap (линию перекрыла отметка, курсор и т.п.)."""
    if v.size == 0 or not v.any():
        return v
    idx = np.nonzero(v)[0]
    out = v.copy()
    for a_, b_ in zip(idx[:-1], idx[1:]):
        if 1 < b_ - a_ <= gap:
            out[a_:b_] = True
    return out


def _band_line(B: np.ndarray, pos: float, lo: float, hi: float, tol: int):
    """Есть ли линия около pos (строки B) на участке [lo, hi) столбцов → (покрытие 0..1, точная позиция)."""
    p0 = int(round(pos)) - tol
    p1 = int(round(pos)) + tol + 1
    p0c, p1c = max(0, p0), min(B.shape[0], p1)
    l0, l1 = max(0, int(lo)), min(B.shape[1], int(hi))
    if p1c - p0c < 1 or l1 - l0 < 8:
        return 0.0, pos
    band = B[p0c:p1c, l0:l1]
    cov = float(_fill_gaps(band.any(axis=0), 10).mean())
    cnt = band.sum(axis=1).astype(np.float64)
    if cnt.max() <= 0:
        return cov, pos
    strong = cnt >= 0.5 * cnt.max()          # оба края толстой линии
    ks = np.nonzero(strong)[0]
    return cov, p0c + (ks.min() + ks.max()) / 2.0 + 0.5


def _lattice_contrast(E: np.ndarray, start: float, step: float, n: int, lo: float, hi: float):
    """Сетка внутри рамки: медиана краёв по линиям решётки против середин клеток.

    E — карта краёв, где строки — «поперёк» линий (для вертикальных линий — строки изображения).
    → (сила на линиях, сила в серединах клеток)."""
    rows = np.clip(np.linspace(lo, hi, 40).astype(int), 0, E.shape[0] - 1)
    c0 = int(max(0, np.floor(start - step * 0.5)))
    c1 = int(min(E.shape[1], np.ceil(start + step * (n + 0.5))))
    if c1 - c0 < n * 2:
        return 0.0, 0.0
    prof = np.median(E[rows, c0:c1], axis=0)
    r = max(1, int(round(0.06 * step)))

    def val(x):
        i = int(round(x)) - c0
        seg = prof[max(0, i - r): max(0, min(prof.size, i + r + 1))]
        return float(seg.max()) if seg.size else 0.0

    lines = [val(start + k * step) for k in range(1, n)]
    mids = [val(start + (k + 0.5) * step) for k in range(n)]
    return float(np.mean(lines)), float(np.mean(mids))


def _frame_grids(a: np.ndarray, eh: np.ndarray, ev: np.ndarray, n: int, min_cell: int, max_cell: int,
                 thr: float = 100.0):
    """Поля в замкнутой жирной рамке; внутри рамки обязана быть сетка с шагом сторона/n."""
    Bh = eh > thr                 # строки — y
    Bv = (ev > thr).T             # строки — x
    min_len = int(n * min_cell * 0.9)
    segs = _strong_segments(eh, thr, min_len)
    out = []
    for y, xs, xe in segs:
        W = xe - xs
        if not n * min_cell * 0.95 <= W <= n * max_cell * 1.05:
            continue
        tol = int(max(4, round(0.015 * W)))
        for sgn in (1, -1):               # отрезок — верх рамки (низ ищем ниже) или её низ
            ys_ = np.arange(int(y + sgn * 0.85 * W), int(y + sgn * 1.18 * W), sgn)
            ys_ = ys_[(ys_ >= 0) & (ys_ < Bh.shape[0])]
            if ys_.size == 0:
                continue
            cov_rows = Bh[ys_, max(0, int(xs)):int(xe)].mean(axis=1)
            if cov_rows.size == 0 or cov_rows.max() < 0.6:
                continue
            # среди хороших строк — ближайшая к квадрату
            good = np.nonzero(cov_rows >= 0.6)[0]
            j = good[np.argmin(np.abs(np.abs(ys_[good] - y) - W))]
            cov_b, y2 = _band_line(Bh, ys_[j], xs, xe, 4)
            if cov_b < 0.8:
                continue
            yt, yb = (y, y2) if sgn > 0 else (y2, y)
            cov_l, xl = _band_line(Bv, xs, yt, yb, tol)
            cov_r, xr = _band_line(Bv, xe, yt, yb, tol)
            if cov_l < 0.8 or cov_r < 0.8:
                continue
            cw, ch = (xr - xl) / n, (yb - yt) / n
            if not min_cell <= cw <= max_cell or not 0.85 <= cw / max(ch, 1e-6) <= 1.18:
                continue
            g = GridGeom(float(xl), float(yt), float(cw), float(ch), n)
            lv, mv = _lattice_contrast(ev, g.x0, g.cw, n, g.y0 + 0.1 * g.height, g.y0 + 0.9 * g.height)
            lh, mh = _lattice_contrast(eh.T, g.y0, g.ch, n, g.x0 + 0.1 * g.width, g.x0 + 0.9 * g.width)
            if lv < 1.8 * mv + 6 or lh < 1.8 * mh + 6:
                continue                  # внутри нет сетки с таким шагом — это не поле (окно, картинка)
            if not any(_same_grid(g, f) for f in out):
                out.append(g)
    refined = []
    for g in out:
        g2, conf = refine_grid(a, g, search=0.25, spread=0.04, snap=False, edges=(eh, ev))
        refined.append(g2 if conf >= 0.15 else g)
    return refined


def _line_mass(E: np.ndarray, pos: float, lo: float, hi: float, r: int) -> float:
    """«Сила» линии около pos: сумма краёв в полосе ±r, медиана по участку [lo, hi)."""
    p = int(round(pos))
    if p - r < 0 or p + r + 1 > E.shape[1]:
        return 0.0
    rows = np.clip(np.linspace(lo, hi, 48).astype(int), 0, E.shape[0] - 1)
    return float(np.median(E[rows, p - r:p + r + 1].sum(axis=1)))


def _snap_to_frame(eh: np.ndarray, ev: np.ndarray, g: GridGeom, max_shift: int = 2) -> GridGeom:
    """Сдвинуть сетку на целое число клеток, если так её края попадают на заметно более сильные
    линии (рамку поля). На клетчатом фоне без этого поле легко «съезжает» на клетку."""
    n = g.n
    for axis in (0, 1):
        if axis == 0:
            E, start, step = ev, g.x0, g.cw
            lo, hi = g.y0 + 0.08 * g.height, g.y0 + 0.92 * g.height
        else:
            E, start, step = eh.T, g.y0, g.ch
            lo, hi = g.x0 + 0.08 * g.width, g.x0 + 0.92 * g.width
        r = max(2, int(round(0.12 * step)))
        mass = {k: _line_mass(E, start + k * step, lo, hi, r)
                for k in range(-max_shift, n + max_shift + 1)}

        def score(s):
            return min(mass[s], mass[s + n])

        best = max(range(-max_shift, max_shift + 1), key=lambda s: (score(s), -abs(s)))
        if best != 0 and score(best) > 1.35 * score(0) + 8:
            g = g.shifted(best * step, 0) if axis == 0 else g.shifted(0, best * step)
    return g


def _profile(edge_abs: np.ndarray, axis: int, sigma: float = 1.0):
    """Профиль «линейности» вдоль оси; сглаживание сводит два края зазора/линии в один пик."""
    p = np.minimum(edge_abs, 120.0).mean(axis=axis)
    rad = max(1, int(np.ceil(sigma * 3)))
    x = np.arange(-rad, rad + 1)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    return np.convolve(p, k / k.sum(), mode="same")


def _fit_lattice(profile: np.ndarray, start0: float, step0: float, n: int, spread=0.15, search=1.0):
    steps = np.arange(step0 * (1 - spread), step0 * (1 + spread) + 0.05, 0.1)
    offs = np.arange(start0 - step0 * search, start0 + step0 * search + 0.01, 0.25)
    xs_k = np.arange(n + 1)
    best = (-1.0, start0, step0)
    grid_pos = np.arange(profile.size, dtype=np.float64)
    for s in steps:
        pos = offs[:, None] + xs_k[None, :] * s
        vals = np.interp(pos, grid_pos, profile, left=0.0, right=0.0)
        sc = vals.sum(axis=1)
        j = int(np.argmax(sc))
        if sc[j] > best[0]:
            best = (float(sc[j]), float(offs[j]), float(s))
    return best


def refine_grid(img, approx: GridGeom, search: float = 1.0, spread: float = 0.15, snap: bool = True,
                edges=None):
    """Подогнать примерную рамку к настоящим линиям сетки. → (GridGeom, уверенность 0..1).

    search — насколько (в клетках) можно сдвинуть начало, spread — шаг (доля).
    snap — затем «прилепить» к самой сильной рамке: на клетчатом фоне (тетрадный лист)
    решётка подходит при любом сдвиге на целую клетку, и только рамка говорит, где поле.
    """
    a = _f32(img)
    H, W = a.shape[:2]
    n = approx.n
    g = approx
    conf = 0.0
    for it in range(2):
        y_lo = int(max(0, g.y0 + 0.5 * g.ch))
        y_hi = int(min(H, g.y0 + g.height - 0.5 * g.ch))
        x_lo = int(max(0, g.x0 + 0.5 * g.cw))
        x_hi = int(min(W, g.x0 + g.width - 0.5 * g.cw))
        if y_hi - y_lo < 4 or x_hi - x_lo < 4:
            return approx, 0.0
        if edges is not None:             # уже посчитанные карты краёв всего изображения
            ev = edges[1][y_lo:y_hi]
            eh = edges[0][:, x_lo:x_hi]
        else:
            ev = np.abs(np.diff(a[y_lo:y_hi], axis=1)).max(axis=-1)
            eh = np.abs(np.diff(a[:, x_lo:x_hi], axis=0)).max(axis=-1)
        sig = max(2.0, 0.08 * min(g.cw, g.ch))
        pv = _profile(ev, 0, sig)
        ph = _profile(eh, 1, sig)
        # второй проход — только тонкая подгонка вокруг найденного
        srch, sprd = (search, spread) if it == 0 else (min(search, 0.25), min(spread, 0.03))
        sx, x0, cw = _fit_lattice(pv, g.x0 - 0.5, g.cw, n, sprd, srch)
        sy, y0, ch = _fit_lattice(ph, g.y0 - 0.5, g.ch, n, sprd, srch)
        g = GridGeom(x0 + 0.5, y0 + 0.5, cw, ch, n)
        base_v = np.median(pv) + 1e-3
        base_h = np.median(ph) + 1e-3
        cx = (sx / (n + 1)) / base_v
        cy = (sy / (n + 1)) / base_h
        conf = float(np.clip((min(cx, cy) - 1.3) / 2.5, 0.0, 1.0))
    if conf < 0.15:
        return approx, conf
    if snap:
        eh, ev = edges if edges is not None else _edge_maps(a)
        g = _snap_to_frame(eh, ev, g)
    return g, conf


def _label_widths(a: np.ndarray, boxes) -> list:
    """Ширина «чернил» подписи в каждой рамке (None — пусто или за краем)."""
    H, W = a.shape[:2]
    out = []
    for x0, y0, x1, y1 in boxes:
        x0, y0 = int(max(0, round(x0))), int(max(0, round(y0)))
        x1, y1 = int(min(W, round(x1))), int(min(H, round(y1)))
        if x1 - x0 < 4 or y1 - y0 < 4:
            out.append(None)
            continue
        box = a[y0:y1, x0:x1]
        bg = np.median(box.reshape(-1, 3), axis=0)
        ink = np.abs(box - bg).max(axis=-1) > 70
        cols = np.nonzero(ink.sum(axis=0) >= 1)[0]
        if ink.sum() < 6 or cols.size < 2:
            out.append(None)
            continue
        out.append(int(cols[-1] - cols[0] + 1))
    return out


def detect_label_layout(img, geom: GridGeom):
    """Где на сайте буквы, а где цифры. → LETTERS_TOP, NUMBERS_TOP или None (не понять).

    Смотрим подписи по всем четырём сторонам поля: подпись «10» почти вдвое шире «2»…«9»,
    а «К» не шире остальных букв. Так что сторона, где последняя подпись широкая, — цифры.
    """
    a = _f32(img)
    n, cw, ch = geom.n, geom.cw, geom.ch
    x0, y0, x1, y1 = geom.x0, geom.y0, geom.x0 + geom.width, geom.y0 + geom.height
    strips = {
        "top": [(x0 + (k + 0.1) * cw, y0 - 0.9 * ch, x0 + (k + 0.9) * cw, y0 - 0.12 * ch) for k in range(n)],
        "bottom": [(x0 + (k + 0.1) * cw, y1 + 0.12 * ch, x0 + (k + 0.9) * cw, y1 + 0.9 * ch) for k in range(n)],
        "left": [(x0 - 0.88 * cw, y0 + (k + 0.1) * ch, x0 - 0.12 * cw, y0 + (k + 0.9) * ch) for k in range(n)],
        "right": [(x1 + 0.12 * cw, y0 + (k + 0.1) * ch, x1 + 0.88 * cw, y0 + (k + 0.9) * ch) for k in range(n)],
    }
    votes = {LETTERS_TOP: 0, NUMBERS_TOP: 0}
    for side, boxes in strips.items():
        w = _label_widths(a, boxes)
        if sum(v is not None for v in w) < n - 2 or w[-1] is None:
            continue                     # подписей с этой стороны нет (или там что-то другое)
        mid = [v for v in w[1:-1] if v is not None]
        if len(mid) < n - 4:
            continue
        med = float(np.median(mid))
        cell = cw if side in ("top", "bottom") else ch
        if med > 0.75 * cell:
            continue                     # «подписи» во всю клетку — это не буквы, а что-то ещё
        numbers_here = w[-1] >= 1.45 * med and w[-1] >= 0.2 * cell
        horizontal = side in ("top", "bottom")
        if numbers_here == horizontal:
            votes[NUMBERS_TOP] += 1
        else:
            votes[LETTERS_TOP] += 1
    if votes[NUMBERS_TOP] > votes[LETTERS_TOP]:
        return NUMBERS_TOP
    if votes[LETTERS_TOP] > votes[NUMBERS_TOP]:
        return LETTERS_TOP
    return None


def paper_lattice(img, geom: GridGeom, others=()) -> bool:
    """«Тетрадный лист»: линии клеток с тем же шагом продолжаются и за рамкой поля — так рисуют
    «Морской Бой классический» на Яндекс Играх и Пикабу. Смотрим полосы снаружи поля (за
    подписями) с каждой стороны; полосы, задевающие другие поля (others), пропускаем.
    Под полем подписей нет, поэтому лист под ним — самый надёжный признак."""
    n, cw, ch = geom.n, geom.cw, geom.ch
    m = 3.0 * max(cw, ch)
    H, W = np.asarray(img).shape[:2]
    cx0, cy0 = int(max(0, geom.x0 - m)), int(max(0, geom.y0 - m))
    cx1, cy1 = int(min(W, geom.x0 + geom.width + m)), int(min(H, geom.y0 + geom.height + m))
    a = _f32(np.asarray(img)[cy0:cy1, cx0:cx1])
    g = geom.shifted(-cx0, -cy0)
    eh, ev = _edge_maps(a)
    h, w = a.shape[:2]
    x0, y0, x1, y1 = g.x0, g.y0, g.x0 + g.width, g.y0 + g.height
    spec = {
        # сторона: карта краёв (строки — поперёк линий), начало и шаг решётки, полоса, её рамка
        "bottom": (ev, x0, cw, y1 + 0.25 * ch, y1 + 1.75 * ch, h, (x0, y1 + 0.25 * ch, x1, y1 + 1.75 * ch)),
        "top": (ev, x0, cw, y0 - 2.75 * ch, y0 - 1.25 * ch, h, (x0, y0 - 2.75 * ch, x1, y0 - 1.25 * ch)),
        "left": (eh.T, y0, ch, x0 - 2.75 * cw, x0 - 1.25 * cw, w, (x0 - 2.75 * cw, y0, x0 - 1.25 * cw, y1)),
        "right": (eh.T, y0, ch, x1 + 1.25 * cw, x1 + 2.75 * cw, w, (x1 + 1.25 * cw, y0, x1 + 2.75 * cw, y1)),
    }
    found = {}
    for side, (E, start, step, lo, hi, lim, (rx0, ry0, rx1, ry1)) in spec.items():
        if lo < 0 or hi > lim - 1:
            continue
        busy = False
        for o in others:
            o = o.shifted(-cx0, -cy0)
            if (rx0 < o.x0 + o.width + 0.5 * o.cw and rx1 > o.x0 - 0.5 * o.cw
                    and ry0 < o.y0 + o.height + 0.5 * o.ch and ry1 > o.y0 - 0.5 * o.ch):
                busy = True
        if busy:
            continue
        lv, mv = _lattice_contrast(E, start, step, n, lo, hi)
        found[side] = lv >= 1.8 * mv + 6
    return bool(found.get("bottom")) or sum(found.values()) >= 2


def border_signature(img, geom: GridGeom, samples: int = 24):
    """Подпись поля — по ней понимаем, что поле не сдвинулось и его не закрыли другим окном.

    → (яркость вдоль вертикальных линий сетки, вдоль горизонтальных, яркость фона клеток).
    Фон берём не в центре клетки (там рисуют точки-промахи) и не на диагоналях (крестики),
    а у верхнего края клетки посередине.
    """
    a = _f32(img).mean(axis=-1)
    H, W = a.shape
    n = geom.n
    t = (np.arange(samples) + 0.5) / samples

    def pick(xs, ys):
        xs = np.clip(np.round(xs).astype(int), 0, W - 1)
        ys = np.clip(np.round(ys).astype(int), 0, H - 1)
        return a[ys, xs]

    k = np.arange(n + 1)[:, None]
    vert = pick(geom.x0 + k * geom.cw + 0 * t, geom.y0 + t * geom.height + 0 * k).ravel()
    horiz = pick(geom.x0 + t * geom.width + 0 * k, geom.y0 + k * geom.ch + 0 * t).ravel()
    gx, gy = np.meshgrid(geom.x0 + (np.arange(n) + 0.5) * geom.cw, geom.y0 + (np.arange(n) + 0.22) * geom.ch)
    return vert, horiz, pick(gx, gy).ravel()


def _signature_keep(n: int, samples: int, cells, pad: float = 0.8):
    """Какие точки подписи оставить, если вокруг клеток cells на линиях может быть нарисовано
    наше же (рамка-подсказка с «прицелом», когда Windows не прячет её от захвата экрана)."""
    t = (np.arange(samples) + 0.5) / samples * n          # где точка вдоль линии, в клетках
    keep_v = np.ones((n + 1, samples), bool)
    keep_h = np.ones((n + 1, samples), bool)
    for r, c in cells:
        for k in (c, c + 1):
            if 0 <= k <= n:
                keep_v[k, (t > r - pad) & (t < r + 1 + pad)] = False
        for k in (r, r + 1):
            if 0 <= k <= n:
                keep_h[k, (t > c - pad) & (t < c + 1 + pad)] = False
    return keep_v.ravel(), keep_h.ravel()


def signature_match(a, b, ignore=()) -> float:
    """Похожесть двух подписей поля: 1 — то же поле на том же месте, около 0 — что-то другое.

    Обычно это корреляция яркости вдоль линий: она не зависит от общей яркости, так что
    затемнение поля в ход соперника — не помеха. Если же все линии одного цвета (чистый рисунок
    без шума), корреляцию не посчитать — тогда сравниваем, насколько линии темнее/светлее фона
    клеток (отдельно вертикальные и горизонтальные): при затемнении это сохраняется, а при
    прокрутке или чужом окне сверху — нет. ignore — клетки, вокруг которых линии не смотрим
    (там может быть наша рамка-подсказка).
    """
    if a is None or b is None:
        return 0.0
    if len(a) != len(b) or any(x.shape != y.shape for x, y in zip(a, b)):
        return 0.0
    va, ha, ca = a
    vb, hb, cb = b
    if ignore:
        n = int(round(np.sqrt(ca.size)))
        samples = va.size // (n + 1)
        kv, kh = _signature_keep(n, samples, ignore)
        if kv.sum() >= 8 and kh.sum() >= 8:
            va, vb, ha, hb = va[kv], vb[kv], ha[kh], hb[kh]
    bg_a, bg_b = float(np.median(ca)), float(np.median(cb))
    worst = 1.0
    # вертикальные и горизонтальные линии — отдельно: прокрутка по вертикали сдвигает только
    # горизонтальные линии, а вертикальные остаются «на месте»
    for xa, xb in ((va, vb), (ha, hb)):
        if xa.std() >= 1.0 and xb.std() >= 1.0:
            da, db = xa - xa.mean(), xb - xb.mean()
            score = float((da * db).sum() / np.sqrt((da * da).sum() * (db * db).sum()))
        else:
            con_a = float(np.median(xa)) - bg_a
            con_b = float(np.median(xb)) - bg_b
            if abs(con_a) < 4.0:               # линии не отличить от фона — судим по яркости
                ok = abs(float(xa.mean()) - float(xb.mean())) < 25
            else:
                ok = np.sign(con_a) == np.sign(con_b) and abs(con_b) >= 0.3 * abs(con_a)
            score = 1.0 if ok else 0.0
        worst = min(worst, score)
    return worst


# ----------------------------------------------------------------------------- трекер
def cell_vector(raw: np.ndarray, diff: np.ndarray) -> np.ndarray:
    return np.concatenate([downsample(raw, 8).ravel() / 255.0,
                           downsample(diff, 8).ravel() / 255.0]).astype(np.float32)


def encode_vec(v: np.ndarray) -> str:
    q = np.clip(np.round((v + 1.0) * 127.5), 0, 255).astype(np.uint8)
    return base64.b64encode(q.tobytes()).decode()


def decode_vec(s: str) -> np.ndarray:
    q = np.frombuffer(base64.b64decode(s), np.uint8).astype(np.float32)
    return q / 127.5 - 1.0


@dataclass
class TrackResult:
    states: np.ndarray
    changed: bool
    status: str = "ok"       # ok | init | unstable | reinit | newgame | lost
    new_marks: list = None


class BoardTracker:
    """Следит за полем соперника и превращает картинку в состояния клеток."""

    def __init__(self, n: int = 10, S: int = PATCH):
        self.n = n
        self.S = S
        self.sensitivity = 1.0
        self.detect_sunk_look = True
        self.examples = []           # [(vec, label)] — исправления пользователя
        self.proto_hit = []          # как на этом сайте выглядит «ранил» (8×8×3)
        self.proto_sunk = []         # … и «убил» — узнаём сами по переходам ранил→убил
        self.reset()

    # ---------------- служебное
    def reset(self):
        n, S = self.n, self.S
        self.states = np.zeros((n, n), np.int8)
        self.base = None
        self.has_base = np.zeros((n, n), bool)
        self.mark_base = np.zeros((n, n, S, S, 3), np.float32)
        self.mark_age = np.zeros((n, n), np.int16)
        self.pending = np.full((n, n), -1, np.int8)
        self.pending_cnt = np.zeros((n, n), np.int16)
        self.prev = None
        self.bg = None
        self.massive = 0
        self.revert_frames = 0
        self.lit_frames = 0
        self.frames = 0

    def new_game(self):
        """Новая партия: состояния в ноль, «пустой» вид клеток берём со следующего кадра."""
        keep = (self.examples, self.proto_hit, self.proto_sunk)
        self.reset()
        self.examples, self.proto_hit, self.proto_sunk = keep

    @property
    def t_pix(self):
        return 36.0 / self.sensitivity

    @property
    def f_min(self):
        return 0.016 / self.sensitivity

    def _init_from(self, patches):
        self.bg = estimate_background(patches)
        self.states = static_classify(patches, self.bg, self.sensitivity)
        if self.examples:
            for r in range(self.n):
                for c in range(self.n):
                    lab = self._knn(patches[r, c], patches[r, c] - self.bg)
                    if lab is not None:
                        self.states[r, c] = lab
        self.base = patches.copy()
        self.has_base = self.states == UNKNOWN
        self.mark_base = patches.copy()
        self.mark_age[:] = 10
        self.pending[:] = -1
        self.pending_cnt[:] = 0

    def _normalize(self, P, ref, use):
        """Компенсация затемнения/подсветки поля: аффинно по каналам ref→current.

        ref — эталонный вид каждой клетки (пустой вид или вид отметки), use — какие клетки
        участвуют. → (P в «освещении» эталона, a(3), b(3), отклонение освещения на фоне).
        Клетки, изменившиеся не так, как большинство (новая отметка, подсветка), отбрасываем.
        """
        raw_med = np.median((P - ref).reshape(self.n, self.n, -1, 3), axis=2)
        gm = np.median(raw_med[use], axis=0)
        inl = use & (np.abs(raw_med - gm).max(axis=-1) < 20)
        if inl.sum() < 8:
            inl = use
        x = ref[inl].reshape(-1, 3).astype(np.float64)
        y = P[inl].reshape(-1, 3).astype(np.float64)
        if x.shape[0] > 6000:
            idx = np.linspace(0, x.shape[0] - 1, 6000).astype(int)
            x, y = x[idx], y[idx]
        N = x.shape[0]
        lam = 400.0
        a = np.ones(3)
        b = np.zeros(3)
        for ch in range(3):
            xc, yc = x[:, ch], y[:, ch]
            sxx = (xc * xc).sum() + lam * N
            sx = xc.sum()
            sxy = (xc * yc).sum() + lam * N
            sy = yc.sum()
            det = sxx * N - sx * sx
            if abs(det) < 1e-6:
                a[ch], b[ch] = 1.0, (sy - sx) / N
            else:
                a[ch] = (sxy * N - sx * sy) / det
                b[ch] = (sxx * sy - sx * sxy) / det
            a[ch] = float(np.clip(a[ch], 0.3, 3.0))
        out = ((P - b) / a).astype(np.float32)
        B = x.mean(axis=0)
        dev = float(np.abs(a * B + b - B).max())
        return out, a, b, dev

    def _knn(self, raw, diff):
        if not self.examples:
            return None
        v = cell_vector(raw, diff)
        best, lab = 1e9, None
        for e, label in self.examples:
            d = float(np.abs(e - v).mean())
            if d < best:
                best, lab = d, label
        return lab if best < 0.07 else None

    def _classify_mark(self, raw, norm, base, chg):
        lab = self._knn(raw, norm - base)
        if lab is not None:
            return lab
        warm = warm_mask(raw)
        nchg = int(chg.sum())
        warm_frac = float((warm & chg).sum()) / max(nchg, 1)
        shift = (raw - base).reshape(-1, 3).mean(axis=0)
        red_tint = shift[0] - 0.5 * (shift[1] + shift[2])
        if warm_frac >= 0.3 or (red_tint >= 25 and warm.mean() > 0.2):
            return HIT
        return MISS

    def _hit_or_sunk(self, P, r, c):
        """«Красная» отметка похожа на виденные «убил» больше, чем на «ранил» → это «убил»."""
        if not self.proto_sunk:
            return HIT
        v = downsample(P[r, c], 8)
        ds = min(float(np.abs(p - v).mean()) for p in self.proto_sunk)
        dh = min((float(np.abs(p - v).mean()) for p in self.proto_hit), default=1e9)
        if ds < 22.0 and ds < 0.75 * dh:
            return SUNK
        return HIT

    def _remember_sunk_look(self, hit_look, sunk_look):
        self.proto_hit.append(downsample(hit_look, 8))
        self.proto_sunk.append(downsample(sunk_look, 8))
        self.proto_hit = self.proto_hit[-20:]
        self.proto_sunk = self.proto_sunk[-20:]

    def add_example(self, raw_patch, label: int, ref_patch=None):
        ref = ref_patch if ref_patch is not None else (self.bg if self.bg is not None else raw_patch)
        self.examples.append((cell_vector(raw_patch, raw_patch - ref), int(label)))
        if len(self.examples) > 160:
            self.examples = self.examples[-160:]

    def learn_from_correction(self, r: int, c: int, label: int):
        """Пользователь поправил клетку: запоминаем, как выглядит такая отметка на этом сайте."""
        if self.prev is None:
            return
        raw = self.prev[r, c]
        ref = self.base[r, c] if self.has_base[r, c] else self.bg
        self.add_example(raw, label, ref)
        self.states[r, c] = label
        self.pending[r, c] = -1
        self.pending_cnt[r, c] = 0
        if label == UNKNOWN:
            self.base[r, c] = raw
            self.has_base[r, c] = True
        else:
            self.mark_base[r, c] = raw
            self.mark_age[r, c] = 0

    def export_examples(self):
        return [{"label": lab, "vec": encode_vec(v)} for v, lab in self.examples]

    def import_examples(self, data):
        self.examples = []
        for d in data or []:
            try:
                self.examples.append((decode_vec(d["vec"]), int(d["label"])))
            except Exception:
                pass

    # ---------------- основной шаг
    def _suppress_line_highlight(self, cand, P, cursor):
        """Подсветка строки/столбца под мышью: много клеток линии сменились одинаково → не отметки."""
        cr, cc = cursor
        n = self.n
        line = np.zeros((n, n), bool)
        line[cr, :] = True
        line[:, cc] = True
        cells = list(zip(*np.nonzero(cand & line)))
        uniform = []
        for r, c in cells:
            d = P[r, c] - self.base[r, c]
            sh = d.reshape(-1, 3).mean(axis=0)
            resid = np.abs(d - sh).max(axis=-1)
            if (resid > self.t_pix).mean() < self.f_min:
                uniform.append(((r, c), sh))
        if len(uniform) >= 3:
            med = np.median(np.array([sh for _, sh in uniform]), axis=0)
            for (r, c), sh in uniform:
                if np.abs(sh - med).max() < 20:
                    cand[r, c] = False

    def process(self, patches: np.ndarray, cursor=None) -> TrackResult:
        n = self.n
        self.frames += 1
        if self.base is None:
            self._init_from(patches)
            self.prev = patches
            return TrackResult(self.states.copy(), True, "init", [])

        unk = (self.states == UNKNOWN) & self.has_base
        marked_now = self.states != UNKNOWN
        ref = np.where(marked_now[..., None, None, None], self.mark_base, self.base)
        use = unk | marked_now
        P, a, b, dev = self._normalize(patches, ref, use)
        if dev < 6.0:                # освещение не менялось — сравниваем как есть
            P, a, b, dev = patches, np.ones(3), np.zeros(3), 0.0
        lit = dev > 10.0             # поле затемнено/подсвечено целиком (например, чужой ход)
        diff = np.abs(P - self.base).max(axis=-1)
        chg = diff > self.t_pix
        chg_frac = chg.mean(axis=(2, 3))
        cand = unk & (chg_frac >= self.f_min)
        if cursor is not None:
            self._suppress_line_highlight(cand, P, cursor)

        n_unk = int(unk.sum())
        n_cand = int(cand.sum())
        if n_cand > max(20, 0.35 * n_unk):
            # Разом поменялось слишком много: перекрыли окно, листнули страницу, новая партия.
            self.massive += 1
            if self.massive >= 3:
                self.massive = 0
                old = self.states.copy()
                self._init_from(patches)
                self.prev = patches
                return TrackResult(self.states.copy(), not np.array_equal(old, self.states),
                                   "reinit", [])
            self.prev = patches
            return TrackResult(self.states.copy(), False, "unstable", [])
        self.massive = 0

        stable = None
        if self.prev is not None:
            stable = np.abs(patches - self.prev).max(axis=-1).mean(axis=(2, 3)) < 6.0

        changed = False
        new_marks = []
        seen = np.zeros((n, n), bool)
        for r, c in zip(*np.nonzero(cand)):
            seen[r, c] = True
            cls = self._classify_mark(patches[r, c], P[r, c], self.base[r, c], chg[r, c])
            if cls == UNKNOWN:
                continue
            if cls == HIT:
                cls = self._hit_or_sunk(P, r, c)
            if self.pending[r, c] == cls:
                self.pending_cnt[r, c] += 1
            else:
                self.pending[r, c] = cls
                self.pending_cnt[r, c] = 1
            under_cursor = cursor is not None and tuple(cursor) == (r, c)
            if self.pending_cnt[r, c] >= 2 and not under_cursor:
                self.states[r, c] = cls
                self.mark_base[r, c] = P[r, c]
                self.mark_age[r, c] = 0
                self.pending[r, c] = -1
                self.pending_cnt[r, c] = 0
                new_marks.append((int(r), int(c), int(cls)))
                changed = True

        # отметка «ранил» сменила вид и устоялась → «убил»
        hits = self.states == HIT
        if lit:
            self.mark_age[hits] = 0          # при необычном освещении вид ненадёжен
        young = hits & (self.mark_age < 3)
        self.mark_base[young] = P[young]     # пока анимация/освещение не устоялись — запоминаем вид
        self.mark_age[hits] += 1
        if self.detect_sunk_look and stable is not None and not lit:
            for r, c in zip(*np.nonzero(hits & ~young)):
                seen[r, c] = True
                d2 = np.abs(P[r, c] - self.mark_base[r, c]).max(axis=-1) > self.t_pix
                if d2.mean() >= 0.2 and stable[r, c]:
                    lab = self._knn(patches[r, c], P[r, c] - self.base[r, c])
                    if lab == HIT:
                        continue
                    if self.pending[r, c] == SUNK:
                        self.pending_cnt[r, c] += 1
                    else:
                        self.pending[r, c] = SUNK
                        self.pending_cnt[r, c] = 1
                    if self.pending_cnt[r, c] >= 2:
                        self._remember_sunk_look(self.mark_base[r, c], P[r, c])
                        self.states[r, c] = SUNK
                        self.pending[r, c] = -1
                        self.pending_cnt[r, c] = 0
                        new_marks.append((int(r), int(c), SUNK))
                        changed = True
            # остальные «ранил», которые выглядят как «убил» (последняя палуба, однопалубники)
            if self.proto_sunk:
                for r, c in zip(*np.nonzero((self.states == HIT) & ~young)):
                    if stable[r, c] and self._hit_or_sunk(P, r, c) == SUNK:
                        self.states[r, c] = SUNK
                        new_marks.append((int(r), int(c), SUNK))
                        changed = True

        self.pending[~seen] = -1
        self.pending_cnt[~seen] = 0

        # отметки исчезли почти везде → началась новая партия
        marked = self.states != UNKNOWN
        n_marked = int(marked.sum())
        if n_marked >= 3 and not lit:
            with_base = marked & self.has_base
            reverted = with_base & (chg_frac < self.f_min * 0.5)
            no_base = marked & ~self.has_base
            reverted_nb = 0
            if no_base.any():
                st_now = static_classify(patches, self.bg, self.sensitivity)
                reverted_nb = int((no_base & (st_now == UNKNOWN)).sum())
            frac = (int(reverted.sum()) + reverted_nb) / n_marked
            self.revert_frames = self.revert_frames + 1 if frac >= 0.6 else 0
            if self.revert_frames >= 2:
                self.revert_frames = 0
                self.new_game()
                self._init_from(patches)
                self.prev = patches
                return TrackResult(self.states.copy(), True, "newgame", [])

        # освещение сменилось надолго (поле затемнено и стоит) → принимаем новый вид за норму
        if lit and not cand.any():
            self.lit_frames += 1
        else:
            self.lit_frames = 0
        if self.lit_frames >= 6:
            self.lit_frames = 0
            mb = marked & self.has_base
            self.base[mb] = self.base[mb] * a + b
            self.base[unk] = patches[unk]
            self.mark_base[marked] = patches[marked]
            self.mark_age[hits] = 3
            if self.bg is not None:
                self.bg = (self.bg * a + b).astype(np.float32)
        elif not lit:
            # медленно подстраиваем «пустой» вид клеток (сглаживание шрифтов и т.п.)
            calm = unk & (chg_frac < self.f_min * 0.3)
            if calm.any():
                self.base[calm] = 0.9 * self.base[calm] + 0.1 * P[calm]
        self.prev = patches
        return TrackResult(self.states.copy(), changed, "lit" if lit else "ok", new_marks)
