"""Автоигра: логика «когда и куда щёлкнуть» и сквозная проверка с окнами Qt."""
import os
import sys
import time

import numpy as np
import pytest

from seabattle.autoplay import AutoPilot, Seen
from seabattle.placement import describe
from seabattle.rules import HIT, LETTERS_TOP, MISS, NUMBERS_TOP, SUNK, UNKNOWN, coord_name


def board(**marks):
    st = np.full((10, 10), UNKNOWN, np.int8)
    for (r, c), v in marks.items():
        st[r, c] = v
    return st


def seen(st, best=(4, 4), **kw):
    d = dict(ready=True, states=st, best=best, fresh=True, done=False, busy=False)
    d.update(kw)
    return Seen(**d)


def started(**kw):
    ap = AutoPilot(delay=0.5, wait_result=2.0, retry=1.0, give_up=10.0, first_delay=1.0, **kw)
    ap.start(0.0)
    return ap


# ---------------------------------------------------------------------------- логика
def test_countdown_then_shot_then_waits_for_mark():
    ap = started()
    st = board()
    assert ap.step(0.5, seen(st)) is None             # ещё идёт отсчёт — человек убирает руку
    assert ap.countdown(0.5) == pytest.approx(0.5)
    assert ap.step(1.0, seen(st)) == (4, 4)
    assert ap.step(1.1, seen(st)) is None              # ждём отметку сайта, второй раз не щёлкаем
    st[4, 4] = HIT
    assert ap.step(1.5, seen(st, best=(4, 5))) is None  # увидели «ранил», пауза delay
    assert ap.results[HIT] == 1 and ap.shots == 1
    assert ap.step(1.8, seen(st, best=(4, 5))) is None
    assert ap.step(2.0, seen(st, best=(4, 5))) == (4, 5)


def test_never_shoots_when_not_ready_busy_or_stale():
    ap = started()
    st = board()
    assert ap.step(5, seen(st, ready=False)) is None    # поле потеряно / пауза
    assert ap.step(5, seen(st, busy=True)) is None      # на поле что-то меняется, чужой ход
    assert ap.step(5, seen(st, fresh=False)) is None    # подсказка посчитана для старого поля
    assert ap.step(5, seen(st, best=None)) is None
    st[4, 4] = MISS
    assert ap.step(5, seen(st)) is None                 # советует уже открытую клетку — не верим
    assert ap.clicks == 0 and ap.active


def test_no_answer_retries_slower_and_slower_then_gives_up():
    ap = started()
    st = board()
    t = 1.0
    assert ap.step(t, seen(st)) == (4, 4)
    clicks = [t]
    while ap.active and t < 30:
        t = round(t + 0.1, 1)
        if ap.step(t, seen(st)) is not None:
            clicks.append(t)
    assert not ap.active and "не отмечает" in ap.note
    assert 1.0 + 10.0 <= t <= 1.0 + 10.0 + 2.0 + 5.0    # сдался после give_up (+ ожидание и пауза)
    gaps = [b - a for a, b in zip(clicks, clicks[1:])]
    assert len(clicks) >= 3 and gaps == sorted(gaps)     # повторяет всё реже
    assert gaps[0] == pytest.approx(2.0 + 1.0, abs=0.15)  # wait_result + retry


def test_waiting_note_mentions_opponent_turn():
    ap = started()
    st = board()
    ap.step(1.0, seen(st))
    ap.step(3.1, seen(st))
    assert "ход соперника" in ap.note and ap.active


def test_answer_after_retry_resets_counter():
    ap = started()
    st = board()
    ap.step(1.0, seen(st))
    ap.step(3.1, seen(st))
    assert ap.step(4.2, seen(st)) == (4, 4)
    st[4, 4] = MISS
    ap.step(4.5, seen(st, best=(0, 0)))
    assert ap.tries == 0 and ap.results[MISS] == 1


def test_stops_on_victory_and_on_contradiction():
    ap = started()
    st = board()
    ap.step(1.0, seen(st, done=True, best=None))
    assert not ap.active and "Победа" in ap.note
    ap = started()
    ap.step(1.0, seen(st, best=None, stuck="решатель не находит клеток"))
    assert not ap.active and "решатель" in ap.note


def test_sunk_result_counts():
    ap = started()
    st = board()
    ap.step(1.0, seen(st))
    st[4, 4] = SUNK
    ap.step(1.2, seen(st))
    assert ap.results[SUNK] == 1 and ap.last_result == SUNK


# ---------------------------------------------------------------------------- названия клеток
def test_coord_names_for_both_label_layouts():
    assert coord_name(0, 8, "ru", LETTERS_TOP) == "И1"     # столбец И, строка 1
    assert coord_name(0, 8, "ru", NUMBERS_TOP) == "А9"     # строка А, столбец 9 (Яндекс Игры)
    assert coord_name(9, 0, "ru", NUMBERS_TOP) == "К1"
    assert coord_name(9, 0, "lat", LETTERS_TOP) == "A10"
    ships = [[(2, 3), (2, 4), (2, 5)]]
    assert describe(ships, "ru", NUMBERS_TOP) == ["трёхпалубный: В4–В6"]
    assert describe(ships, "ru", LETTERS_TOP) == ["трёхпалубный: Г3–Е3"]


# ---------------------------------------------------------------------------- сквозная проверка
def _qt_app():
    pytest.importorskip("PySide6")
    if not sys.platform.startswith("win") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _wait(app, cond, timeout):
    t = time.time()
    while time.time() - t < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_autoplay_clicks_the_right_cells_and_waits_out_opponent_turn(tmp_path, monkeypatch):
    """Главное окно + тренировка: автоигра сама стреляет «мышью» (события Qt в поле тренировки),
    после промаха соперник ходит секунду без затемнения поля — щелчки в это время пропадают,
    и автоигра должна дождаться и выстрелить снова. Каждый щелчок — ровно в ту клетку, что задумана."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _qt_app()
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QGuiApplication, QMouseEvent
    from PySide6.QtWidgets import QApplication

    from seabattle import capture
    from seabattle.mouse import FakeMouse
    from seabattle.rules import Rules
    from seabattle.settings import Settings
    from seabattle.ui.main_window import MainWindow
    from seabattle.ui.practice import PracticeWindow
    from seabattle.vision import find_grids

    st = Settings()
    st.budget = 0.3
    st.interval_ms = 150
    st.first_run = False
    st.hide_from_capture = False
    win = MainWindow(st)
    practice = PracticeWindow(Rules())
    try:
        scr = QGuiApplication.primaryScreen().availableGeometry()
        practice.move(scr.x() + 10, scr.y() + 10)
        practice.show()
        win.resize(380, 640)
        win.move(scr.x() + practice.width() + 60, scr.y() + 10)
        win.show()
        _wait(app, lambda: False, 0.8)

        ps = QGuiApplication.primaryScreen()
        img = capture.grab_screen(ps)
        grids = find_grids(capture.qimage_to_rgb(img))
        smap = capture.ScreenMap(ps, (img.width(), img.height()))
        b = practice.board
        cx, cy = smap.to_image(b.mapToGlobal(b.rect().center()))
        pick = [g for g in grids if g.x0 <= cx <= g.x0 + g.width and g.y0 <= cy <= g.y0 + g.height]
        if not pick:
            pytest.skip("на этой системе снимок экрана не показывает окна")
        win.apply_calibration(ps, pick[0], None, img)

        def fake_click(x, y):
            local = b.mapFromGlobal(QPoint(x, y))
            if not b.rect().contains(local):
                return
            for et in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
                ev = QMouseEvent(et, QPointF(local), QPointF(x, y), Qt.LeftButton,
                                 Qt.LeftButton if et == QEvent.MouseButtonPress else Qt.NoButton,
                                 Qt.NoModifier)
                QApplication.sendEvent(b, ev)

        win.mouse = FakeMouse(fake_click)
        practice.opp_turn.setChecked(True)
        practice.opponent_delay = 0.8
        practice.dim_on_opponent = False          # хода соперника не видно — только щелчки пропадают
        ap = win.autopilot
        ap.first_delay, ap.wait_result, ap.retry = 0.2, 0.9, 0.3
        win.s.auto_delay = 0.15
        assert win.start_auto()
        ok = _wait(app, lambda: practice.ref.shots >= 8 or practice.ref.done or not ap.active, 60)
        assert ok, "автоигра не сделала 8 выстрелов за минуту"
        assert ap.active or practice.ref.done, ap.note
        targets = [t[1] for t in win.auto_log]
        clicks = list(b.clicks)
        assert len(clicks) == len(targets) and all(tuple(a) == tuple(c) for a, c in zip(targets, clicks)), \
            (targets, clicks)
        # каждый выстрел, который тренировка приняла, помощник тоже увидел (или вот-вот увидит)
        assert _wait(app, lambda: np.array_equal(np.where(practice.ref.states == SUNK, HIT, practice.ref.states),
                                                 np.where(win.tracker.states == SUNK, HIT, win.tracker.states)), 5)
        # пропавшие во время хода соперника щелчки повторены: щелчков больше, чем принятых выстрелов
        assert ap.clicks == practice.ref.shots + practice.ignored_clicks
    finally:
        win.stop_auto()
        win.timer.stop()
        win.worker.stop()
        for w in (win.overlay, win.ghost, win.debug, practice, win):
            w.hide()
        app.processEvents()
