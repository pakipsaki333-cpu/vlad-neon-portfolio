"""Распознавание: поиск сетки, слежение за полем на синтетических «сайтах» разных стилей."""
import random

import numpy as np
import pytest

from seabattle.game import Referee
from seabattle.rules import HIT, MISS, SUNK, UNKNOWN, Rules
from seabattle.solver import random_layout
from seabattle.vision import (BoardTracker, GridGeom, border_signature, decode_vec, encode_vec,
                              extract_patches, find_grids, refine_grid, signature_match,
                              static_classify)
from synth import STYLES, make_screen


def random_marks(seed, k=25):
    rng = random.Random(seed)
    st = np.zeros((10, 10), np.int8)
    for _ in range(k):
        st[rng.randrange(10), rng.randrange(10)] = rng.choice([MISS, MISS, HIT, SUNK])
    return st


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("cell", [20, 33])
def test_find_both_boards(style, cell):
    st = random_marks(1)
    off = (cell * 20, cell * 4)
    im = make_screen(st, style=style, cell=cell, size=(cell * 40, cell * 20), offset=off,
                     my_ships={(1, 1), (1, 2), (5, 5)})
    grids = find_grids(im)
    assert len(grids) == 2, grids
    enemy = grids[-1]                                 # правое поле
    assert abs(enemy.x0 - off[0]) < 1.5 and abs(enemy.y0 - off[1]) < 1.5
    assert abs(enemy.cw - cell) < 0.3 and abs(enemy.ch - cell) < 0.3


@pytest.mark.parametrize("style", STYLES)
def test_refine_rough_selection(style):
    cell = 30
    im = make_screen(random_marks(2), style=style, cell=cell, size=(1200, 640), offset=(640, 160))
    rough = GridGeom(640 + 10, 160 - 8, cell * 1.07, cell * 0.95)
    g, conf = refine_grid(im, rough)
    assert conf > 0.3
    assert abs(g.x0 - 640) < 1.5 and abs(g.y0 - 160) < 1.5
    assert abs(g.cw - cell) < 0.3 and abs(g.ch - cell) < 0.3


def play_tracked(style, seed, shots=40, cell=26, auto_mark=True, dim=True):
    rng = random.Random(seed)
    rules = Rules()
    ref = Referee(random_layout(rules, rng), rules, auto_mark=auto_mark)
    off = (cell * 18, cell * 3)
    size = (cell * 30, cell * 15)
    geom = GridGeom(off[0], off[1], cell, cell)

    def frame(**kw):
        return make_screen(ref.states, style=style, cell=cell, size=size, offset=off,
                           clutter=False, seed=seed, **kw)

    tr = BoardTracker()
    tr.process(extract_patches(frame(), geom))
    order = [(r, c) for r in range(10) for c in range(10)]
    rng.shuffle(order)
    for rc in order:
        if ref.shots >= shots or ref.done:
            break
        if ref.states[rc] != UNKNOWN:
            continue
        res = ref.shoot(*rc)
        seq = [dict(hover=rc), dict()]
        if res == MISS and dim:
            seq += [dict(dim=0.45)] * 3 + [dict()]
        for kw in seq:
            tr.process(extract_patches(frame(**kw), geom), cursor=kw.get("hover"))
    tr.process(extract_patches(frame(), geom))
    return ref, tr


@pytest.mark.parametrize("style", STYLES)
def test_tracker_follows_game(style):
    ref, tr = play_tracked(style, seed=11, shots=45)
    truth, got = ref.states, tr.states
    bad = (truth != got) & ~((truth == SUNK) & (got == HIT))
    assert not bad.any(), np.argwhere(bad)
    assert (got == MISS).sum() == (truth == MISS).sum()


def test_tracker_learns_sunk_look():
    ref, tr = play_tracked("notebook", seed=1, shots=100, auto_mark=False)
    sunk_truth = ref.states == SUNK
    assert sunk_truth.sum() >= 10
    assert (tr.states[sunk_truth] == SUNK).mean() > 0.8


@pytest.mark.parametrize("style", ["notebook", "dark", "flat"])
def test_hover_and_dimming_do_not_create_marks(style):
    st = random_marks(5, k=15)
    geom = GridGeom(400, 100, 30, 30)
    kw = dict(style=style, cell=30, size=(900, 500), offset=(400, 100), clutter=False)
    tr = BoardTracker()
    tr.process(extract_patches(make_screen(st, **kw), geom))
    init = tr.states.copy()
    rng = random.Random(3)
    for i in range(30):
        hover = (rng.randrange(10), rng.randrange(10))
        dim = 0.45 if (i // 5) % 2 else 0.0
        tr.process(extract_patches(make_screen(st, hover=hover, dim=dim, **kw), geom), cursor=hover)
    assert np.array_equal(tr.states, init)


@pytest.mark.parametrize("style", ["notebook", "dark", "flat"])
def test_static_classify_midgame(style):
    st = random_marks(7, k=30)
    geom = GridGeom(400, 100, 30, 30)
    im = make_screen(st, style=style, cell=30, size=(900, 500), offset=(400, 100), clutter=False)
    got = static_classify(extract_patches(im, geom))
    exp = st.copy()
    exp[exp == SUNK] = HIT
    assert (got == exp).mean() >= 0.97


def test_new_game_is_detected():
    geom = GridGeom(400, 100, 30, 30)
    kw = dict(style="notebook", cell=30, size=(900, 500), offset=(400, 100), clutter=False)
    empty = np.zeros((10, 10), np.int8)
    tr = BoardTracker()
    tr.process(extract_patches(make_screen(empty, **kw), geom))
    st = random_marks(9, k=20)
    for _ in range(3):
        tr.process(extract_patches(make_screen(st, **kw), geom))
    assert (tr.states != UNKNOWN).sum() >= 10
    statuses = [tr.process(extract_patches(make_screen(empty, **kw), geom)).status for _ in range(3)]
    assert "newgame" in statuses
    assert (tr.states == UNKNOWN).all()


def test_learning_from_user_correction():
    """Сайт рисует промахи красными точками — после одного исправления помощник понимает."""
    from PIL import Image, ImageDraw
    geom = GridGeom(100, 100, 30, 30)

    def screen(misses):
        img = Image.new("RGB", (500, 450), (255, 255, 255))
        d = ImageDraw.Draw(img)
        for k in range(11):
            d.line([100 + k * 30, 100, 100 + k * 30, 400], fill=(150, 150, 220))
            d.line([100, 100 + k * 30, 400, 100 + k * 30], fill=(150, 150, 220))
        for r, c in misses:
            x, y = 100 + c * 30 + 15, 100 + r * 30 + 15
            d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(220, 40, 40))
        return np.asarray(img)

    tr = BoardTracker()
    tr.process(extract_patches(screen([]), geom))
    marks = [(2, 2)]
    for _ in range(2):
        tr.process(extract_patches(screen(marks), geom))
    assert tr.states[2, 2] == HIT                       # без обучения — похоже на попадание
    tr.learn_from_correction(2, 2, MISS)
    marks.append((6, 7))
    for _ in range(2):
        tr.process(extract_patches(screen(marks), geom))
    assert tr.states[6, 7] == MISS
    data = tr.export_examples()
    tr2 = BoardTracker()
    tr2.import_examples(data)
    assert len(tr2.examples) == 1


def test_vec_roundtrip():
    v = np.linspace(-1, 1, 384).astype(np.float32)
    assert np.abs(decode_vec(encode_vec(v)) - v).max() < 0.01


def test_border_signature_detects_moved_board():
    st = random_marks(4)
    geom = GridGeom(400, 100, 30, 30)
    kw = dict(style="notebook", cell=30, size=(900, 520), clutter=True)
    a = make_screen(st, offset=(400, 100), **kw)
    b = make_screen(st, offset=(400, 100), dim=0.4, **kw)
    c = make_screen(st, offset=(400, 160), **kw)
    sig = border_signature(a, geom)
    assert signature_match(sig, border_signature(b, geom)) > 0.8
    assert signature_match(sig, border_signature(c, geom)) < 0.6
