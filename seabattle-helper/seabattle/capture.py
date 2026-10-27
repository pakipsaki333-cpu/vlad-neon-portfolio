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


def native_origin(screen) -> tuple:
    """Левый верхний угол экрана в физических пикселях рабочего стола (для щелчков мышью).

    Qt 6 хранит у экрана «родной» левый верхний угол, а размер — в логических единицах,
    так что обычно это просто geometry().topLeft(). В Windows сверяемся с самой системой.
    """
    from . import winutil
    g = screen.geometry()
    rects = winutil.monitor_rects()
    r = rects.get(screen.name())
    if r is None and rects:
        # имя не совпало (Qt может звать монитор «по-человечески») — ищем по размеру и положению
        dpr = screen.devicePixelRatio() or 1.0
        w, h = g.width() * dpr, g.height() * dpr

        def score(m):
            return (abs((m[2] - m[0]) - w) + abs((m[3] - m[1]) - h) > 4,
                    abs(m[0] - g.x()) + abs(m[1] - g.y()))
        r = min(rects.values(), key=score)
    if r is not None:
        return float(r[0]), float(r[1])
    return float(g.x()), float(g.y())


class ScreenMap:
    """Пересчёт координат: пиксели снимка экрана ↔ логические координаты Qt (с учётом масштаба)
    ↔ физические координаты рабочего стола (куда щёлкает автоигра)."""

    def __init__(self, screen, image_size):
        self.screen = screen
        g = screen.geometry()
        self.origin = QPointF(g.x(), g.y())
        self.kx = g.width() / max(1, image_size[0])
        self.ky = g.height() / max(1, image_size[1])
        self._native = None

    @property
    def native(self) -> tuple:
        if self._native is None:
            self._native = native_origin(self.screen)
        return self._native

    def to_native(self, x: float, y: float) -> tuple:
        """Пиксель снимка → точка рабочего стола для SetCursorPos (снимок — в физических пикселях)."""
        nx, ny = self.native
        return int(round(nx + x)), int(round(ny + y))

    def native_to_image(self, px: float, py: float) -> tuple:
        nx, ny = self.native
        return px - nx, py - ny

    def to_logical(self, x: float, y: float) -> QPointF:
        return QPointF(self.origin.x() + x * self.kx, self.origin.y() + y * self.ky)

    def rect_to_logical(self, x0, y0, x1, y1) -> QRectF:
        a = self.to_logical(x0, y0)
        b = self.to_logical(x1, y1)
        return QRectF(a, b)

    def to_image(self, p: QPoint):
        return ((p.x() - self.origin.x()) / self.kx, (p.y() - self.origin.y()) / self.ky)
