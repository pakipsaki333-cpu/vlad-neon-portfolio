"""Выбор поля на экране: застывший снимок, найденные поля подсвечены, можно выделить рамкой."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ..capture import qimage_to_rgb
from ..vision import GridGeom, refine_grid
from . import theme


class RegionPicker(QWidget):
    picked = Signal(object, object)      # enemy GridGeom, my GridGeom | None (пиксели снимка)
    cancelled = Signal()

    def __init__(self, screen, image, grids, n: int = 10):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.screen_ = screen
        self.image = image
        self.pix = QPixmap.fromImage(image)
        self.grids = list(grids)
        self.n = n
        self.step = "enemy"
        self.enemy = None
        self.drag_from = None
        self.drag_to = None
        self.hover = None
        self.message = ""
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(screen.geometry())

    # пиксели снимка ↔ координаты виджета
    @property
    def k(self):
        return self.image.width() / max(1, self.width())

    def to_widget(self, g: GridGeom) -> QRectF:
        k = self.k
        return QRectF(g.x0 / k, g.y0 / k, g.width / k, g.height / k)

    def grid_at(self, pos):
        for g in self.grids:
            if self.to_widget(g).adjusted(-6, -6, 6, 6).contains(pos):
                return g
        return None

    def start(self):
        self.showFullScreen()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ события
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self.step == "my" and self.enemy is not None:
                self._finish(None)
            else:
                self.cancelled.emit()
                self.close()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.step == "my":
            self._finish(None)

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self.drag_from is not None:
            self.drag_to = pos
        else:
            self.hover = self.grid_at(pos)
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self.keyPressEvent(type("E", (), {"key": lambda self_: Qt.Key_Escape})())
            return
        self.drag_from = e.position()
        self.drag_to = e.position()

    def mouseReleaseEvent(self, e):
        if self.drag_from is None:
            return
        a, b = self.drag_from, e.position()
        self.drag_from = self.drag_to = None
        if abs(a.x() - b.x()) < 12 and abs(a.y() - b.y()) < 12:
            g = self.grid_at(b)
            if g is None:
                self.message = "Здесь поля не нашлось — выдели его рамкой (от угла до угла клеток)."
                self.update()
                return
            self._choose(g)
            return
        k = self.k
        x0, x1 = sorted((a.x() * k, b.x() * k))
        y0, y1 = sorted((a.y() * k, b.y() * k))
        rough = GridGeom(x0, y0, (x1 - x0) / self.n, (y1 - y0) / self.n, self.n)
        margin = int(max(x1 - x0, y1 - y0) * 0.2)
        from PySide6.QtCore import QRect
        crop = QRect(int(x0) - margin, int(y0) - margin, int(x1 - x0) + 2 * margin,
                     int(y1 - y0) + 2 * margin).intersected(self.image.rect())
        arr = qimage_to_rgb(self.image, crop)
        local = rough.shifted(-crop.x(), -crop.y())
        g, conf = refine_grid(arr, local)
        g = g.shifted(crop.x(), crop.y())
        if conf < 0.15:
            self.message = "Линий сетки не видно — беру рамку как есть. Выделяй точно по клеткам."
        self._choose(g)

    def _choose(self, g: GridGeom):
        if self.step == "enemy":
            self.enemy = g
            others = [x for x in self.grids if x is not g and abs(x.x0 - g.x0) > g.cw]
            if len(others) == 1:
                self._finish(others[0])
                return
            self.step = "my"
            self.message = ""
            self.update()
        else:
            self._finish(g)

    def _finish(self, my):
        self.picked.emit(self.enemy, my)
        self.close()

    # ------------------------------------------------------------------ отрисовка
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawPixmap(self.rect(), self.pix)
        p.fillRect(self.rect(), QColor(5, 8, 16, 120))
        for g in self.grids:
            r = self.to_widget(g)
            is_enemy = self.enemy is g
            col = QColor(theme.CYAN if (self.step == "enemy" or is_enemy) else theme.MAGENTA)
            if g is self.hover:
                fill = QColor(col)
                fill.setAlpha(50)
                p.fillRect(r, fill)
            p.setPen(QPen(col, 3 if g is self.hover or is_enemy else 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(r)
            for i in range(1, self.n):
                c2 = QColor(col)
                c2.setAlpha(90)
                p.setPen(QPen(c2, 1))
                p.drawLine(QPointF(r.x() + i * r.width() / self.n, r.y()),
                           QPointF(r.x() + i * r.width() / self.n, r.bottom()))
                p.drawLine(QPointF(r.x(), r.y() + i * r.height() / self.n),
                           QPointF(r.right(), r.y() + i * r.height() / self.n))
            if is_enemy:
                p.setPen(QColor(theme.CYAN))
                p.setFont(theme.ui_font(11, True))
                p.drawText(QRectF(r.x(), r.y() - 26, r.width(), 22), Qt.AlignCenter, "Поле соперника ✓")
        if self.drag_from is not None and self.drag_to is not None:
            r = QRectF(self.drag_from, self.drag_to).normalized()
            p.setPen(QPen(QColor(theme.MAGENTA), 2, Qt.DashLine))
            p.setBrush(QColor(255, 43, 214, 40))
            p.drawRect(r)
            for i in range(1, self.n):
                p.setPen(QPen(QColor(255, 43, 214, 90), 1))
                p.drawLine(QPointF(r.x() + i * r.width() / self.n, r.y()),
                           QPointF(r.x() + i * r.width() / self.n, r.bottom()))
                p.drawLine(QPointF(r.x(), r.y() + i * r.height() / self.n),
                           QPointF(r.right(), r.y() + i * r.height() / self.n))
        # плашка с инструкцией
        if self.step == "enemy":
            found = f"Найдено полей: {len(self.grids)}. " if self.grids else "Поле не нашлось автоматически. "
            text = (found + "Щёлкни по полю СОПЕРНИКА (по нему ты стреляешь) "
                    "или выдели его рамкой — точно по клеткам, без букв и цифр.   Esc — отмена")
        else:
            text = ("Готово! Теперь можно указать СВОЁ поле (для подсказок расстановки) — "
                    "щёлкни по нему или выдели рамкой.   Enter/Esc — пропустить")
        f = QFont(theme.ui_font(12, True))
        p.setFont(f)
        w = min(self.width() - 40, p.fontMetrics().horizontalAdvance(text) + 40)
        box = QRectF((self.width() - w) / 2, 18, w, 44)
        p.setPen(QPen(QColor(theme.CYAN), 1.5))
        p.setBrush(QColor(11, 15, 26, 235))
        p.drawRoundedRect(box, 10, 10)
        p.setPen(QColor(theme.TEXT))
        p.drawText(box.adjusted(12, 0, -12, 0), Qt.AlignCenter | Qt.TextWordWrap, text)
        if self.message:
            p.setFont(theme.ui_font(11))
            box2 = QRectF((self.width() - w) / 2, 68, w, 30)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(60, 30, 0, 220))
            p.drawRoundedRect(box2, 8, 8)
            p.setPen(QColor(theme.AMBER))
            p.drawText(box2, Qt.AlignCenter, self.message)
        p.end()
