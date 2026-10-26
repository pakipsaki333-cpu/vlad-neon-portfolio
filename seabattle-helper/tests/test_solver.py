"""Решатель: точность вероятностей (сравнение с полным перебором) и типовые ситуации."""
import itertools
import random
import time

import numpy as np
import pytest

from seabattle.board import prepare, validate_layout
from seabattle.game import Referee
from seabattle.rules import (HIT, MISS, SUNK, TOUCH_ANY, TOUCH_CORNERS, TOUCH_NONE,
                             UNKNOWN, Rules, coord_name)
from seabattle.solver import (Geometry, Problem, analyze, mask_from_bool, random_layout)


def exact_probs(states, rules):
    """Честный перебор всех расстановок, согласных с полем (только для маленьких полей)."""
    p = prepare(states, rules)
    assert not p.fatal, p.issues
    n = rules.size
    geo = Geometry.get(n, p.touch)
    prob = Problem(geo, p.remaining, mask_from_bool(p.forbidden | (p.states == SUNK)),
                   mask_from_bool(p.required), mask_from_bool(p.unknown), p.sunk_marked)
    lengths = prob.lengths
    counts = np.zeros(n * n)
    total = 0

    def rec(k, halo_acc, cells_acc, last_p):
        nonlocal total
        if k == len(lengths):
            if (cells_acc & prob.R) == prob.R:
                total += 1
                m = cells_acc
                while m:
                    b = m & -m
                    counts[b.bit_length() - 1] += 1
                    m ^= b
            return
        L = lengths[k]
        start = last_p + 1 if k > 0 and lengths[k - 1] == L else 0
        C, H = prob.C[L], prob.H[L]
        for pp in prob.valid[L]:
            if pp < start:
                continue
            if C[pp] & halo_acc:
                continue
            rec(k + 1, halo_acc | H[pp], cells_acc | C[pp], pp)

    rec(0, 0, 0, -1)
    assert total > 0
    pr = (counts / total).reshape(n, n)
    st = p.states
    pr[(st == HIT) | (st == SUNK)] = 1.0
    return pr, total


def random_position(rules, rng, n_shots):
    ships = random_layout(rules, rng)
    ref = Referee(ships, rules, auto_mark=rng.random() < 0.5)
    cells = [(r, c) for r in range(rules.size) for c in range(rules.size)]
    rng.shuffle(cells)
    for r, c in cells:
        if ref.shots >= n_shots or ref.done:
            break
        if ref.states[r, c] == UNKNOWN:
            ref.shoot(r, c)
    return ref


@pytest.mark.parametrize("touch", [TOUCH_NONE, TOUCH_CORNERS, TOUCH_ANY])
def test_probabilities_match_exact_enumeration(touch):
    rules = Rules(size=6, fleet=(3, 2, 2, 1), touch=touch)
    rng = random.Random(7)
    worst = 0.0
    checked = 0
    for trial in range(10):
        ref = random_position(rules, rng, n_shots=rng.randint(0, 12))
        if ref.done:
            continue
        st = ref.visible()
        exact, total = exact_probs(st, rules)
        a = analyze(st, rules, budget=0.5, seed=trial)
        assert a.method == "mcmc" and not a.relaxed, (a.relaxed, a.issues)
        mask = st == UNKNOWN
        err = np.abs(a.probs[mask] - exact[mask]).max() if mask.any() else 0
        worst = max(worst, err)
        checked += 1
    assert checked >= 6
    assert worst < 0.04, f"макс. отклонение от точного ответа {worst:.3f}"


def test_hit_neighbours_and_diagonals_classic():
    rules = Rules()
    st = np.zeros((10, 10), np.int8)
    st[4, 4] = HIT
    a = analyze(st, rules, budget=0.4)
    assert a.mode == "target"
    for dr, dc in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
        assert a.probs[4 + dr, 4 + dc] == 0.0      # по диагонали — пусто
        assert a.known_empty[4 + dr, 4 + dc]
    assert a.best in {(3, 4), (5, 4), (4, 3), (4, 5)}
    assert 1 not in a.focus_lengths                 # раненый — не однопалубный


def test_two_hits_shoot_along_the_line():
    rules = Rules()
    st = np.zeros((10, 10), np.int8)
    st[4, 4] = st[4, 5] = HIT
    a = analyze(st, rules, budget=0.4)
    assert a.best in {(4, 3), (4, 6)}
    assert a.probs[3, 4] == 0 and a.probs[5, 5] == 0   # борта линии пусты


def test_enclosed_hit_is_sunk_and_halo_empty():
    rules = Rules()
    st = np.zeros((10, 10), np.int8)
    st[0, 0] = HIT
    st[0, 1] = MISS
    st[1, 0] = MISS
    a = analyze(st, rules, budget=0.3)
    assert a.states[0, 0] == SUNK
    assert a.mode == "hunt"
    assert a.remaining.count(1) == 3
    assert a.probs[1, 1] == 0


def test_longest_group_is_sunk():
    rules = Rules()
    st = np.zeros((10, 10), np.int8)
    st[5, 2:6] = HIT                                    # 4 в ряд = линкор убит
    a = analyze(st, rules, budget=0.3)
    assert (a.states[5, 2:6] == SUNK).all()
    assert 4 not in a.remaining


def test_corners_rule_allows_diagonal():
    rules = Rules(touch=TOUCH_CORNERS)
    st = np.zeros((10, 10), np.int8)
    st[4, 4] = HIT
    a = analyze(st, rules, budget=0.4)
    assert a.probs[3, 3] > 0.02                         # по диагонали может стоять другой корабль


def test_inconsistent_board_does_not_crash():
    rules = Rules()
    st = np.zeros((10, 10), np.int8)
    st[2, 2] = st[3, 3] = HIT                           # диагональ при классике невозможна
    st[7, 0:6] = SUNK                                   # 6-палубного не бывает
    a = analyze(st, rules, budget=0.4)
    assert a.best is not None
    assert a.relaxed or a.issues


def test_done_when_everything_sunk():
    rules = Rules()
    rng = random.Random(3)
    ships = random_layout(rules, rng)
    ref = Referee(ships, rules)
    for s in ships:
        for r, c in s:
            ref.shoot(r, c)
    a = analyze(ref.visible(), rules, budget=0.2)
    assert a.mode == "done" and a.best is None


@pytest.mark.parametrize("touch", [TOUCH_NONE, TOUCH_CORNERS, TOUCH_ANY])
def test_random_layout_valid(touch):
    rules = Rules(touch=touch)
    rng = random.Random(11)
    for _ in range(20):
        ships = random_layout(rules, rng)
        assert validate_layout(ships, rules) == []


def test_speed_midgame():
    rules = Rules()
    rng = random.Random(5)
    ref = random_position(rules, rng, n_shots=30)
    t = time.perf_counter()
    a = analyze(ref.visible(), rules, budget=0.5)
    assert time.perf_counter() - t < 2.5
    assert a.samples > 300


def test_full_game_wins_quickly():
    """Решатель всегда доигрывает партию и делает это заметно быстрее случайной стрельбы."""
    rules = Rules()
    rng = random.Random(2024)
    shots = []
    for g in range(3):
        ref = Referee(random_layout(rules, rng), rules, auto_mark=bool(g % 2))
        while not ref.done:
            a = analyze(ref.visible(), rules, budget=0.08)
            assert a.best is not None
            ref.shoot(*a.best)
            assert ref.shots <= 100
        shots.append(ref.shots)
    assert sum(shots) / len(shots) < 70, shots
