"""«Судья» для симуляций и тестов: знает настоящую расстановку и отвечает «мимо/ранил/убил»."""
from __future__ import annotations

import numpy as np

from .board import ring_cells
from .rules import HIT, MISS, SUNK, UNKNOWN, Rules


class Referee:
    def __init__(self, ships, rules: Rules, auto_mark: bool = True):
        self.rules = rules
        self.n = rules.size
        self.ships = [list(map(tuple, s)) for s in ships]
        self.owner = {}
        for i, s in enumerate(self.ships):
            for cell in s:
                self.owner[cell] = i
        self.auto_mark = auto_mark
        self.states = np.zeros((self.n, self.n), dtype=np.int8)
        self.shots = 0
        self.sunk = [False] * len(self.ships)

    def shoot(self, r: int, c: int) -> int:
        if self.states[r, c] != UNKNOWN:
            raise ValueError("сюда уже стреляли")
        self.shots += 1
        i = self.owner.get((r, c))
        if i is None:
            self.states[r, c] = MISS
            return MISS
        self.states[r, c] = HIT
        if all(self.states[y, x] != UNKNOWN for y, x in self.ships[i]):
            self.sunk[i] = True
            for y, x in self.ships[i]:
                self.states[y, x] = SUNK
            if self.auto_mark:
                for y, x in ring_cells(self.n, self.ships[i], self.rules.touch):
                    if self.states[y, x] == UNKNOWN:
                        self.states[y, x] = MISS
            return SUNK
        return HIT

    @property
    def done(self) -> bool:
        return all(self.sunk)

    def visible(self) -> np.ndarray:
        """Что видит стреляющий (если сайт не различает «убит», SUNK показывается как HIT)."""
        st = self.states.copy()
        if not self.rules.sunk_marked:
            st[st == SUNK] = HIT
        return st
