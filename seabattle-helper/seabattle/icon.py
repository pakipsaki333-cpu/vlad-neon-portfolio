"""Иконка приложения рисуется кодом — не нужно хранить бинарники в репозитории."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient


def make_pixmap(size: int = 256) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size
    r = QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92)
    bg = QRadialGradient(QPointF(s * 0.5, s * 0.45), s * 0.6)
    bg.setColorAt(0, QColor("#1b2550"))
    bg.setColorAt(1, QColor("#0b0f1a"))
    path = QPainterPath()
    path.addRoundedRect(r, s * 0.2, s * 0.2)
    p.fillPath(path, bg)
    p.setPen(QPen(QColor("#00f0ff"), max(1.0, s * 0.03)))
    p.drawPath(path)
    # сетка 4×4
    p.setPen(QPen(QColor(0, 240, 255, 70), max(1.0, s * 0.012)))
    g0, g1 = s * 0.2, s * 0.8
    for i in range(5):
        t = g0 + (g1 - g0) * i / 4
        p.drawLine(QPointF(t, g0), QPointF(t, g1))
        p.drawLine(QPointF(g0, t), QPointF(g1, t))
    # прицел
    c = QPointF(s * 0.5, s * 0.5)
    p.setPen(QPen(QColor("#ff2bd6"), max(1.5, s * 0.045)))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(c, s * 0.2, s * 0.2)
    p.setPen(QPen(QColor("#ff2bd6"), max(1.0, s * 0.03)))
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        p.drawLine(QPointF(c.x() + dx * s * 0.1, c.y() + dy * s * 0.1),
                   QPointF(c.x() + dx * s * 0.3, c.y() + dy * s * 0.3))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#39ff14"))
    p.drawEllipse(c, s * 0.045, s * 0.045)
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for sz in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(make_pixmap(sz))
    return icon


def save_ico(path: str) -> bool:
    """Сохранить .ico (для сборки exe). Нужен QGuiApplication."""
    return make_pixmap(256).toImage().save(path, "ICO")


def glyph_icon(kind: str, color: str = "#8aa0c8", size: int = 32) -> QIcon:
    """Значки кнопок заголовка (рисуем сами — не зависим от шрифтов с эмодзи)."""
    def draw(col):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(col), size * 0.09)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        s = size
        if kind == "pin":
            p.drawEllipse(QPointF(s * 0.5, s * 0.34), s * 0.16, s * 0.16)
            p.drawLine(QPointF(s * 0.5, s * 0.5), QPointF(s * 0.5, s * 0.86))
            p.drawLine(QPointF(s * 0.3, s * 0.52), QPointF(s * 0.7, s * 0.52))
        elif kind == "mini":
            p.drawRoundedRect(QRectF(s * 0.18, s * 0.3, s * 0.64, s * 0.4), s * 0.06, s * 0.06)
            p.drawLine(QPointF(s * 0.3, s * 0.5), QPointF(s * 0.7, s * 0.5))
        elif kind == "min":
            p.drawLine(QPointF(s * 0.25, s * 0.62), QPointF(s * 0.75, s * 0.62))
        elif kind == "close":
            p.drawLine(QPointF(s * 0.28, s * 0.28), QPointF(s * 0.72, s * 0.72))
            p.drawLine(QPointF(s * 0.28, s * 0.72), QPointF(s * 0.72, s * 0.28))
        p.end()
        return pm
    icon = QIcon()
    icon.addPixmap(draw(color), QIcon.Normal, QIcon.Off)
    icon.addPixmap(draw("#00f0ff"), QIcon.Normal, QIcon.On)
    icon.addPixmap(draw("#ffffff"), QIcon.Active, QIcon.Off)
    return icon
