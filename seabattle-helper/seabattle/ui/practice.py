"""Тренировка: встроенная партия против компьютера — проверить помощника без сайта."""
from __future__ import annotations

import random

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from ..game import Referee
from ..rules import HIT, MISS, SUNK, UNKNOWN, Rules, letters_for
from ..solver import random_layout


class PracticeBoard(QWidget):
    """Поле в стиле «тетрадки» (как на популярных сайтах): белый фон, синяя сетка."""
    shot = Signal(int, int)

    def __init__(self, n=10, cell=32, parent=None):
        super().__init__(parent)
        self.n = n
        self.cell = cell
        self.states = None
        self.letters = "ru"
        self.margin = 26
        self.setFixedSize(self.margin + n * cell + 8, self.margin + n * cell + 8)
        self.setCursor(Qt.PointingHandCursor)

    @property
    def origin(self):
        return self.margin, self.margin

    def mousePressEvent(self, e):
        ox, oy = self.origin
        c = int((e.position().x() - ox) // self.cell)
        r = int((e.position().y() - oy) // self.cell)
        if 0 <= r < self.n and 0 <= c < self.n and e.button() == Qt.LeftButton:
            self.shot.emit(r, c)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("white"))
        ox, oy = self.origin
        n, cs = self.n, self.cell
        p.setPen(QColor(70, 70, 90))
        p.setFont(QFont("Arial", 10))
        L = letters_for(self.letters)
        for k in range(n):
            p.drawText(QRectF(ox + k * cs, 2, cs, self.margin - 4), Qt.AlignCenter, L[k])
            p.drawText(QRectF(0, oy + k * cs, self.margin - 4, cs), Qt.AlignCenter, str(k + 1))
        st = self.states
        if st is not None:
            for r in range(n):
                for c in range(n):
                    x, y = ox + c * cs, oy + r * cs
                    s = st[r, c]
                    if s == MISS:
                        p.setPen(Qt.NoPen)
                        p.setBrush(QColor(60, 60, 60))
                        p.drawEllipse(QPointF(x + cs / 2, y + cs / 2), cs * 0.1, cs * 0.1)
                    elif s in (HIT, SUNK):
                        if s == SUNK:
                            p.fillRect(QRectF(x + 1, y + 1, cs - 2, cs - 2), QColor(255, 214, 214))
                        pen = QPen(QColor(225, 30, 30), max(2, cs // 12))
                        p.setPen(pen)
                        k = cs * 0.22
                        p.drawLine(QPointF(x + k, y + k), QPointF(x + cs - k, y + cs - k))
                        p.drawLine(QPointF(x + k, y + cs - k), QPointF(x + cs - k, y + k))
        p.setPen(QPen(QColor(180, 180, 255), 1))
        for k in range(n + 1):
            p.drawLine(QPointF(ox + k * cs + 0.5, oy), QPointF(ox + k * cs + 0.5, oy + n * cs))
            p.drawLine(QPointF(ox, oy + k * cs + 0.5), QPointF(ox + n * cs, oy + k * cs + 0.5))
        p.end()


class PracticeWindow(QWidget):
    def __init__(self, rules: Rules, letters="ru", parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Тренировка — морской бой")
        self.setStyleSheet("QWidget{background:white;color:#222;font-family:Arial;font-size:10pt}"
                           "QPushButton{background:#eef;border:1px solid #99c;border-radius:5px;padding:5px 10px}"
                           "QPushButton:hover{background:#dde}")
        self.rules = rules
        self.board = PracticeBoard(rules.size)
        self.board.letters = letters
        self.board.shot.connect(self.shoot)
        self.info = QLabel()
        self.auto_mark = QCheckBox("Отмечать клетки вокруг убитых")
        self.auto_mark.setChecked(True)
        btn = QPushButton("Новая партия")
        btn.clicked.connect(self.new_game)
        title = QLabel("Поле противника")
        title.setStyleSheet("font-size:13pt;font-weight:bold;color:#335")
        top = QHBoxLayout()
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(btn)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.board, 0, Qt.AlignCenter)
        lay.addWidget(self.info)
        lay.addWidget(self.auto_mark)
        self.rng = random.Random()
        self.new_game()

    def new_game(self):
        self.ref = Referee(random_layout(self.rules, self.rng), self.rules,
                           auto_mark=self.auto_mark.isChecked())
        self.board.states = self.ref.states
        self.info.setText("Стреляй по полю — помощник подскажет, куда.")
        self.board.update()

    def shoot(self, r, c):
        if self.ref.done or self.ref.states[r, c] != UNKNOWN:
            return
        self.ref.auto_mark = self.auto_mark.isChecked()
        res = self.ref.shoot(r, c)
        word = {MISS: "Мимо", HIT: "Ранил!", SUNK: "Убил!"}[res]
        left = sum(1 for s in self.ref.sunk if not s)
        if self.ref.done:
            self.info.setText(f"Победа за {self.ref.shots} выстрелов! «Новая партия» — ещё раз.")
        else:
            self.info.setText(f"{word}  Выстрелов: {self.ref.shots}.  Кораблей осталось: {left}.")
        self.board.update()
