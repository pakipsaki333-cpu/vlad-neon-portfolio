"""Подсказка прямо поверх игры: прозрачное окно, клики проходят насквозь."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from .. import winutil
from ..rules import UNKNOWN, coord_name
from . import theme

BADGE_H = 30


class Overlay(QWidget):
    def __init__(self, kind: str = "attack"):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.kind = kind
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.board = QRectF()          # прямоугольник клеток в глобальных логических координатах
        self.n = 10
        self.best = None
        self.best_prob = 0.0
        self.top = []
        self.probs = None
        self.states = None
        self.heat = False
        self.ships = []
        self.letters = "ru"
        self.captions = True
        self.capture_hidden = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(60)

    def showEvent(self, e):
        super().showEvent(e)
        hwnd = int(self.winId())
        winutil.make_click_through(hwnd)
        self.capture_hidden = winutil.exclude_from_capture(hwnd, True)

    def _tick(self):
        self._phase = (self._phase + 0.12) % (2 * math.pi)
        if self.isVisible():
            self.update()

    def place(self, board_rect: QRectF, n: int):
        self.board = QRectF(board_rect)
        self.n = n
        m = 8
        g = board_rect.adjusted(-m, -m - BADGE_H, m, m).toAlignedRect()
        if self.geometry() != g:
            self.setGeometry(g)

    def set_attack(self, analysis, states, heat=False):
        self.best = analysis.best if analysis else None
        self.best_prob = analysis.best_prob if analysis else 0.0
        self.top = analysis.top if analysis else []
        self.probs = analysis.probs if analysis else None
        self.states = states
        self.heat = heat
        self.update()

    def set_ships(self, ships):
        self.ships = [list(map(tuple, s)) for s in ships]
        self.update()

    def _local_board(self):
        return QRectF(self.board.x() - self.x(), self.board.y() - self.y(),
                      self.board.width(), self.board.height())

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        b = self._local_board()
        cw = b.width() / self.n
        ch = b.height() / self.n
        if self.kind == "attack":
            self._paint_attack(p, b, cw, ch)
        else:
            self._paint_ships(p, b, cw, ch)
        p.end()

    def _paint_attack(self, p, b, cw, ch):
        # Тепловая карта поверх клеток — только если окно точно скрыто от захвата,
        # иначе помощник увидел бы собственную раскраску.
        if self.heat and self.capture_hidden and self.probs is not None and self.states is not None:
            mx = max(1e-6, float(self.probs[self.states == UNKNOWN].max()) if (self.states == UNKNOWN).any() else 1)
            for r in range(self.n):
                for c in range(self.n):
                    if self.states[r, c] != UNKNOWN:
                        continue
                    col = theme.heat_color(self.probs[r, c] / mx)
                    col.setAlpha(int(40 + 110 * self.probs[r, c] / mx))
                    p.fillRect(QRectF(b.x() + c * cw, b.y() + r * ch, cw, ch), col)
        if self.best is None:
            return
        r, c = self.best
        glow = 0.5 + 0.5 * math.sin(self._phase)
        cell = QRectF(b.x() + c * cw, b.y() + r * ch, cw, ch)
        # Всё рисуем строго по линиям сетки: внутрь клеток (там смотрит распознавание, отступ 16%)
        # линии заходят не дальше чем на ~10% — даже если окно не удалось спрятать от захвата.
        m = min(cw, ch)
        t = max(2.0, m * 0.09)
        glow_w = min(m * 0.2, t * 2.4)
        for k, a in ((glow_w, 70), (t, 255)):
            col = QColor(theme.CYAN)
            col.setAlpha(int(a * (0.55 + 0.45 * glow)))
            p.setPen(QPen(col, k))
            p.setBrush(Qt.NoBrush)
            p.drawRect(cell)
        # «прицел»: продолжение линий сетки от углов клетки наружу
        pen = QPen(QColor(theme.MAGENTA), 2)
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        L = m * 0.7
        for x in (cell.left(), cell.right()):
            for y in (cell.top(), cell.bottom()):
                dx = -L if x == cell.left() else L
                dy = -L if y == cell.top() else L
                p.drawLine(QPointF(x, y), QPointF(x + dx, y))
                p.drawLine(QPointF(x, y), QPointF(x, y + dy))
        if self.captions:
            text = f"Бей: {coord_name(r, c, self.letters)}  {self.best_prob * 100:.0f}%"
            f = QFont(theme.ui_font(11, True))
            p.setFont(f)
            w = p.fontMetrics().horizontalAdvance(text) + 20
            bx = min(max(b.x(), cell.center().x() - w / 2), b.right() - w)
            badge = QRectF(bx, b.y() - BADGE_H - 4, w, BADGE_H - 4)
            path = QPainterPath()
            path.addRoundedRect(badge, 8, 8)
            p.fillPath(path, QColor(11, 15, 26, 225))
            p.setPen(QPen(QColor(theme.CYAN), 1.5))
            p.drawPath(path)
            p.setPen(QColor(theme.TEXT))
            p.drawText(badge, Qt.AlignCenter, text)

    def _paint_ships(self, p, b, cw, ch):
        colors = {4: theme.MAGENTA, 3: theme.CYAN, 2: theme.GREEN, 1: theme.AMBER}
        for ship in self.ships:
            rs = [r for r, _ in ship]
            cs = [c for _, c in ship]
            rect = QRectF(b.x() + min(cs) * cw + 3, b.y() + min(rs) * ch + 3,
                          (max(cs) - min(cs) + 1) * cw - 6, (max(rs) - min(rs) + 1) * ch - 6)
            col = QColor(colors.get(len(ship), theme.CYAN))
            fill = QColor(col)
            fill.setAlpha(60)
            path = QPainterPath()
            path.addRoundedRect(rect, min(cw, ch) * 0.25, min(cw, ch) * 0.25)
            p.fillPath(path, fill)
            pen = QPen(col, 2.5, Qt.DashLine)
            p.setPen(pen)
            p.drawPath(path)
        if self.captions and self.ships:
            f = QFont(theme.ui_font(10, True))
            p.setFont(f)
            text = "Так можно расставить корабли"
            w = p.fontMetrics().horizontalAdvance(text) + 20
            badge = QRectF(b.x(), b.y() - BADGE_H - 4, w, BADGE_H - 4)
            path = QPainterPath()
            path.addRoundedRect(badge, 8, 8)
            p.fillPath(path, QColor(11, 15, 26, 225))
            p.setPen(QPen(QColor(theme.MAGENTA), 1.5))
            p.drawPath(path)
            p.setPen(QColor(theme.TEXT))
            p.drawText(badge, Qt.AlignCenter, text)
