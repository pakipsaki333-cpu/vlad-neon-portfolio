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

from .rules import HIT, MISS, SUNK, UNKNOWN

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
    return a[..., :3].astype(np.float32)


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


def find_grids(img, n: int = 10, min_cell: int = 12, max_cell: int = 140):
    """Найти все сетки n×n на изображении. → список GridGeom, слева направо."""
    eh, ev = _edge_maps(img)
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
                if not any(abs(g.x0 - f.x0) < sx * 0.5 and abs(g.y0 - f.y0) < sy * 0.5 for f in found):
                    found.append(g)
    found.sort(key=lambda g: (g.x0, g.y0))
    return found


def _profile(edge_abs: np.ndarray, axis: int, sigma: float = 1.0):
    """Профиль «линейности» вдоль оси; сглаживание сводит два края зазора/линии в один пик."""
    p = np.minimum(edge_abs, 120.0).mean(axis=axis)
    rad = max(1, int(np.ceil(sigma * 3)))
    x = np.arange(-rad, rad + 1)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    return np.convolve(p, k / k.sum(), mode="same")


def _fit_lattice(profile: np.ndarray, start0: float, step0: float, n: int, spread=0.15):
    steps = np.arange(step0 * (1 - spread), step0 * (1 + spread), 0.1)
    offs = np.arange(start0 - step0, start0 + step0, 0.25)
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


def refine_grid(img, approx: GridGeom):
    """Подогнать примерную рамку к настоящим линиям сетки. → (GridGeom, уверенность 0..1)."""
    a = _f32(img)
    H, W = a.shape[:2]
    n = approx.n
    g = approx
    conf = 0.0
    for _ in range(2):
        y_lo = int(max(0, g.y0 + 0.5 * g.ch))
        y_hi = int(min(H, g.y0 + g.height - 0.5 * g.ch))
        x_lo = int(max(0, g.x0 + 0.5 * g.cw))
        x_hi = int(min(W, g.x0 + g.width - 0.5 * g.cw))
        if y_hi - y_lo < 4 or x_hi - x_lo < 4:
            return approx, 0.0
        ev = np.abs(np.diff(a[y_lo:y_hi], axis=1)).max(axis=-1)
        eh = np.abs(np.diff(a[:, x_lo:x_hi], axis=0)).max(axis=-1)
        sig = max(2.0, 0.08 * min(g.cw, g.ch))
        pv = _profile(ev, 0, sig)
        ph = _profile(eh, 1, sig)
        sx, x0, cw = _fit_lattice(pv, g.x0 - 0.5, g.cw, n)
        sy, y0, ch = _fit_lattice(ph, g.y0 - 0.5, g.ch, n)
        g = GridGeom(x0 + 0.5, y0 + 0.5, cw, ch, n)
        base_v = np.median(pv) + 1e-3
        base_h = np.median(ph) + 1e-3
        cx = (sx / (n + 1)) / base_v
        cy = (sy / (n + 1)) / base_h
        conf = float(np.clip((min(cx, cy) - 1.3) / 2.5, 0.0, 1.0))
    if conf < 0.15:
        return approx, conf
    return g, conf


def border_signature(img, geom: GridGeom, samples: int = 24) -> np.ndarray:
    """Яркость вдоль линий сетки — по ней понимаем, что поле не сдвинулось."""
    a = _f32(img).mean(axis=-1)
    H, W = a.shape
    n = geom.n
    t = (np.arange(samples) + 0.5) / samples
    xs, ys = [], []
    for k in range(n + 1):
        x = geom.x0 + k * geom.cw
        xs.append(np.full(samples, x))
        ys.append(geom.y0 + t * geom.height)
        y = geom.y0 + k * geom.ch
        xs.append(geom.x0 + t * geom.width)
        ys.append(np.full(samples, y))
    xs = np.clip(np.round(np.concatenate(xs)).astype(int), 0, W - 1)
    ys = np.clip(np.round(np.concatenate(ys)).astype(int), 0, H - 1)
    return a[ys, xs]


def signature_match(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    da, db = a - a.mean(), b - b.mean()
    sa, sb = np.sqrt((da * da).sum()), np.sqrt((db * db).sum())
    if sa < 1e-3 or sb < 1e-3:
        # ровная заливка без линий: сравниваем просто яркость
        return float(np.abs(a.mean() - b.mean()) < 25)
    return float((da * db).sum() / (sa * sb))


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
