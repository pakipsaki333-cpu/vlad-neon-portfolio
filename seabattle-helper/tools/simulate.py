"""Бенчмарк: сколько выстрелов нужно, чтобы потопить весь флот, при разных стратегиях.

    python tools/simulate.py --games 40 --budget 0.15

Стратегии:
  random       — случайная клетка (так играет новичок без системы);
  hunt-target  — «шахматка» + добивание соседних клеток (так играет опытный человек);
  helper       — наш вероятностный решатель (то, что подсказывает помощник).
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from seabattle.board import prepare  # noqa: E402
from seabattle.game import Referee  # noqa: E402
from seabattle.rules import HIT, UNKNOWN, Rules  # noqa: E402
from seabattle.solver import analyze, random_layout  # noqa: E402


def pick_random(st, rules, rng):
    cells = [(r, c) for r in range(rules.size) for c in range(rules.size) if st[r, c] == UNKNOWN]
    return rng.choice(cells)


def pick_hunt_target(st, rules, rng):
    p = prepare(st, rules)
    n = rules.size
    free = lambda r, c: 0 <= r < n and 0 <= c < n and st[r, c] == UNKNOWN and not p.forbidden[r, c]
    for comp in p.hit_groups:                      # добиваем раненого
        if len(comp) == 1:
            (r, c), = comp
            opts = [(r + dr, c + dc) for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1))]
        elif comp[0][0] == comp[-1][0]:
            r = comp[0][0]
            opts = [(r, comp[0][1] - 1), (r, comp[-1][1] + 1)]
        else:
            c = comp[0][1]
            opts = [(comp[0][0] - 1, c), (comp[-1][0] + 1, c)]
        opts = [o for o in opts if free(*o)]
        if opts:
            return rng.choice(opts)
    big = max(p.remaining) if p.remaining else 1
    par = rng.randrange(big) if big > 1 else 0
    cand = [(r, c) for r in range(n) for c in range(n) if free(r, c) and (r + c) % big == par % big]
    if not cand:
        cand = [(r, c) for r in range(n) for c in range(n) if free(r, c)]
    if not cand:
        cand = [(r, c) for r in range(n) for c in range(n) if st[r, c] == UNKNOWN]
    return rng.choice(cand)


def play(strategy, rules, rng, budget):
    ref = Referee(random_layout(rules, rng), rules, auto_mark=True)
    while not ref.done:
        st = ref.visible()
        if strategy == "random":
            cell = pick_random(st, rules, rng)
        elif strategy == "hunt-target":
            cell = pick_hunt_target(st, rules, rng)
        else:
            cell = analyze(st, rules, budget=budget).best
        ref.shoot(*cell)
    return ref.shots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=30)
    ap.add_argument("--budget", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--strategies", default="random,hunt-target,helper")
    args = ap.parse_args()
    rules = Rules()
    print(f"Классика 10×10, флот {rules.fleet}, {args.games} партий на стратегию\n")
    results = {}
    for strat in args.strategies.split(","):
        rng = random.Random(args.seed)
        t = time.time()
        shots = [play(strat, rules, rng, args.budget) for _ in range(args.games)]
        results[strat] = shots
        print(f"{strat:12s} среднее {statistics.mean(shots):5.1f}  медиана {statistics.median(shots):5.1f}"
              f"  лучшая {min(shots):3d}  худшая {max(shots):3d}   ({time.time() - t:.0f} с)", flush=True)
    if "helper" in results and "hunt-target" in results:
        # Шанс выиграть дуэль «помощник против опытного человека», если ходят по очереди
        # (упрощение: учитываем только число выстрелов до полной победы; кто первый — монетка).
        h, o = results["helper"], results["hunt-target"]
        wins = sum((a < b) + 0.5 * (a == b) for a in h for b in o) / (len(h) * len(o))
        print(f"\nПомощник обгоняет «шахматку с добиванием» в {wins * 100:.0f}% пар партий")


if __name__ == "__main__":
    main()
