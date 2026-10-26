"""Захват экрана через Qt (на Windows это BitBlt/DXGI внутри Qt — без лишних библиотек)."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF
from PySide6.QtGui import QGuiApplication, QImage


def screens():
    return QGuiApplication.screens()


def find_screen(name: str):
    for s in screens():
        if s.name() == name:
            return s
    return QGuiApplication.primaryScreen()


def grab_screen(screen) -> QImage:
    """Весь экран в физических пикселях (QImage RGB32)."""
    pm = screen.grabWindow(0)
    img = pm.toImage()
    if img.format() != QImage.Format_RGB32:
        img = img.convertToFormat(QImage.Format_RGB32)
    return img


def qimage_to_rgb(img: QImage, rect: QRect | None = None) -> np.ndarray:
    """QImage → numpy (H, W, 3) uint8 RGB, по желанию только кусок rect (быстрее)."""
    if rect is not None:
        rect = rect.intersected(img.rect())
        if rect.isEmpty():
            return np.zeros((1, 1, 3), np.uint8)
        img = img.copy(rect)
    if img.format() != QImage.Format_RGB32:
        img = img.convertToFormat(QImage.Format_RGB32)
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    arr = buf[:, : w * 4].reshape(h, w, 4)
    return arr[..., [2, 1, 0]].copy()          # BGRA → RGB


def rgb_to_qimage(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr[..., :3].astype(np.uint8))
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, 3 * w, QImage.Format_RGB888)
    return img.copy()


class ScreenMap:
    """Пересчёт координат: пиксели снимка экрана ↔ логические координаты Qt (с учётом масштаба)."""

    def __init__(self, screen, image_size):
        self.screen = screen
        g = screen.geometry()
        self.origin = QPointF(g.x(), g.y())
        self.kx = g.width() / max(1, image_size[0])
        self.ky = g.height() / max(1, image_size[1])

    def to_logical(self, x: float, y: float) -> QPointF:
        return QPointF(self.origin.x() + x * self.kx, self.origin.y() + y * self.ky)

    def rect_to_logical(self, x0, y0, x1, y1) -> QRectF:
        a = self.to_logical(x0, y0)
        b = self.to_logical(x1, y1)
        return QRectF(a, b)

    def to_image(self, p: QPoint):
        return ((p.x() - self.origin.x()) / self.kx, (p.y() - self.origin.y()) / self.ky)
