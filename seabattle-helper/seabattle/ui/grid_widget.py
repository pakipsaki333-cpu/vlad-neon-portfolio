"""Виджет поля: тепловая карта и отметки (атака) или корабли (расстановка)."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..rules import HIT, MISS, SUNK, UNKNOWN, letters_for
from . import theme


class BoardView(QWidget):
    cellClicked = Signal(int, int)               # левый клик
    cellMenu = Signal(int, int, object)          # правый клик (глобальная позиция)

    def __init__(self, n: int = 10, parent=None):
        super().__init__(parent)
        self.n = n
        self.letters = "ru"
        self.mode = "attack"                     # attack | placement
        self.states = np.zeros((n, n), np.int8)
        self.inferred = None                     # состояния после логического вывода
        self.probs = None
        self.known_empty = None
        self.best = None
        self.top = []
        self.ships = []
        self.show_numbers = True
        self.hover = None
        self.interactive = True
        self._phase = 0.0
        self._pulse = QTimer(self)
        self._pulse.timeout.connect(self._tick)
        self._pulse.start(50)
        self.setMouseTracking(True)
        self.setMinimumSize(220, 220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------ данные
    def set_size(self, n: int):
        if n != self.n:
            self.n = n
            self.states = np.zeros((n, n), np.int8)
            self.inferred = self.probs = self.known_empty = None
            self.best, self.top, self.ships = None, [], []
            self.update()

    def set_attack(self, states, analysis=None):
        self.mode = "attack"
        self.states = np.array(states, np.int8)
        if analysis is not None:
            self.probs = analysis.probs
            self.known_empty = analysis.known_empty
            self.inferred = analysis.states
            self.best = analysis.best
            self.top = analysis.top
        else:
            self.probs = self.known_empty = self.inferred = None
            self.best, self.top = None, []
        self.update()

    def set_ships(self, ships):
        self.mode = "placement"
        self.ships = [list(map(tuple, s)) for s in ships]
        self.update()

    # ------------------------------------------------------------------ геометрия
    def _layout(self):
        w, h = self.width(), self.height()
        lab = max(14.0, min(w, h) * 0.06)
        side = min(w - lab - 4, h - lab - 4)
        cell = max(4.0, side / self.n)
        ox = (w - lab - cell * self.n) / 2 + lab
        oy = (h - lab - cell * self.n) / 2 + lab
        return ox, oy, cell, lab

    def cell_at(self, pos):
        ox, oy, cell, _ = self._layout()
        c = math.floor((pos.x() - ox) / cell)
        r = math.floor((pos.y() - oy) / cell)
        if 0 <= r < self.n and 0 <= c < self.n:
            return r, c
        return None

    # ------------------------------------------------------------------ события
    def _tick(self):
        self._phase = (self._phase + 0.08) % (2 * math.pi)
        if self.best is not None and self.mode == "attack":
            self.update()

    def mouseMoveEvent(self, e):
        cell = self.cell_at(e.position())
        if cell != self.hover:
            self.hover = cell
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mousePressEvent(self, e):
        if not self.interactive:
            return
        cell = self.cell_at(e.position())
        if cell is None:
            return
        if e.button() == Qt.LeftButton:
            self.cellClicked.emit(*cell)
        elif e.button() == Qt.RightButton:
            self.cellMenu.emit(cell[0], cell[1], e.globalPosition().toPoint())

    # ------------------------------------------------------------------ отрисовка
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        ox, oy, cell, lab = self._layout()
        n = self.n
        letters = letters_for(self.letters)

        f = QFont(theme.ui_font(max(7, int(cell * 0.30))))
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        for k in range(n):
            p.drawText(QRectF(ox + k * cell, oy - lab, cell, lab), Qt.AlignCenter, letters[k])
            p.drawText(QRectF(ox - lab - 2, oy + k * cell, lab, cell), Qt.AlignCenter, str(k + 1))

        if self.mode == "attack":
            self._paint_attack(p, ox, oy, cell)
        else:
            self._paint_placement(p, ox, oy, cell)

        # сетка
        p.setPen(QPen(QColor(theme.BORDER), 1))
        for k in range(n + 1):
            p.drawLine(QPointF(ox + k * cell, oy), QPointF(ox + k * cell, oy + n * cell))
            p.drawLine(QPointF(ox, oy + k * cell), QPointF(ox + n * cell, oy + k * cell))
        p.setPen(QPen(QColor(theme.CYAN), 1.5))
        p.drawRoundedRect(QRectF(ox, oy, n * cell, n * cell), 3, 3)

        if self.hover is not None and self.interactive:
            r, c = self.hover
            p.setPen(QPen(QColor(255, 255, 255, 140), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(ox + c * cell + 1, oy + r * cell + 1, cell - 2, cell - 2))
        p.end()

    def _paint_attack(self, p, ox, oy, cell):
        n = self.n
        st = self.states
        inf = self.inferred if self.inferred is not None else st
        probs = self.probs
        maxp = 0.0
        if probs is not None:
            m = (st == UNKNOWN)
            if self.known_empty is not None:
                m &= ~self.known_empty
            if m.any():
                maxp = float(probs[m].max())
        pct_font = QFont(theme.ui_font(max(6, int(cell * 0.26))))
        for r in range(n):
            for c in range(n):
                rect = QRectF(ox + c * cell, oy + r * cell, cell, cell)
                s = int(st[r, c])
                si = int(inf[r, c])
                empty = self.known_empty is not None and self.known_empty[r, c]
                if s == UNKNOWN and si == UNKNOWN:
                    if probs is not None and not empty and maxp > 0:
                        t = probs[r, c] / maxp
                        p.fillRect(rect, theme.heat_color(0.08 + 0.92 * t))
                        if self.show_numbers and cell >= 22:
                            pct = int(round(probs[r, c] * 100))
                            p.setFont(pct_font)
                            p.setPen(QColor(255, 255, 255, 200 if t > 0.55 else 110))
                            p.drawText(rect, Qt.AlignCenter, str(pct))
                    else:
                        p.fillRect(rect, QColor("#0e1424"))
                        if empty:
                            p.setPen(Qt.NoPen)
                            p.setBrush(QColor(theme.MUTED))
                            p.drawEllipse(rect.center(), cell * 0.06, cell * 0.06)
                elif s == MISS:
                    p.fillRect(rect, QColor("#0e1424"))
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor("#9fb3d9"))
                    p.drawEllipse(rect.center(), cell * 0.12, cell * 0.12)
                else:
                    sunk = si == SUNK or s == SUNK
                    p.fillRect(rect, QColor("#4a0d1c") if sunk else QColor("#2a1020"))
                    pen = QPen(QColor(theme.RED if sunk else "#ff8a3d"), max(2.0, cell * 0.09))
                    pen.setCapStyle(Qt.RoundCap)
                    p.setPen(pen)
                    k = cell * 0.26
                    p.drawLine(QPointF(rect.left() + k, rect.top() + k),
                               QPointF(rect.right() - k, rect.bottom() - k))
                    p.drawLine(QPointF(rect.left() + k, rect.bottom() - k),
                               QPointF(rect.right() - k, rect.top() + k))
        # обводка убитых кораблей
        if self.inferred is not None:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(theme.RED), 2))
            for r in range(n):
                for c in range(n):
                    if self.inferred[r, c] == SUNK:
                        p.drawRect(QRectF(ox + c * cell + 1, oy + r * cell + 1, cell - 2, cell - 2))
        # лучший выстрел и альтернативы
        for i, (r, c, _) in enumerate(self.top[1:3], start=2):
            rect = QRectF(ox + c * cell + 2, oy + r * cell + 2, cell - 4, cell - 4)
            p.setPen(QPen(QColor(0, 240, 255, 120), 1.5, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)
        if self.best is not None:
            r, c = self.best
            glow = 0.5 + 0.5 * math.sin(self._phase)
            center = QPointF(ox + (c + 0.5) * cell, oy + (r + 0.5) * cell)
            for w, a in ((6, 50), (3, 120), (1.6, 255)):
                col = QColor(theme.CYAN)
                col.setAlpha(int(a * (0.55 + 0.45 * glow)))
                p.setPen(QPen(col, w))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(center, cell * 0.42, cell * 0.42)
            p.setPen(QPen(QColor(theme.CYAN), 1.2))
            L = cell * 0.62
            p.drawLine(QPointF(center.x() - L, center.y()), QPointF(center.x() - cell * 0.2, center.y()))
            p.drawLine(QPointF(center.x() + cell * 0.2, center.y()), QPointF(center.x() + L, center.y()))
            p.drawLine(QPointF(center.x(), center.y() - L), QPointF(center.x(), center.y() - cell * 0.2))
            p.drawLine(QPointF(center.x(), center.y() + cell * 0.2), QPointF(center.x(), center.y() + L))

    def _paint_placement(self, p, ox, oy, cell):
        n = self.n
        p.fillRect(QRectF(ox, oy, n * cell, n * cell), QColor("#0e1424"))
        colors = {4: theme.MAGENTA, 3: theme.CYAN, 2: theme.GREEN, 1: theme.AMBER}
        for ship in self.ships:
            col = QColor(colors.get(len(ship), theme.CYAN))
            rs = [r for r, _ in ship]
            cs = [c for _, c in ship]
            rect = QRectF(ox + min(cs) * cell + 3, oy + min(rs) * cell + 3,
                          (max(cs) - min(cs) + 1) * cell - 6, (max(rs) - min(rs) + 1) * cell - 6)
            fill = QColor(col)
            fill.setAlpha(70)
            path = QPainterPath()
            path.addRoundedRect(rect, cell * 0.25, cell * 0.25)
            p.fillPath(path, fill)
            p.setPen(QPen(col, 2))
            p.drawPath(path)
