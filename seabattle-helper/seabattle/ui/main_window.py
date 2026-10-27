"""Главное окно помощника (и «мозг» приложения: захват → распознавание → решатель → подсказка)."""
from __future__ import annotations

import random
import threading
import time

import numpy as np
from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFrame,
                               QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
                               QPushButton, QRadioButton, QScrollArea, QSizeGrip, QSlider, QSpinBox,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import __version__, capture, placement, winutil
from .. import mouse as mouse_mod
from ..autoplay import AutoPilot, Seen
from ..board import components
from ..rules import (HIT, LETTERS_TOP, MISS, NUMBERS_TOP, PRESETS, SHIP_NAMES, SUNK, TOUCH_ANY,
                     TOUCH_CORNERS, TOUCH_LABELS, TOUCH_NONE, UNKNOWN, Rules, coord_name, fleet_to_text,
                     parse_fleet, plural)
from ..settings import Settings
from ..solver import analyze
from ..vision import (BoardTracker, GridGeom, border_signature, detect_label_layout, extract_patches,
                      find_grids, signature_match)
from ..icon import glyph_icon
from . import theme
from .grid_widget import BoardView
from .overlay import Overlay
from .practice import PracticeWindow
from .region_picker import RegionPicker


# ============================================================================ решатель в фоне
class SolverWorker(QThread):
    done = Signal(object, int)

    def __init__(self):
        super().__init__()
        self._cv = threading.Condition()
        self._job = None
        self._stop = False
        self.latest = 0

    def submit(self, states, rules, budget):
        with self._cv:
            self.latest += 1
            self._job = (np.array(states, np.int8), rules, budget, self.latest)
            self._cv.notify()
        return self.latest

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify()
        self.wait(3000)

    def run(self):
        while True:
            with self._cv:
                while self._job is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                states, rules, budget, jid = self._job
                self._job = None
            try:
                a = analyze(states, rules, budget=budget, cancel=lambda: self._job is not None)
            except Exception as ex:          # не роняем приложение из-за редкой ошибки
                print("solver error:", ex)
                continue
            if jid == self.latest:
                self.done.emit(a, jid)


# ============================================================================ мелкие виджеты
class FleetBar(QWidget):
    """Какие корабли ещё плавают: ▮▮▮▮ — живые яркие, потопленные — тусклые и зачёркнутые."""

    def __init__(self):
        super().__init__()
        self.fleet = []
        self.remaining = []
        self.setMinimumHeight(26)

    def set_data(self, fleet, remaining):
        self.fleet = list(fleet)
        self.remaining = list(remaining)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rem = list(self.remaining)
        x = 2.0
        unit = 7.0
        h = 12.0
        y = (self.height() - h) / 2
        for L in self.fleet:
            alive = L in rem
            if alive:
                rem.remove(L)
            col = QColor(theme.CYAN if alive else "#33405f")
            for i in range(L):
                p.fillRect(QRectF(x + i * (unit + 1), y, unit, h), col)
            if not alive:
                p.setPen(QPen(QColor(theme.RED), 1.5))
                p.drawLine(int(x - 1), int(y + h / 2), int(x + L * (unit + 1)), int(y + h / 2))
            x += L * (unit + 1) + 7
            if x > self.width() - 20:
                break
        p.end()


class DebugView(QWidget):
    """«Что видит помощник»: снимок поля, сетка и распознанные отметки."""

    def __init__(self):
        super().__init__(None, Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Что видит помощник")
        self.setStyleSheet(theme.STYLESHEET)
        self.img = None
        self.geom = None
        self.states = None
        self.capture_hidden = False
        self.info = QLabel()
        self.info.setObjectName("Sub")
        self.info.setWordWrap(True)
        self.canvas = QLabel()
        self.canvas.setMinimumSize(320, 320)
        self.canvas.setAlignment(Qt.AlignCenter)
        lay = QVBoxLayout(self)
        lay.addWidget(self.canvas, 1)
        lay.addWidget(self.info)
        self.resize(420, 470)

    def showEvent(self, e):
        super().showEvent(e)
        # окно не должно попадать в захват: если положить его на поле, помощник увидит сам себя
        self.capture_hidden = winutil.exclude_from_capture(int(self.winId()), True)

    def update_view(self, qimg, geom, states, status):
        if not self.isVisible() or qimg is None or geom is None:
            return
        # снимок экрана при масштабе 125–150% несёт devicePixelRatio>1, и QPainter стал бы рисовать
        # сетку в «логических» единицах — она разъезжалась с картинкой. Рисуем в пикселях снимка.
        img = qimg.copy()
        img.setDevicePixelRatio(1.0)
        pm = QPixmap.fromImage(img)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        n = geom.n
        p.setPen(QPen(QColor(0, 240, 255, 200), 1))
        for k in range(n + 1):
            p.drawLine(int(geom.x0 + k * geom.cw), int(geom.y0), int(geom.x0 + k * geom.cw), int(geom.y0 + geom.height))
            p.drawLine(int(geom.x0), int(geom.y0 + k * geom.ch), int(geom.x0 + geom.width), int(geom.y0 + k * geom.ch))
        cols = {MISS: QColor(120, 160, 255), HIT: QColor(255, 140, 40), SUNK: QColor(255, 40, 80)}
        names = {MISS: "·", HIT: "Р", SUNK: "У"}
        f = theme.ui_font(max(7, int(geom.ch * 0.3)), True)
        p.setFont(f)
        for r in range(n):
            for c in range(n):
                s = int(states[r, c])
                if s == UNKNOWN:
                    continue
                rect = QRectF(geom.x0 + c * geom.cw + 2, geom.y0 + r * geom.ch + 2, geom.cw - 4, geom.ch - 4)
                p.setPen(QPen(cols[s], 2))
                p.drawRect(rect)
                p.drawText(rect, Qt.AlignRight | Qt.AlignBottom, names[s])
        p.end()
        dpr = self.canvas.devicePixelRatioF() or 1.0
        size = self.canvas.size()
        spm = pm.scaled(int(size.width() * dpr), int(size.height() * dpr), Qt.KeepAspectRatio,
                        Qt.SmoothTransformation)
        spm.setDevicePixelRatio(dpr)
        self.canvas.setPixmap(spm)
        if not self.capture_hidden:
            status += " Не клади это окно на поле — помощник увидит его вместо игры."
        self.info.setText(status)


# ============================================================================ главное окно
class MainWindow(QWidget):
    def __init__(self, settings: Settings | None = None, icon: QIcon | None = None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Window)
        self.s = settings or Settings.load()
        self.rules = self.s.rules()
        self.setWindowTitle("Морской бой — помощник")
        if icon is not None:
            self.setWindowIcon(icon)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.STYLESHEET)
        self.setMinimumSize(300, 150)

        # состояние партии
        n = self.rules.size
        self.manual_states = np.zeros((n, n), np.int8)
        self.history = []
        self.analysis = None
        self.analysis_src = None             # на каких состояниях посчитан self.analysis
        self._submitted = {}
        # распознавание
        self.tracker = None
        self.screen_ = None
        self.enemy_geom = None
        self.my_geom = None
        self.signature = None
        self.lost = 0
        self.last_relocate = 0.0
        self.last_frame = None
        self.last_crop = None
        self.frame_source = None             # для самопроверки: функция → QImage
        self.capture_hidden = False
        self.hotkey_ids = []
        self._hotkey_hwnd = 0
        self._placement_dirty = True
        self.paused = False
        self.placement_ships = []
        self.placement_rng = random.Random()
        self.last_status = None              # что сказал трекер на последнем кадре
        self.label_layout_detected = None    # как подписаны клетки на сайте (узнаём по снимку)
        self._labels_pending = False
        # автоигра
        self.mouse = mouse_mod.default_mouse()
        self.autopilot = AutoPilot(delay=self.s.auto_delay)
        self._auto_cursor = None             # где мы оставили курсор (если сдвинулся — это человек)
        self._busy_since = None
        self._overlay_cells = []             # куда недавно рисовалась рамка-подсказка
        self._signature_cells = set()
        self._cover_warned = False
        self.auto_log = []                   # (время, клетка, точка экрана) — для самопроверки

        self.worker = SolverWorker()
        self.worker.done.connect(self.on_analysis)
        self.worker.start()

        self.overlay = Overlay("attack")
        self.overlay.letters = self.s.letters
        self.ghost = Overlay("ships")
        self.ghost.letters = self.s.letters
        self.overlay.label_layout = self.ghost.label_layout = self.labels_layout
        self.debug = DebugView()
        self.practice = None

        self._build_ui()
        self._apply_window_settings()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.capture_tick)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save_settings)
        self.auto_timer = QTimer(self)
        self.auto_timer.setInterval(100)
        self.auto_timer.timeout.connect(self.auto_tick)

        self.refresh_states(analyze_now=True)
        if self.s.enemy_geom and self.s.screen_name:
            QTimer.singleShot(400, self.restore_calibration)
        else:
            self.set_status("idle", "Открой игру в браузере и нажми «Найти поле» (или Ctrl+Alt+R).")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.frame = QFrame()
        self.frame.setObjectName("Frame")
        outer.addWidget(self.frame)
        root = QVBoxLayout(self.frame)
        root.setContentsMargins(10, 6, 10, 8)
        root.setSpacing(6)

        # заголовок
        bar = QWidget()
        bar.setObjectName("TitleBar")
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(2)
        logo = QLabel("⚓")
        logo.setStyleSheet(f"color:{theme.MAGENTA};font-size:14pt;")
        title = QLabel("Морской бой")
        title.setObjectName("Title")
        title.setToolTip("Помощник для морского боя")
        hb.addWidget(logo)
        hb.addWidget(title)
        hb.addStretch(1)
        self.btn_pin = self._title_btn("pin", "Поверх всех окон", checkable=True)
        self.btn_pin.setChecked(self.s.on_top)
        self.btn_pin.toggled.connect(self.set_on_top)
        self.btn_mini = self._title_btn("mini", "Мини-режим: только подсказка", checkable=True)
        self.btn_mini.setChecked(self.s.mini)
        self.btn_mini.toggled.connect(self.set_mini)
        btn_min = self._title_btn("min", "Свернуть")
        btn_min.clicked.connect(self.showMinimized)
        btn_close = QPushButton()
        btn_close.setIcon(glyph_icon("close"))
        btn_close.setObjectName("CloseBtn")
        btn_close.setToolTip("Закрыть")
        btn_close.clicked.connect(self.close)
        for b in (self.btn_pin, self.btn_mini, btn_min, btn_close):
            hb.addWidget(b)
        self.title_bar = bar
        root.addWidget(bar)

        # статус
        st = QHBoxLayout()
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color:{theme.MUTED};")
        self.status = QLabel("")
        self.status.setObjectName("Status")
        self.status.setWordWrap(True)
        self.btn_find = QPushButton("Найти поле")
        self.btn_find.setObjectName("Primary")
        self.btn_find.setToolTip("Найти поле соперника на экране (Ctrl+Alt+R)")
        self.btn_find.clicked.connect(self.start_calibration)
        st.addWidget(self.status_dot)
        st.addWidget(self.status, 1)
        st.addWidget(self.btn_find)
        self.status_row = QWidget()
        self.status_row.setLayout(st)
        st.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.status_row)

        # подсказка (видна и в мини-режиме)
        card = QFrame()
        card.setObjectName("Card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 8, 12, 8)
        cl.setSpacing(2)
        row = QHBoxLayout()
        self.hint = QLabel("—")
        self.hint.setObjectName("Hint")
        self.hint_pct = QLabel("")
        self.hint_pct.setObjectName("HintPct")
        row.addWidget(self.hint)
        row.addStretch(1)
        row.addWidget(self.hint_pct)
        self.btn_auto = QPushButton("▶ Авто")
        self.btn_auto.setObjectName("AutoBtn")
        self.btn_auto.setCheckable(True)
        self.btn_auto.setFocusPolicy(Qt.NoFocus)
        self.btn_auto.setToolTip("Автоигра: помощник сам стреляет по полю соперника (Ctrl+Alt+A).\n"
                                 "Двинешь мышь — автоигра встанет на паузу.")
        self.btn_auto.toggled.connect(self._auto_toggled)
        row.addSpacing(6)
        row.addWidget(self.btn_auto)
        cl.addLayout(row)
        self.hint_sub = QLabel("")
        self.hint_sub.setObjectName("Sub")
        self.hint_sub.setWordWrap(True)
        cl.addWidget(self.hint_sub)
        self.hint_alt = QLabel("")
        self.hint_alt.setObjectName("Sub")
        cl.addWidget(self.hint_alt)
        self.auto_info = QLabel("")
        self.auto_info.setObjectName("AutoInfo")
        self.auto_info.setWordWrap(True)
        self.auto_info.hide()
        cl.addWidget(self.auto_info)
        self.warn = QLabel("")
        self.warn.setObjectName("Warn")
        self.warn.setWordWrap(True)
        self.warn.hide()
        cl.addWidget(self.warn)
        self.card = card
        root.addWidget(card)

        # вкладки
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_attack(), "Атака")
        self.tabs.addTab(self._tab_place(), "Расстановка")
        self.tabs.addTab(self._tab_settings(), "Настройки")
        self.tabs.addTab(self._tab_help(), "?")
        self.tabs.setTabToolTip(3, "Справка")
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)

        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch(1)
        self.grip = QSizeGrip(self)
        self.grip.setStyleSheet("background:transparent;")
        grip_row.addWidget(self.grip)
        root.addLayout(grip_row)

        act_undo = QAction(self)
        act_undo.setShortcut("Ctrl+Z")
        act_undo.triggered.connect(self.undo)
        self.addAction(act_undo)

    def _title_btn(self, kind, tip, checkable=False):
        b = QPushButton()
        b.setIcon(glyph_icon(kind))
        b.setObjectName("TitleBtn")
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setFocusPolicy(Qt.NoFocus)
        return b

    def _tab_attack(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)
        self.view = BoardView(self.rules.size)
        self.view.letters = self.s.letters
        self.view.label_layout = self.labels_layout
        self.view.cellClicked.connect(self.cycle_cell)
        self.view.cellMenu.connect(self.cell_menu)
        self.view.setToolTip("Щелчок — сменить отметку (мимо → ранил → убил → пусто).\n"
                             "Правый щелчок — меню. Исправления помощник запоминает.")
        lay.addWidget(self.view, 1)
        fl = QHBoxLayout()
        lab = QLabel("Флот:")
        lab.setObjectName("Sub")
        self.fleet_bar = FleetBar()
        fl.addWidget(lab)
        fl.addWidget(self.fleet_bar, 1)
        lay.addLayout(fl)
        self.stats = QLabel("")
        self.stats.setObjectName("Sub")
        self.stats.setWordWrap(True)
        lay.addWidget(self.stats)
        btns = QGridLayout()
        b_new = QPushButton("Новая партия")
        b_new.setToolTip("Очистить поле (Ctrl+Alt+N)")
        b_new.clicked.connect(self.new_game)
        b_undo = QPushButton("Отменить")
        b_undo.setToolTip("Отменить последнее исправление (Ctrl+Z)")
        b_undo.clicked.connect(self.undo)
        self.btn_pause = QPushButton("Пауза")
        self.btn_pause.setCheckable(True)
        self.btn_pause.setToolTip("Остановить слежение за экраном (поле можно размечать вручную)")
        self.btn_pause.toggled.connect(self.set_paused)
        b_dbg = QPushButton("Что видит")
        b_dbg.setToolTip("Показать, как помощник распознал поле")
        b_dbg.clicked.connect(self.show_debug)
        btns.addWidget(b_new, 0, 0)
        btns.addWidget(b_undo, 0, 1)
        btns.addWidget(self.btn_pause, 1, 0)
        btns.addWidget(b_dbg, 1, 1)
        lay.addLayout(btns)
        return w

    def _tab_place(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        self.place_group = QButtonGroup(self)
        for key, (label, _) in placement.STRATEGIES.items():
            b = QPushButton(label)
            b.setCheckable(True)
            b.setProperty("strategy", key)
            b.clicked.connect(lambda _=False, k=key: self.regenerate_placement(k))
            self.place_group.addButton(b)
            row.addWidget(b)
            if key == self.s.placement_strategy:
                b.setChecked(True)
        lay.addLayout(row)
        self.place_text = QLabel("")
        self.place_text.setObjectName("Sub")
        self.place_text.setWordWrap(True)
        lay.addWidget(self.place_text)
        self.place_view = BoardView(self.rules.size)
        self.place_view.letters = self.s.letters
        self.place_view.label_layout = self.labels_layout
        self.place_view.interactive = False
        lay.addWidget(self.place_view, 1)
        self.place_score = QLabel("")
        self.place_score.setObjectName("Sub")
        self.place_score.setWordWrap(True)
        lay.addWidget(self.place_score)
        self.place_list = QLabel("")
        self.place_list.setObjectName("Sub")
        self.place_list.setWordWrap(True)
        self.place_list.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.place_list)
        br = QHBoxLayout()
        b_more = QPushButton("Ещё вариант")
        b_more.clicked.connect(lambda: self.regenerate_placement(self.s.placement_strategy))
        self.btn_ghost = QPushButton("На моём поле")
        self.btn_ghost.setCheckable(True)
        self.btn_ghost.setToolTip("Нарисовать расстановку поверх твоего поля на сайте "
                                  "(сначала укажи своё поле в «Найти поле»)")
        self.btn_ghost.toggled.connect(self.update_ghost)
        b_copy = QPushButton("Копировать")
        b_copy.clicked.connect(self.copy_placement)
        br.addWidget(b_more)
        br.addWidget(self.btn_ghost)
        br.addWidget(b_copy)
        lay.addLayout(br)
        return w

    def _tab_settings(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 4, 0)
        lay.setSpacing(4)

        g = QGroupBox("Правила")
        gl = QVBoxLayout(g)
        self.cmb_preset = QComboBox()
        self.cmb_preset.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_preset.setMinimumContentsLength(12)
        for key, (label, _) in PRESETS.items():
            self.cmb_preset.addItem(label, key)
        self.cmb_preset.addItem("Свои правила", "custom")
        self.cmb_preset.activated.connect(self._preset_chosen)
        gl.addWidget(self.cmb_preset)
        self.touch_group = QButtonGroup(self)
        for key in (TOUCH_NONE, TOUCH_CORNERS, TOUCH_ANY):
            rb = QRadioButton(TOUCH_LABELS[key])
            rb.setProperty("touch", key)
            self.touch_group.addButton(rb)
            gl.addWidget(rb)
            if key == self.s.touch:
                rb.setChecked(True)
        self.touch_group.buttonClicked.connect(lambda _b: self._rules_edited())
        fl = QGridLayout()
        fl.addWidget(QLabel("Флот:"), 0, 0)
        self.ed_fleet = QLineEdit(fleet_to_text(self.s.fleet))
        self.ed_fleet.setMinimumWidth(60)
        self.ed_fleet.setToolTip("Количество×длина: 1x4 2x3 3x2 4x1  или просто длины: 4,3,3,2,2,2,1,1,1,1")
        self.ed_fleet.editingFinished.connect(self._rules_edited)
        fl.addWidget(self.ed_fleet, 0, 1)
        fl.addWidget(QLabel("Поле:"), 1, 0)
        self.sp_size = QSpinBox()
        self.sp_size.setRange(6, 15)
        self.sp_size.setToolTip("Размер поля: 10 — классика")
        self.sp_size.setValue(self.s.size)
        self.sp_size.valueChanged.connect(lambda _v: self._rules_edited())
        fl.addWidget(self.sp_size, 1, 1)
        fl.setColumnStretch(1, 1)
        gl.addLayout(fl)
        self.cb_sunk = QCheckBox("Сайт показывает, что корабль убит")
        self.cb_sunk.setToolTip("Почти все сайты так делают (обводка, точки вокруг, другой цвет).")
        self.cb_sunk.setChecked(self.s.sunk_marked)
        self.cb_sunk.toggled.connect(lambda _v: self._rules_edited())
        gl.addWidget(self.cb_sunk)
        lr = QHBoxLayout()
        lr.addWidget(QLabel("Буквы:"))
        self.cmb_letters = QComboBox()
        self.cmb_letters.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_letters.setMinimumContentsLength(8)
        self.cmb_letters.addItem("А Б В … К (рус)", "ru")
        self.cmb_letters.addItem("A B C … J (лат)", "lat")
        self.cmb_letters.setCurrentIndex(0 if self.s.letters == "ru" else 1)
        self.cmb_letters.currentIndexChanged.connect(self._letters_changed)
        lr.addWidget(self.cmb_letters, 1)
        gl.addLayout(lr)
        lr = QHBoxLayout()
        lr.addWidget(QLabel("Подписи:"))
        self.cmb_labels = QComboBox()
        self.cmb_labels.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_labels.setMinimumContentsLength(8)
        self.cmb_labels.addItem("Как на сайте (сам)", "auto")
        self.cmb_labels.addItem("Буквы сверху: Д5 = столбец Д", LETTERS_TOP)
        self.cmb_labels.addItem("Цифры сверху: Д5 = строка Д", NUMBERS_TOP)
        self.cmb_labels.setToolTip("Как называть клетки. На Яндекс Играх цифры сверху, а буквы сбоку —\n"
                                   "тогда «Д5» — это строка Д, столбец 5. «Сам» — помощник смотрит на поле.")
        self.cmb_labels.setCurrentIndex(max(0, self.cmb_labels.findData(self.s.labels)))
        self.cmb_labels.currentIndexChanged.connect(self._labels_changed)
        lr.addWidget(self.cmb_labels, 1)
        gl.addLayout(lr)
        lay.addWidget(g)
        self._sync_preset_combo()

        g = QGroupBox("Распознавание")
        gl = QVBoxLayout(g)
        gl.addWidget(QLabel("Чувствительность:"))
        self.sl_sens = QSlider(Qt.Horizontal)
        self.sl_sens.setRange(60, 160)
        self.sl_sens.setValue(int(self.s.sensitivity * 100))
        self.sl_sens.setToolTip("Выше — замечает бледные отметки, но чаще ошибается")
        self.sl_sens.valueChanged.connect(self._recog_changed)
        gl.addWidget(self.sl_sens)
        ir = QHBoxLayout()
        ir.addWidget(QLabel("Проверять экран каждые"))
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(150, 2000)
        self.sp_interval.setSingleStep(50)
        self.sp_interval.setSuffix(" мс")
        self.sp_interval.setValue(self.s.interval_ms)
        self.sp_interval.valueChanged.connect(self._recog_changed)
        ir.addWidget(self.sp_interval)
        gl.addLayout(ir)
        self.cb_sunklook = QCheckBox("Узнавать «убил» по виду клетки")
        self.cb_sunklook.setChecked(self.s.detect_sunk_look)
        self.cb_sunklook.toggled.connect(self._recog_changed)
        gl.addWidget(self.cb_sunklook)
        rr = QVBoxLayout()
        b_swap = QPushButton("Поменять поля местами")
        b_swap.setToolTip("Если помощник перепутал своё поле и поле соперника")
        b_swap.clicked.connect(self.swap_boards)
        self.btn_forget = QPushButton("Сбросить обучение")
        self.btn_forget.setToolTip("Забыть, как выглядят отметки на сайте (если сменил сайт)")
        self.btn_forget.clicked.connect(self.forget_examples)
        rr.addWidget(b_swap)
        rr.addWidget(self.btn_forget)
        gl.addLayout(rr)
        lay.addWidget(g)

        g = QGroupBox("Подсказки")
        gl = QVBoxLayout(g)
        gl.addWidget(QLabel("Сколько думать над ходом:"))
        self.sl_budget = QSlider(Qt.Horizontal)
        self.sl_budget.setRange(3, 25)
        self.sl_budget.setValue(int(self.s.budget * 10))
        self.sl_budget.setToolTip("Дольше — точнее вероятности (0.3–2.5 с)")
        self.sl_budget.valueChanged.connect(self._hint_settings_changed)
        gl.addWidget(self.sl_budget)
        self.cb_overlay = QCheckBox("Рамка-подсказка поверх игры")
        self.cb_overlay.setChecked(self.s.overlay)
        self.cb_overlay.toggled.connect(self._hint_settings_changed)
        gl.addWidget(self.cb_overlay)
        self.cb_heat = QCheckBox("Тепловая карта поверх игры")
        self.cb_heat.setChecked(self.s.overlay_heat)
        self.cb_heat.toggled.connect(self._hint_settings_changed)
        gl.addWidget(self.cb_heat)
        lay.addWidget(g)

        g = QGroupBox("Автоигра")
        gl = QVBoxLayout(g)
        ar = QHBoxLayout()
        ar.addWidget(QLabel("Пауза перед выстрелом:"))
        self.sp_auto_delay = QDoubleSpinBox()
        self.sp_auto_delay.setRange(0.2, 5.0)
        self.sp_auto_delay.setSingleStep(0.1)
        self.sp_auto_delay.setDecimals(1)
        self.sp_auto_delay.setSuffix(" с")
        self.sp_auto_delay.setValue(float(self.s.auto_delay))
        self.sp_auto_delay.setToolTip("Сколько ждать перед каждым выстрелом (0.2–5 с)")
        self.sp_auto_delay.valueChanged.connect(self._auto_settings_changed)
        ar.addWidget(self.sp_auto_delay)
        gl.addLayout(ar)
        an = QLabel("Кнопка «▶ Авто» или Ctrl+Alt+A. Помощник сам щёлкает по полю соперника, "
                    "ждёт ответа сайта и стреляет дальше. Двинешь мышь — пауза. "
                    "Расставлять корабли и начинать партию нужно самому.")
        an.setObjectName("Sub")
        an.setWordWrap(True)
        gl.addWidget(an)
        lay.addWidget(g)

        g = QGroupBox("Окно")
        gl = QVBoxLayout(g)
        gl.addWidget(QLabel("Прозрачность окна:"))
        self.sl_opacity = QSlider(Qt.Horizontal)
        self.sl_opacity.setRange(35, 100)
        self.sl_opacity.setValue(int(self.s.opacity * 100))
        self.sl_opacity.valueChanged.connect(lambda v: (self.setWindowOpacity(v / 100), self._later_save()))
        gl.addWidget(self.sl_opacity)
        self.cb_hide = QCheckBox("Прятать от записи экрана")
        self.cb_hide.setToolTip("Окно помощника не попадёт в скриншоты, запись и стримы (Windows 10 2004+).\n"
                               "Заодно помощник не «видит» сам себя, если окно лежит поверх игры.")
        self.cb_hide.setChecked(self.s.hide_from_capture)
        self.cb_hide.toggled.connect(self._apply_capture_hiding)
        gl.addWidget(self.cb_hide)
        keys = "\n".join(f"Ctrl+Alt+{k} — {d}" for k, d in winutil.HOTKEYS.values())
        hk = QLabel("Горячие клавиши:\n" + keys + "\nCtrl+Z — отменить исправление")
        hk.setObjectName("Sub")
        hk.setWordWrap(True)
        gl.addWidget(hk)
        lay.addWidget(g)
        lay.addStretch(1)

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sc.setWidget(page)
        return sc

    def _tab_help(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 6, 2, 0)
        txt = QLabel(
            "<b style='color:#00f0ff'>Как пользоваться</b><br>"
            "1. Открой морской бой в браузере, чтобы было видно поле соперника.<br>"
            "2. Нажми <b>«Найти поле»</b> и щёлкни по полю соперника (или выдели рамкой).<br>"
            "3. Стреляй туда, где светится рамка: «Бей: Д5». Помощник сам видит «мимо», "
            "«ранил», «убил» и пересчитывает.<br>"
            "4. Ошибся с отметкой? Щёлкни по клетке на поле помощника — он исправится и запомнит, "
            "как такие отметки выглядят на этом сайте.<br><br>"
            "<b style='color:#00f0ff'>Как он думает</b><br>"
            "Перебирает тысячи расстановок флота, которые не противоречат полю, и считает, в скольких "
            "из них в клетке стоит корабль. Это и есть процент. При «ранил» добивает, учитывает, что "
            "корабли не касаются, и что вокруг убитого пусто.<br><br>"
            "<b style='color:#00f0ff'>Автоигра</b><br>"
            "Нажми <b>«▶ Авто»</b> (или Ctrl+Alt+A) — помощник сам будет стрелять туда, куда советует: "
            "щёлкнет по клетке, дождётся отметки сайта и выстрелит снова. Пока ход соперника, ждёт. "
            "Чтобы остановить — просто двинь мышь. Расставить корабли и начать партию нужно самому. "
            "Играть так с живыми людьми нечестно — лучше против компьютера.<br><br>"
            "<b style='color:#ff2bd6'>Честно</b>: гарантии победы нет — удача никуда не девается. "
            "Но в среднем помощнику нужно заметно меньше выстрелов, чем человеку.<br><br>"
            f"<span style='color:#7f8db0'>Версия {__version__}</span>")
        txt.setWordWrap(True)
        txt.setTextFormat(Qt.RichText)
        lay.addWidget(txt)
        b = QPushButton("Открыть тренировку")
        b.setToolTip("Встроенная партия против компьютера — проверить помощника без сайта")
        b.clicked.connect(self.open_practice)
        lay.addWidget(b)
        lay.addStretch(1)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sc.setWidget(w)
        return sc

    # ------------------------------------------------------------------ окно
    def _apply_window_settings(self):
        self.setWindowOpacity(max(0.35, min(1.0, self.s.opacity)))
        if self.s.on_top:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        geo = self.s.window
        if geo and len(geo) == 4:
            r = QRect(*geo)
            if any(sc.availableGeometry().intersects(r) for sc in QGuiApplication.screens()):
                self.setGeometry(r)
            else:
                self.resize(380, 640)
        else:
            self.resize(380, 640)
            scr = QGuiApplication.primaryScreen().availableGeometry()
            self.move(scr.right() - 400, scr.top() + 60)
        if self.s.mini:
            QTimer.singleShot(0, lambda: self.set_mini(True))

    def showEvent(self, e):
        super().showEvent(e)
        self._bind_native()

    def _bind_native(self):
        """Горячие клавиши и скрытие от захвата привязаны к HWND, а Qt пересоздаёт окно,
        например при переключении «поверх всех окон» — перепривязываем."""
        hwnd = int(self.winId())
        if hwnd != self._hotkey_hwnd:
            if self._hotkey_hwnd:
                winutil.unregister_hotkeys(self._hotkey_hwnd, self.hotkey_ids)
            self.hotkey_ids = winutil.register_hotkeys(hwnd)
            self._hotkey_hwnd = hwnd
            self._apply_capture_hiding(self.s.hide_from_capture)

    def _apply_capture_hiding(self, on):
        self.s.hide_from_capture = bool(on)
        self.capture_hidden = winutil.exclude_from_capture(int(self.winId()), bool(on))
        self._later_save()

    def nativeEvent(self, event_type, message):
        hid = winutil.hotkey_from_native(event_type, message)
        if hid is not None:
            self.on_hotkey(hid)
            return True, 0
        return super().nativeEvent(event_type, message)

    def on_hotkey(self, hid):
        if hid == 1:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.showNormal()
                self.raise_()
        elif hid == 2:
            self.cb_overlay.setChecked(not self.cb_overlay.isChecked())
        elif hid == 3:
            self.new_game()
        elif hid == 4:
            self.start_calibration()
        elif hid == 5:
            self.toggle_auto()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 40:
            h = self.windowHandle()
            if h is not None:
                h.startSystemMove()

    def moveEvent(self, e):
        super().moveEvent(e)
        self._later_save()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._later_save()

    def set_on_top(self, on):
        self.s.on_top = bool(on)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        self.show()
        self._bind_native()
        self._apply_capture_hiding(self.s.hide_from_capture)
        self._later_save()

    def set_mini(self, on):
        self.s.mini = bool(on)
        self.card.setVisible(bool(on) or self.tabs.currentIndex() == 0)
        self.tabs.setVisible(not on)
        self.status_row.setVisible(not on)
        self.grip.setVisible(not on)
        self.hint_alt.setVisible(not on)
        if on:
            self._full_geom = self.geometry()
            self.setMinimumSize(240, 60)
            w = 350
            lay = self.layout()
            lay.activate()
            h = lay.totalHeightForWidth(w) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
            self.resize(w, max(80, h))
        else:
            self.setMinimumSize(300, 150)
            g = getattr(self, "_full_geom", None)
            if g is not None:
                self.resize(g.size())
            else:
                self.resize(380, 640)
        self._later_save()

    def _later_save(self):
        if hasattr(self, "save_timer"):
            self.save_timer.start(800)

    def save_settings(self):
        if not self.s.mini:
            g = self.geometry()
            self.s.window = [g.x(), g.y(), g.width(), g.height()]
        if self.tracker is not None:
            self.s.examples = self.tracker.export_examples()
        self.s.save()

    def closeEvent(self, e):
        self.timer.stop()
        self.auto_timer.stop()
        self.save_settings()
        winutil.unregister_hotkeys(self._hotkey_hwnd or int(self.winId()), self.hotkey_ids)
        self.worker.stop()
        for w in (self.overlay, self.ghost, self.debug, self.practice):
            if w is not None:
                w.close()
        super().closeEvent(e)
        QApplication.quit()

    # ------------------------------------------------------------------ статус
    def set_status(self, kind, text):
        colors = {"ok": theme.GREEN, "idle": theme.MUTED, "warn": theme.AMBER,
                  "lost": theme.RED, "pause": theme.AMBER}
        self.status_dot.setStyleSheet(f"color:{colors.get(kind, theme.MUTED)};")
        self.status.setText(text)

    # ------------------------------------------------------------------ состояния и анализ
    @property
    def calibrated(self):
        return self.tracker is not None and self.enemy_geom is not None

    def current_states(self):
        if self.calibrated:
            return self.tracker.states.copy()
        return self.manual_states.copy()

    def refresh_states(self, analyze_now=True):
        st = self.current_states()
        self.view.set_attack(st, self.analysis if self.analysis is not None and
                             self.analysis.states.shape == st.shape else None)
        if analyze_now:
            jid = self.worker.submit(st, self.rules, self.s.budget)
            self._submitted = {jid: st}

    def on_analysis(self, a, jid):
        if jid != self.worker.latest:
            return
        self.analysis = a
        self.analysis_src = self._submitted.get(jid, self.analysis_src)
        st = self.current_states()
        if a.states.shape != st.shape:
            return
        self.view.set_attack(st, a)
        letters, lay = self.s.letters, self.labels_layout
        if a.mode == "done":
            self.hint.setText("Победа! 🎉")
            self.hint_pct.setText("")
            self.hint_sub.setText("Все корабли соперника потоплены. «Новая партия» — сыграть ещё.")
            self.hint_alt.setText("")
        elif a.best is None:
            self.hint.setText("—")
            self.hint_pct.setText("")
            self.hint_sub.setText("Нет подходящих клеток — проверь отметки на поле.")
            self.hint_alt.setText("")
        else:
            r, c = a.best
            name = coord_name(r, c, letters, lay)
            # буква другим цветом: русская «З» в крупном шрифте похожа на тройку («З4» ≠ «34»)
            self.hint.setText(f"Бей: <span style='color:#ffffff'>{name[0]}</span>{name[1:]}")
            self.hint_pct.setText(f"{a.best_prob * 100:.0f}%")
            if a.mode == "target":
                lens = sorted(a.focus_lengths.items(), key=lambda t: -t[1])
                guess = ""
                if lens:
                    L, pL = lens[0]
                    guess = f" · скорее всего {SHIP_NAMES.get(L, str(L))} ({pL * 100:.0f}%)"
                self.hint_sub.setText("Добиваем раненого" + guess)
            else:
                big = max(a.remaining) if a.remaining else 0
                self.hint_sub.setText(f"Ищем корабли · самый большой оставшийся — "
                                      f"{SHIP_NAMES.get(big, str(big))}")
            alts = [f"{coord_name(rr, cc, letters, lay)} {p * 100:.0f}%" for rr, cc, p in a.top[1:4]]
            self.hint_alt.setText(("Ещё: " + " · ".join(alts)) if alts else "")
        notes = list(a.issues) + list(a.relaxed)
        if notes:
            self.warn.setText("⚠ " + notes[0])
            self.warn.show()
        else:
            self.warn.hide()
        self.fleet_bar.set_data(self.rules.fleet, a.remaining)
        shots = int((st != UNKNOWN).sum())
        hits = int(((st == HIT) | (st == SUNK)).sum())
        left = sum(a.remaining) - int((a.states == HIT).sum())
        self.stats.setText(f"Открыто клеток: {shots} · попаданий: {hits} · палуб осталось: {left}")
        self.update_overlay()

    # ------------------------------------------------------------------ ручная разметка
    def _push_history(self):
        self.history.append(self.current_states())
        self.history = self.history[-200:]

    def set_cell(self, r, c, val, whole_ship=False):
        self._push_history()
        cells = [(r, c)]
        if whole_ship and val == SUNK:
            st = self.current_states()
            mask = (st == HIT) | (st == SUNK)
            mask[r, c] = True
            for comp in components(mask):
                if (r, c) in comp:
                    cells = comp
                    break
        for rr, cc in cells:
            if self.calibrated:
                self.tracker.learn_from_correction(rr, cc, val)
            else:
                self.manual_states[rr, cc] = val
        if self.calibrated:
            self.s.examples = self.tracker.export_examples()
            self._later_save()
        self.refresh_states()

    def cycle_cell(self, r, c):
        st = self.current_states()
        nxt = {UNKNOWN: MISS, MISS: HIT, HIT: SUNK, SUNK: UNKNOWN}[int(st[r, c])]
        self.set_cell(r, c, nxt, whole_ship=(nxt == SUNK))

    def cell_menu(self, r, c, gpos):
        m = QMenu(self)
        name = coord_name(r, c, self.s.letters, self.labels_layout)
        m.addSection(name)
        acts = [("Мимо", MISS, False), ("Ранил", HIT, False), ("Убил (весь корабль)", SUNK, True),
                ("Пусто — не стреляли", UNKNOWN, False)]
        for text, val, whole in acts:
            a = m.addAction(text)
            a.triggered.connect(lambda _=False, v=val, w=whole: self.set_cell(r, c, v, w))
        m.exec(gpos)

    def undo(self):
        if not self.history:
            return
        prev = self.history.pop()
        if self.calibrated:
            self.tracker.states = prev.copy()
            self.tracker.pending[:] = -1
            self.tracker.pending_cnt[:] = 0
        else:
            self.manual_states = prev.copy()
        self.refresh_states()

    def new_game(self):
        self._push_history()
        if self.calibrated:
            self.tracker.new_game()
        self.manual_states[:] = UNKNOWN
        self.analysis = None
        self.refresh_states()
        self.set_status("ok" if self.calibrated else "idle",
                        "Новая партия. " + ("Слежу за полем." if self.calibrated else
                                            "Размечай выстрелы на поле помощника или найди поле на экране."))

    # ------------------------------------------------------------------ калибровка
    def _windows_to_hide(self):
        ws = []
        if not self.capture_hidden:
            ws.append(self)
        for o in (self.overlay, self.ghost, self.debug):
            if o.isVisible() and not getattr(o, "capture_hidden", False):
                ws.append(o)
        return ws

    def start_calibration(self):
        if self.autopilot.active:
            self.stop_auto("Автоигра выключена: ищем поле заново.")
        hidden = self._windows_to_hide()
        for w in hidden:
            w.hide()
        QTimer.singleShot(300 if hidden else 30, lambda: self._calibrate_grab(hidden))

    def _screen_for_calibration(self):
        c = self.frameGeometry().center()
        scr = QGuiApplication.screenAt(c) or QGuiApplication.primaryScreen()
        # если помощник на одном экране, а игра — на другом, берём экран с найденными полями
        return scr

    def _calibrate_grab(self, hidden):
        best = None
        cand_screens = [self._screen_for_calibration()] + [s for s in QGuiApplication.screens()
                                                           if s is not self._screen_for_calibration()]
        for scr in cand_screens:
            img = capture.grab_screen(scr)
            arr = capture.qimage_to_rgb(img)
            grids = find_grids(arr, n=self.rules.size)
            if best is None or len(grids) > len(best[2]):
                best = (scr, img, grids)
            if grids:
                break
        for w in hidden:
            if w is self:
                w.show()
        scr, img, grids = best
        self.picker = RegionPicker(scr, img, grids, self.rules.size)
        self.picker.picked.connect(lambda e, m: self.on_picked(scr, img, e, m))
        self.picker.cancelled.connect(lambda: self._after_picker(hidden))
        self.picker.start()

    def _after_picker(self, hidden):
        for w in hidden:
            if w is self:
                w.show()
        self.update_overlay()

    def on_picked(self, scr, img, enemy, my):
        self._after_picker([self])
        self.apply_calibration(scr, enemy, my, img)
        self.s.screen_name = scr.name()
        self.s.enemy_geom = enemy.to_dict()
        self.s.my_geom = my.to_dict() if my is not None else None
        self.s.first_run = False
        self.save_settings()

    def restore_calibration(self):
        scr = capture.find_screen(self.s.screen_name)
        try:
            enemy = GridGeom.from_dict(self.s.enemy_geom)
            my = GridGeom.from_dict(self.s.my_geom) if self.s.my_geom else None
        except Exception:
            return
        if enemy.n != self.rules.size:
            return
        self.apply_calibration(scr, enemy, my, None)
        self.set_status("ok", "Слежу за полем с прошлого раза. Если оно сдвинулось — «Найти поле».")

    def apply_calibration(self, scr, enemy, my, img=None):
        self.screen_ = scr
        self.enemy_geom = enemy
        self.my_geom = my
        self.tracker = BoardTracker(enemy.n)
        self.tracker.sensitivity = self.s.sensitivity
        self.tracker.detect_sunk_look = self.s.detect_sunk_look
        self.tracker.import_examples(self.s.examples)
        self.signature = None
        self.lost = 0
        self.history.clear()
        self.analysis = None
        self.btn_forget.setText(f"Сбросить обучение ({len(self.tracker.examples)})")
        self.last_status = None
        self.label_layout_detected = None
        self._labels_pending = True
        if img is not None:
            self._labels_pending = False
            self._set_detected_layout(self._detect_labels(img, enemy))
        self.set_status("ok", "Поле найдено — слежу за ним. Стреляй туда, где рамка.")
        if not self.paused:
            self.timer.start(self.s.interval_ms)
        self.capture_tick()
        self.update_ghost(self.btn_ghost.isChecked())

    def swap_boards(self):
        if self.enemy_geom is None or self.my_geom is None:
            QMessageBox.information(self, "Поменять поля", "Сначала найди оба поля кнопкой «Найти поле».")
            return
        self.apply_calibration(self.screen_, self.my_geom, self.enemy_geom)
        self.s.enemy_geom, self.s.my_geom = self.enemy_geom.to_dict(), self.my_geom.to_dict()
        self.save_settings()

    def forget_examples(self):
        self.s.examples = []
        if self.tracker is not None:
            self.tracker.examples = []
            self.tracker.proto_hit, self.tracker.proto_sunk = [], []
        self.btn_forget.setText("Сбросить обучение (0)")
        self.save_settings()

    # ------------------------------------------------------------------ захват
    def set_paused(self, on):
        self.paused = bool(on)
        if on:
            self.timer.stop()
            self.set_status("pause", "Пауза: экран не читаю. Отметки можно ставить вручную.")
        elif self.calibrated:
            self.timer.start(self.s.interval_ms)
            self.set_status("ok", "Слежу за полем.")

    def _grab(self):
        if self.frame_source is not None:
            return self.frame_source()
        return capture.grab_screen(self.screen_)

    def cursor_cell(self):
        if self.frame_source is not None or self.last_frame is None:
            return None
        smap = capture.ScreenMap(self.screen_, (self.last_frame.width(), self.last_frame.height()))
        x, y = smap.to_image(QCursor.pos())
        return self.enemy_geom.cell_at(x, y)

    def capture_tick(self):
        if not self.calibrated:
            return
        t0 = time.perf_counter()
        img = self._grab()
        if img is None or img.isNull():
            return
        self.last_frame = img
        g = self.enemy_geom
        m = int(max(g.cw, g.ch)) + 2
        crop = QRect(int(g.x0) - m, int(g.y0) - m, int(g.width) + 2 * m, int(g.height) + 2 * m)
        crop = crop.intersected(img.rect())
        if crop.width() < g.width * 0.8 or crop.height() < g.height * 0.8:
            self._board_lost("Поле вышло за край экрана.")
            return
        arr = capture.qimage_to_rgb(img, crop)
        local = g.shifted(-crop.x(), -crop.y())
        sig = border_signature(arr, local)
        # где рамка-подсказка могла попасть в снимок (если Windows не прячет её от захвата)
        drawn = set(self._overlay_cells)
        if self.overlay.isVisible() and self.overlay.best is not None:
            drawn.add(tuple(self.overlay.best))
        if self.signature is None:
            self.signature, self._signature_cells = sig, drawn
        else:
            match = signature_match(self.signature, sig, ignore=drawn | self._signature_cells)
            if match < 0.55:
                self._board_lost("Поле не на прежнем месте (прокрутка, другое окно сверху?).")
                return
            if match > 0.8:
                # отметки копятся и понемногу меняют вид линий (кляксы и крестики на Яндексе
                # залезают на клетчатые линии) — сравниваем с недавним видом, а не с самым первым
                self.signature, self._signature_cells = sig, drawn
        if self.lost:
            self.lost = 0
            self.set_status("ok", "Поле снова видно — слежу.")
        cover = self._own_window_over_board() is not None
        if cover != self._cover_warned:
            self._cover_warned = cover
            if cover:
                self.set_status("warn", "Окно помощника лежит на поле соперника — отодвинь его, "
                                        "чтобы поле было видно целиком.")
            else:
                self.set_status("ok", "Слежу за полем.")
        res = self.tracker.process(extract_patches(arr, local), cursor=self.cursor_cell())
        self.last_status = res.status
        self.last_crop = (img.copy(crop), local)
        if self._labels_pending:
            self._labels_pending = False
            self._set_detected_layout(self._detect_labels(img, g))
        if res.status in ("reinit", "newgame"):
            if self.autopilot.active:
                self.stop_auto("Автоигра выключена: на сайте новая партия или поле сильно изменилось.")
            self.history.clear()
            self.signature = sig
            self.set_status("ok", "Новая партия на сайте — начинаю заново." if res.status == "newgame"
                            else "Поле сильно изменилось — перечитал его заново.")
        if res.changed:
            self.refresh_states()
        if self.debug.isVisible():
            dt = (time.perf_counter() - t0) * 1000
            mode = {"ok": "норма", "lit": "поле затемнено/подсвечено", "unstable": "сильные изменения",
                    "init": "первый кадр", "reinit": "перечитал", "newgame": "новая партия"}.get(res.status, res.status)
            self.debug.update_view(self.last_crop[0], local, self.tracker.states,
                                   f"Состояние: {mode}. Кадр обработан за {dt:.0f} мс. "
                                   f"Образцов обучения: {len(self.tracker.examples)}.")
        self.update_overlay()

    def _own_window_over_board(self):
        """Какое окно помощника лежит на поле соперника (None — никакое)."""
        rect = self._board_rect_logical(self.enemy_geom) if self.frame_source is None else None
        if rect is None:
            return None
        r = rect.toAlignedRect()
        for w in (self, self.debug):
            if w.isVisible() and not w.isMinimized() and w.frameGeometry().intersects(r):
                return w
        return None

    def _board_lost(self, why):
        if self._own_window_over_board() is not None:
            why = "Окно помощника лежит на поле соперника — отодвинь его в сторону."
        self.lost += 1
        if self.lost >= 3:
            self.set_status("lost", why + " Ищу поле…")
            self.overlay.hide()
            now = time.time()
            if now - self.last_relocate > 2.0 and self.frame_source is None:
                self.last_relocate = now
                self._relocate()

    def _relocate(self):
        img = self.last_frame
        if img is None:
            return
        grids = find_grids(capture.qimage_to_rgb(img), n=self.rules.size)
        g0 = self.enemy_geom
        same = [g for g in grids if abs(g.cw - g0.cw) < 0.08 * g0.cw]
        # среди похожих — ближайшее к прежнему месту поля соперника (своё поле обычно того же
        # размера и рядом: на Яндексе оно слева, и брать «первое слева» нельзя)
        same.sort(key=lambda g: abs(g.x0 - g0.x0) + abs(g.y0 - g0.y0))
        if same:
            dx, dy = same[0].x0 - g0.x0, same[0].y0 - g0.y0
            self.enemy_geom = same[0]
            if self.my_geom is not None:
                self.my_geom = self.my_geom.shifted(dx, dy)
            self.signature = None
            self.lost = 0
            self.s.enemy_geom = self.enemy_geom.to_dict()
            self._later_save()
            self.set_status("ok", "Поле нашлось на новом месте — продолжаю.")

    # ------------------------------------------------------------------ оверлей
    def _board_rect_logical(self, geom):
        if self.screen_ is None or self.last_frame is None or geom is None:
            return None
        smap = capture.ScreenMap(self.screen_, (self.last_frame.width(), self.last_frame.height()))
        return smap.rect_to_logical(geom.x0, geom.y0, geom.x0 + geom.width, geom.y0 + geom.height)

    def update_overlay(self):
        show = (self.s.overlay and self.calibrated and not self.paused and not self.lost
                and self.frame_source is None and self.analysis is not None)
        if not show:
            self.overlay.hide()
            return
        rect = self._board_rect_logical(self.enemy_geom)
        if rect is None:
            return
        self.overlay.place(rect, self.enemy_geom.n)
        self.overlay.set_attack(self.analysis, self.current_states(), heat=self.s.overlay_heat)
        if self.overlay.best is not None:
            cell = tuple(self.overlay.best)
            if cell in self._overlay_cells:
                self._overlay_cells.remove(cell)
            self._overlay_cells = (self._overlay_cells + [cell])[-3:]
        if not self.overlay.isVisible():
            self.overlay.show()

    def update_ghost(self, on):
        on = bool(on)
        if on and self.my_geom is None:
            if self.btn_ghost.isChecked():
                self.btn_ghost.setChecked(False)
            QMessageBox.information(self, "Своё поле",
                                    "Сначала укажи своё поле: «Найти поле» → после поля соперника "
                                    "щёлкни по своему полю.")
            return
        if not on:
            self.ghost.hide()
            return
        if self.last_frame is None and self.screen_ is not None:
            self.last_frame = capture.grab_screen(self.screen_)
        rect = self._board_rect_logical(self.my_geom)
        if rect is None:
            return
        self.ghost.place(rect, self.my_geom.n)
        self.ghost.set_ships(self.placement_ships)
        self.ghost.show()

    # ------------------------------------------------------------------ расстановка
    def regenerate_placement(self, strategy):
        self.s.placement_strategy = strategy
        for b in self.place_group.buttons():
            b.setChecked(b.property("strategy") == strategy)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ships = placement.generate(strategy, self.rules, self.placement_rng)
            score = placement.predictability(ships, self.rules)
        finally:
            QApplication.restoreOverrideCursor()
        self.placement_ships = ships
        self._placement_dirty = False
        self.place_view.set_ships(ships)
        self.place_text.setText(placement.STRATEGIES[strategy][1])
        verdict = "низкая — хорошо против ботов" if score < 35 else (
            "высокая: бот найдёт быстрее, люди — нет" if score > 65 else "средняя")
        self.place_score.setText(f"Предсказуемость для бота: <b>{score:.0f}</b>/100 — {verdict}")
        self.place_list.setText(placement.describe_short(ships, self.s.letters, self.labels_layout))
        self.place_list.setToolTip("\n".join(placement.describe(ships, self.s.letters, self.labels_layout)))
        if self.btn_ghost.isChecked():
            self.ghost.set_ships(ships)
        self._later_save()

    def copy_placement(self):
        QGuiApplication.clipboard().setText("\n".join(placement.describe(self.placement_ships, self.s.letters,
                                                                          self.labels_layout)))
        self.place_score.setText(self.place_score.text().split("  ✓")[0] + "  ✓ скопировано")

    # ------------------------------------------------------------------ настройки
    def _sync_preset_combo(self):
        cur = Rules(size=self.s.size, fleet=tuple(self.s.fleet), touch=self.s.touch,
                    sunk_marked=self.s.sunk_marked) if True else None
        idx = self.cmb_preset.count() - 1
        for i in range(self.cmb_preset.count() - 1):
            key = self.cmb_preset.itemData(i)
            r = PRESETS[key][1]
            if (r.size, r.fleet, r.touch) == (cur.size, cur.fleet, cur.touch):
                idx = i
        self.cmb_preset.setCurrentIndex(idx)

    def _preset_chosen(self, i):
        key = self.cmb_preset.itemData(i)
        if key == "custom":
            return
        r = PRESETS[key][1]
        self.ed_fleet.setText(fleet_to_text(r.fleet))
        self.sp_size.blockSignals(True)
        self.sp_size.setValue(r.size)
        self.sp_size.blockSignals(False)
        for b in self.touch_group.buttons():
            b.setChecked(b.property("touch") == r.touch)
        self._rules_edited()

    def _rules_edited(self):
        try:
            fleet = parse_fleet(self.ed_fleet.text())
            touch = next(b.property("touch") for b in self.touch_group.buttons() if b.isChecked())
            rules = Rules(size=self.sp_size.value(), fleet=fleet, touch=touch,
                          sunk_marked=self.cb_sunk.isChecked())
        except (ValueError, StopIteration) as ex:
            self.ed_fleet.setStyleSheet(f"border-color:{theme.RED};")
            self.ed_fleet.setToolTip(f"Ошибка: {ex}")
            return
        self.ed_fleet.setStyleSheet("")
        size_changed = rules.size != self.rules.size
        self.rules = rules
        self.s.size, self.s.fleet, self.s.touch, self.s.sunk_marked = (
            rules.size, list(rules.fleet), rules.touch, rules.sunk_marked)
        self._sync_preset_combo()
        if size_changed:
            self.timer.stop()
            self.tracker = None
            self.enemy_geom = self.my_geom = None
            self.s.enemy_geom = self.s.my_geom = None
            self.manual_states = np.zeros((rules.size, rules.size), np.int8)
            self.view.set_size(rules.size)
            self.place_view.set_size(rules.size)
            self.overlay.hide()
            self.set_status("idle", "Размер поля изменился — найди поле заново.")
        self.analysis = None
        self.refresh_states()
        self._placement_changed_rules()
        self._later_save()

    def _letters_changed(self, _i):
        self.s.letters = self.cmb_letters.currentData()
        for v in (self.view, self.place_view):
            v.letters = self.s.letters
        self.overlay.letters = self.ghost.letters = self.s.letters
        self._refresh_names()
        self._later_save()

    def _refresh_names(self):
        """Перерисовать всё, где есть названия клеток (буквы/подписи поменялись)."""
        lay = self.labels_layout
        for v in (self.view, self.place_view):
            v.label_layout = lay
            v.update()
        self.overlay.label_layout = self.ghost.label_layout = lay
        self.overlay.update()
        if self.analysis is not None:
            self.on_analysis(self.analysis, self.worker.latest)
        if self.placement_ships:
            self.place_list.setText(placement.describe_short(self.placement_ships, self.s.letters, lay))
            self.place_list.setToolTip("\n".join(placement.describe(self.placement_ships, self.s.letters, lay)))

    @property
    def labels_layout(self):
        if self.s.labels in (LETTERS_TOP, NUMBERS_TOP):
            return self.s.labels
        return self.label_layout_detected or LETTERS_TOP

    def _labels_changed(self, _i):
        self.s.labels = self.cmb_labels.currentData()
        self._refresh_names()
        self._update_labels_hint()
        self._later_save()

    def _update_labels_hint(self):
        if not hasattr(self, "cmb_labels"):
            return
        det = {LETTERS_TOP: "буквы сверху", NUMBERS_TOP: "цифры сверху"}.get(self.label_layout_detected)
        self.cmb_labels.setItemText(0, f"Как на сайте ({det})" if det else "Как на сайте (сам)")

    def _detect_labels(self, img, geom):
        """Посмотреть на подписи вокруг поля: где буквы, где цифры (Яндекс Игры — цифры сверху)."""
        try:
            m = int(max(geom.cw, geom.ch) * 1.3) + 2
            rect = QRect(int(geom.x0) - m, int(geom.y0) - m, int(geom.width) + 2 * m, int(geom.height) + 2 * m)
            rect = rect.intersected(img.rect())
            arr = capture.qimage_to_rgb(img, rect)
            found = detect_label_layout(arr, geom.shifted(-rect.x(), -rect.y()))
        except Exception:
            found = None
        if found is None and self.my_geom is not None and geom is not self.my_geom:
            return self._detect_labels(img, self.my_geom)
        return found

    def _set_detected_layout(self, found):
        if found is not None and found != self.label_layout_detected:
            self.label_layout_detected = found
            self._refresh_names()
        self._update_labels_hint()

    def _recog_changed(self, *_):
        self.s.sensitivity = self.sl_sens.value() / 100
        self.s.interval_ms = self.sp_interval.value()
        self.s.detect_sunk_look = self.cb_sunklook.isChecked()
        if self.tracker is not None:
            self.tracker.sensitivity = self.s.sensitivity
            self.tracker.detect_sunk_look = self.s.detect_sunk_look
        if self.timer.isActive():
            self.timer.start(self.s.interval_ms)
        self._later_save()

    def _hint_settings_changed(self, *_):
        self.s.budget = self.sl_budget.value() / 10
        self.s.overlay = self.cb_overlay.isChecked()
        self.s.overlay_heat = self.cb_heat.isChecked()
        self.update_overlay()
        self._later_save()

    def _tab_changed(self, i):
        self.card.setVisible(i == 0 or self.s.mini)
        if i == 1 and self._placement_dirty:
            self.regenerate_placement(self.s.placement_strategy)
        if i == 1 and self.btn_ghost.isChecked():
            self.update_ghost(True)

    def _placement_changed_rules(self):
        if self.tabs.currentIndex() == 1:
            self.regenerate_placement(self.s.placement_strategy)
        else:
            self._placement_dirty = True

    # ------------------------------------------------------------------ прочее
    def show_debug(self):
        if not self.debug.isVisible():
            # рядом с помощником, а не посреди экрана (там обычно и лежит поле игры)
            fg = self.frameGeometry()
            avail = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
            w, h = self.debug.width(), self.debug.height()
            x = fg.left() - w - 12 if fg.left() - w - 12 >= avail.left() else fg.right() + 12
            x = max(avail.left(), min(x, avail.right() - w))
            y = max(avail.top(), min(fg.top(), avail.bottom() - h))
            self.debug.move(x, y)
        self.debug.show()
        self.debug.raise_()
        if self.last_crop is not None:
            self.debug.update_view(self.last_crop[0], self.last_crop[1], self.tracker.states, "")
        elif not self.calibrated:
            self.debug.info.setText("Поле ещё не найдено — нажми «Найти поле».")

    def open_practice(self):
        if self.practice is None:
            self.practice = PracticeWindow(self.rules, self.s.letters)
        self.practice.show()
        self.practice.raise_()

    # ------------------------------------------------------------------ автоигра
    def _auto_toggled(self, on):
        if on and not self.autopilot.active:
            self.start_auto()
        elif not on and self.autopilot.active:
            self.stop_auto("Автоигра выключена.")

    def toggle_auto(self):
        if self.autopilot.active:
            self.stop_auto("Автоигра выключена.")
        else:
            self.start_auto()

    def start_auto(self):
        why = ""
        if not self.mouse.available:
            why = self.mouse.why
        elif not self.calibrated or self.frame_source is not None:
            why = "Сначала найди поле соперника: кнопка «Найти поле» (Ctrl+Alt+R)."
        if why:
            self._sync_auto_ui()
            self.auto_info.setText(why)
            self.auto_info.show()
            return False
        if self.paused:
            self.btn_pause.setChecked(False)
        self.autopilot.delay = self.s.auto_delay
        self.autopilot.start(time.monotonic())
        self._auto_cursor = None
        self.auto_timer.start()
        self._sync_auto_ui()
        return True

    def stop_auto(self, note=""):
        self.autopilot.stop(note)
        self.auto_timer.stop()
        self._auto_cursor = None
        self._sync_auto_ui()

    def _auto_settings_changed(self, *_):
        self.s.auto_delay = round(float(self.sp_auto_delay.value()), 1)
        self.autopilot.delay = self.s.auto_delay
        self._later_save()

    def _sync_auto_ui(self, now=None):
        ap = self.autopilot
        self.btn_auto.blockSignals(True)
        self.btn_auto.setChecked(ap.active)
        self.btn_auto.blockSignals(False)
        self.btn_auto.setText("■ Стоп" if ap.active else "▶ Авто")
        if ap.active:
            now = time.monotonic() if now is None else now
            left = ap.countdown(now)
            if left > 0:
                text = f"Автоигра: первый выстрел через {left:.0f} с — убери руку с мыши."
            else:
                res = ap.results
                text = (f"Автоигра · выстрелов {ap.shots}: ранил {res[HIT]}, убил {res[SUNK]}, мимо {res[MISS]}"
                        f" · {ap.note}  Двинь мышь — пауза.")
            self.auto_info.setText(text)
            self.auto_info.show()
        elif ap.note:
            self.auto_info.setText(ap.note)
            self.auto_info.show()
        else:
            self.auto_info.hide()

    def _auto_seen(self):
        if not self.calibrated or self.last_frame is None:
            return Seen(ready=False, states=None)
        st = self.tracker.states.copy()
        a = self.analysis
        fresh = (a is not None and self.analysis_src is not None and a.states.shape == st.shape
                 and np.array_equal(self.analysis_src, st))
        busy = bool((self.tracker.pending >= 0).any()) or self.last_status not in ("ok",)
        now = time.monotonic()
        if not busy:
            self._busy_since = None
        elif self._busy_since is None:
            self._busy_since = now
        elif now - self._busy_since > 4.0:
            busy = False                     # «вечная» анимация на поле не должна остановить игру навсегда
        ready = not self.paused and not self.lost and self.frame_source is None
        stuck = ""
        if fresh and a.best is None and a.mode != "done":
            stuck = "решатель не находит подходящих клеток — проверь отметки на поле помощника."
        return Seen(ready=ready, states=st, best=a.best if fresh else None, fresh=fresh,
                    done=bool(fresh and a.mode == "done"), busy=busy, stuck=stuck)

    def auto_tick(self):
        ap = self.autopilot
        if not ap.active:
            self.auto_timer.stop()
            self._sync_auto_ui()
            return
        now = time.monotonic()
        if self._auto_cursor is not None:
            p = self.mouse.pos()
            if p is not None and max(abs(p[0] - self._auto_cursor[0]), abs(p[1] - self._auto_cursor[1])) > 8:
                self.stop_auto("Ты двинул мышь — автоигра на паузе. Продолжить: «▶ Авто» или Ctrl+Alt+A.")
                return
        if self.lost >= 15:
            self.stop_auto("Автоигра выключена: поле давно не видно.")
            return
        cell = ap.step(now, self._auto_seen())
        if cell is not None:
            self._auto_click(*cell)
        if ap.active:
            self._sync_auto_ui(now)
        else:
            self.stop_auto(ap.note)

    def _own_windows_at(self, lp):
        for w in (self, self.debug):
            if w.isVisible() and not w.isMinimized() and w.frameGeometry().contains(lp):
                return w
        return None

    def _auto_click(self, r, c):
        g = self.enemy_geom
        smap = capture.ScreenMap(self.screen_, (self.last_frame.width(), self.last_frame.height()))
        x, y = g.cell_center(r, c)
        lp = smap.to_logical(x, y).toPoint()
        if self._own_windows_at(lp) is not None:
            self.stop_auto("Окно помощника закрывает поле соперника — отодвинь его и включи автоигру снова.")
            return False
        nx, ny = smap.to_native(x, y)
        orig = self.mouse.pos()
        if self.mouse.real:
            # сверка: курсор, поставленный по физическим координатам, глазами Qt должен оказаться
            # над той же клеткой (иначе что-то с масштабом экрана — лучше не щёлкать вслепую)
            self.mouse.move(nx, ny)
            qx, qy = smap.to_image(QCursor.pos())
            err = max(abs(qx - x), abs(qy - y))
            if err > max(4.0, 0.3 * min(g.cw, g.ch)):
                if orig is not None:
                    self.mouse.move(*orig)
                self.stop_auto(f"Не получается точно навести мышь на клетку (промах {err:.0f} px). "
                               "Напиши разработчику, какой у тебя масштаб экрана в Windows.")
                return False
        ok = self.mouse.click(nx, ny)
        park = self._auto_park(smap, orig)
        if park is not None:
            self.mouse.move(*park)
        self._auto_cursor = self.mouse.pos() or park or (nx, ny)
        self.auto_log.append((round(time.monotonic(), 2), (r, c), (nx, ny), bool(ok)))
        self.auto_log = self.auto_log[-300:]
        if not ok:
            self.stop_auto("Windows не дала щёлкнуть мышью (окно игры запущено от администратора?).")
        return ok

    def _auto_park(self, smap, orig):
        """Куда убрать курсор после выстрела: туда, где он был, если это не над полем соперника
        (над полем курсор мешает распознаванию), иначе — рядом с полем."""
        g = self.enemy_geom
        box = (g.x0 - 0.5 * g.cw, g.y0 - 0.5 * g.ch, g.x0 + g.width + 0.5 * g.cw, g.y0 + g.height + 0.5 * g.ch)

        def over_board(ix, iy):
            return box[0] <= ix <= box[2] and box[1] <= iy <= box[3]

        if orig is not None:
            ix, iy = smap.native_to_image(*orig)
            if not over_board(ix, iy):
                return orig
        w, h = self.last_frame.width(), self.last_frame.height()
        cx, cy = g.x0 + g.width / 2, g.y0 + g.height / 2
        for ix, iy in ((g.x0 + g.width + 1.5 * g.cw, cy), (g.x0 - 1.5 * g.cw, cy),
                       (cx, g.y0 + g.height + 1.5 * g.ch), (cx, g.y0 - 1.5 * g.ch)):
            if 0 <= ix < w and 0 <= iy < h and not over_board(ix, iy):
                return smap.to_native(ix, iy)
        return None
