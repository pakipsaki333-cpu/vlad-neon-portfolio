"""Распознавание на настоящих скриншотах сайтов (а не только на синтетике).

data/yandex_midgame.jpg — «Морской бой классический» на Яндекс Играх, середина партии
(присланный скриншот, обрезан до игрового поля). Особенность сайта: тетрадный лист —
линии клеток идут по всей странице, поле выделено только жирной рамкой; сверху цифры,
сбоку буквы. Раньше помощник на таком фоне съезжал на клетку влево.
"""
import os

import numpy as np
import pytest
from PIL import Image

from seabattle.rules import HIT, MISS, UNKNOWN
from seabattle.vision import (NUMBERS_TOP, GridGeom, border_signature, detect_label_layout, extract_patches,
                              find_grids, refine_grid, signature_match, static_classify)

DATA = os.path.join(os.path.dirname(__file__), "data")


@pytest.fixture(scope="module")
def yandex():
    return np.asarray(Image.open(os.path.join(DATA, "yandex_midgame.jpg")).convert("RGB"))


# левый верхний угол клеток и шаг (замерено по линиям тетрадного листа)
OWN = (148.0, 50.0, 49.8)
ENEMY = (698.0, 50.0, 49.8)


def _close(g, ref, tol=2.5):
    x0, y0, cell = ref
    return abs(g.x0 - x0) < tol and abs(g.y0 - y0) < tol and abs(g.cw - cell) < 0.5 and abs(g.ch - cell) < 0.5


def test_yandex_both_boards_found(yandex):
    grids = find_grids(yandex)
    assert len(grids) == 2, grids
    assert _close(grids[0], OWN), grids[0]
    assert _close(grids[1], ENEMY), grids[1]


@pytest.mark.parametrize("dx0,dy0,dx1,dy1", [(0, 0, 0, 0), (-20, -15, 20, 15), (18, 12, -18, -12),
                                             (-22, 10, -20, 10), (21, -14, 22, 14), (0, 15, -20, 0)])
def test_yandex_rough_selection_does_not_slip_a_cell(yandex, dx0, dy0, dx1, dy1):
    """Рамку выделили мышкой неточно — сетка должна встать ровно на поле, а не на клетку левее."""
    x0, y0, cell = ENEMY
    rough = GridGeom(x0 + dx0, y0 + dy0, (cell * 10 + dx1 - dx0) / 10, (cell * 10 + dy1 - dy0) / 10)
    g, conf = refine_grid(yandex, rough)
    assert conf > 0.3
    assert _close(g, ENEMY), g


def test_yandex_marks_recognized(yandex):
    st = static_classify(extract_patches(yandex, GridGeom(*ENEMY[:2], ENEMY[2], ENEMY[2])))
    exp = np.full((10, 10), UNKNOWN, np.int8)
    for rc in [(0, 3), (0, 6), (5, 8), (6, 0), (6, 1), (6, 2), (7, 0), (7, 2), (8, 0), (8, 2),
               (9, 0), (9, 2), (9, 6)]:
        exp[rc] = MISS                      # кляксы
    for rc in [(7, 1), (8, 1), (9, 1)]:
        exp[rc] = HIT                       # кресты на убитом трёхпалубнике
    assert np.array_equal(st, exp), np.argwhere(st != exp)


def test_yandex_paper_page_recognized(yandex):
    """По тетрадному листу вокруг поля и цифрам сверху помощник узнаёт игру и её флот."""
    from seabattle.vision import paper_lattice
    own, enemy = GridGeom(OWN[0], OWN[1], OWN[2], OWN[2]), GridGeom(ENEMY[0], ENEMY[1], ENEMY[2], ENEMY[2])
    assert paper_lattice(yandex, enemy, [own])
    assert paper_lattice(yandex, own, [enemy])


def test_yandex_labels_numbers_on_top(yandex):
    for ref in (OWN, ENEMY):
        g = GridGeom(ref[0], ref[1], ref[2], ref[2])
        assert detect_label_layout(yandex, g) == NUMBERS_TOP


# ---------------------------------------------------------------------------- партия на настоящих отметках
def _cell_box(r, c, ref=ENEMY):
    x0, y0, cw = ref
    return (int(round(x0 + c * cw)), int(round(y0 + r * cw)),
            int(round(x0 + (c + 1) * cw)), int(round(y0 + (r + 1) * cw)))


def _get(img, r, c):
    x0, y0, x1, y1 = _cell_box(r, c)
    return img[y0:y1, x0:x1].copy()


def _put(img, r, c, patch):
    x0, y0, x1, y1 = _cell_box(r, c)
    h, w = y1 - y0, x1 - x0
    p = patch[:h, :w]
    if p.shape[0] < h or p.shape[1] < w:
        p = np.pad(p, ((0, h - p.shape[0]), (0, w - p.shape[1]), (0, 0)), mode="edge")
    img[y0:y1, x0:x1] = p


@pytest.mark.parametrize("seed", [0, 4])
def test_yandex_tracker_follows_game_with_real_marks(yandex, seed):
    """Партия, собранная из настоящих отметок Яндекса: кляксы-промахи, красные кресты,
    прорисованный убитый корабль; флот как на сайте — с пятипалубным. Помощник должен всё время
    верно видеть «мимо»/«попал», заметить, что сайт сам обводит потопленные корабли точками,
    а в конце решатель — понимать, что флот потоплен."""
    import random

    from seabattle.fleetcheck import HaloWatch
    from seabattle.game import Referee
    from seabattle.rules import PRESETS, SUNK
    from seabattle.solver import analyze, random_layout
    from seabattle.vision import BoardTracker

    misses = [(0, 3), (0, 6), (5, 8), (6, 0), (6, 1), (6, 2), (7, 0), (7, 2), (8, 0), (8, 2), (9, 0), (9, 2), (9, 6)]
    sunk = [(7, 1), (8, 1), (9, 1)]
    blots = [_get(yandex, *rc) for rc in misses]
    s_top, s_mid, s_bot = [_get(yandex, *rc) for rc in sunk]
    clean = yandex.copy()
    for r, c in misses + sunk:
        _put(clean, r, c, _get(yandex, 1, c))           # в строке Б поле соперника пустое
    red = (s_mid[..., 0].astype(int) > 140) & (s_mid[..., 0].astype(int) - s_mid[..., 2].astype(int) > 60)

    rng = random.Random(seed)
    rules = PRESETS["yandex"][1]
    ref = Referee(random_layout(rules, rng), rules, auto_mark=True)
    halo = HaloWatch()
    ship_of = {rc: sorted(sh) for sh in ref.ships for rc in sh}
    blot_of = {(r, c): blots[rng.randrange(len(blots))] for r in range(10) for c in range(10)}

    def render():
        img = clean.copy()
        for r in range(10):
            for c in range(10):
                s = ref.states[r, c]
                if s == MISS:
                    _put(img, r, c, blot_of[(r, c)])
                elif s == HIT:                       # крест на пустой клетке
                    cell = _get(clean, r, c)
                    h, w = min(cell.shape[0], red.shape[0]), min(cell.shape[1], red.shape[1])
                    m = red[:h, :w]
                    cell[:h, :w][m] = s_mid[:h, :w][m]
                    _put(img, r, c, cell)
                elif s == SUNK:                      # корабль прорисован, кресты поверх
                    sh = ship_of[(r, c)]
                    i = sh.index((r, c))
                    spr = s_mid if len(sh) == 1 else (s_top if i == 0 else s_bot if i == len(sh) - 1 else s_mid)
                    if len(sh) > 1 and sh[0][0] == sh[1][0]:
                        spr = np.ascontiguousarray(np.rot90(spr))
                    _put(img, r, c, spr)
        return img

    geom = GridGeom(ENEMY[0], ENEMY[1], ENEMY[2], ENEMY[2])
    tr = BoardTracker()
    first = render()
    tr.process(extract_patches(first, geom))
    sig = border_signature(first, geom)
    order = [(r, c) for r in range(10) for c in range(10)]
    rng.shuffle(order)
    for rc in order:
        if ref.done:
            break
        if ref.states[rc] != UNKNOWN:
            continue
        ref.shoot(*rc)
        frame = render()
        # как в главном окне: поле «на месте», хотя кляксы и кресты залезают на линии
        cur = border_signature(frame, geom)
        match = signature_match(sig, cur)
        assert match >= 0.55, (rc, ref.shots, match)
        if match > 0.8:
            sig = cur
        for k in range(3):
            res = tr.process(extract_patches(frame, geom))
            halo.feed(ref.shots * 2.0 + k * 0.3, res.new_marks, res.states)
        loose_truth = np.where(ref.states == SUNK, HIT, ref.states)
        loose_got = np.where(tr.states == SUNK, HIT, tr.states)
        assert np.array_equal(loose_truth, loose_got), (rc, np.argwhere(loose_truth != loose_got))
    assert ref.done
    assert halo.seen                                     # точки вокруг потопленных ставит сайт
    a = analyze(tr.states, rules, budget=0.1)
    assert a.mode == "done" and not a.remaining


def test_yandex_signature_sees_scroll_and_cover_but_not_dimming(yandex):
    geom = GridGeom(ENEMY[0], ENEMY[1], ENEMY[2], ENEMY[2])
    img = yandex.astype(np.float32)
    sig = border_signature(img, geom)
    covered = img.copy()
    covered[100:400, 750:1100] = (30, 30, 40)            # чужое окно поверх половины поля
    assert signature_match(sig, border_signature(img * 0.6 + 40, geom)) > 0.8
    assert signature_match(sig, border_signature(covered, geom)) < 0.55
    for dy, dx in ((12, 0), (5, 0), (0, 50), (0, -50), (-25, 0)):
        moved = np.roll(img, (dy, dx), axis=(0, 1))       # страницу прокрутили
        assert signature_match(sig, border_signature(moved, geom)) < 0.55, (dy, dx)
