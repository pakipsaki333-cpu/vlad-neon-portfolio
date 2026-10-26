"""Запуск: python -m seabattle   |   самопроверка: python -m seabattle --selftest папка"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback


def _qt_app():
    from PySide6.QtWidgets import QApplication

    from . import winutil
    winutil.set_app_id()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("SeaBattleHelper")
    app.setQuitOnLastWindowClosed(False)
    return app


def main_gui():
    app = _qt_app()
    from .icon import app_icon
    from .ui.main_window import MainWindow
    icon = app_icon()
    app.setWindowIcon(icon)
    win = MainWindow(icon=icon)
    win.show()
    return app.exec()


# ---------------------------------------------------------------------------- самопроверка
def run_selftest(out_dir: str, max_seconds: float = 170.0) -> int:
    """Тренировочная партия: помощник распознаёт поле с экрана и доигрывает её по своим подсказкам.

    Выстрелы делаются через внутренний метод тренировки (никакой эмуляции мыши).
    Пишет report.json и скриншоты. Код возврата 0 — всё в порядке.
    """
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="seabattle_selftest_")
    os.environ["APPDATA"] = tmp                 # не трогаем настройки пользователя
    os.environ["XDG_CONFIG_HOME"] = tmp
    app = _qt_app()
    import numpy as np
    from PySide6.QtCore import QRect, QTimer
    from PySide6.QtGui import QGuiApplication

    from . import capture
    from .icon import app_icon, save_ico
    from .rules import HIT, SUNK, Rules
    from .settings import Settings
    from .ui.main_window import MainWindow
    from .ui.practice import PracticeWindow
    from .vision import find_grids

    report = {"ok": False, "mode": None, "shots": 0, "won": False, "errors": [],
              "max_lag_s": 0.0, "platform": sys.platform, "screens": []}
    for s in QGuiApplication.screens():
        g = s.geometry()
        report["screens"].append({"name": s.name(), "geometry": [g.x(), g.y(), g.width(), g.height()],
                                  "dpr": s.devicePixelRatio()})
    save_ico(os.path.join(out_dir, "icon.ico"))
    app_icon().pixmap(256, 256).save(os.path.join(out_dir, "icon.png"))

    st = Settings()
    st.budget = 0.35
    st.interval_ms = 200
    st.overlay = True
    st.first_run = False
    win = MainWindow(st, app_icon())
    scr = QGuiApplication.primaryScreen().availableGeometry()
    win.resize(380, 660)
    win.move(scr.x() + 560, scr.y() + 20)
    win.show()
    practice = PracticeWindow(Rules())
    practice.move(scr.x() + 20, scr.y() + 40)
    practice.show()

    t_start = time.time()
    state = {"phase": "calibrate", "since_sync": time.time(), "shot_taken_at": None, "shots": 0}

    def capture_hiding_check():
        """Видно ли окно помощника на снимке экрана (при скрытии от захвата — не должно)."""
        report["capture_hidden_main"] = bool(win.capture_hidden)
        report["capture_hidden_overlay"] = bool(win.overlay.capture_hidden)
        if report.get("mode") != "screen":
            return
        try:
            ps = win.screen() or QGuiApplication.primaryScreen()
            shot = capture.grab_screen(ps)
            smap = capture.ScreenMap(ps, (shot.width(), shot.height()))
            g = win.frameGeometry()
            x0, y0 = smap.to_image(g.topLeft())
            x1, y1 = smap.to_image(g.bottomRight())
            crop = capture.qimage_to_rgb(shot, QRect(int(x0), int(y0), int(x1 - x0), int(y1 - y0)))
            own = capture.qimage_to_rgb(win.grab().toImage())
            h, w = min(crop.shape[0], own.shape[0]), min(crop.shape[1], own.shape[1])
            diff = float(np.abs(crop[:h, :w].astype(int) - own[:h, :w].astype(int)).mean())
            report["main_window_pixels_diff"] = round(diff, 1)
            report["main_window_in_capture"] = diff < 25
        except Exception as ex:
            report["errors"].append(f"capture hiding check: {ex}")

    def finish(ok_extra=True):
        capture_hiding_check()
        try:
            win.grab().save(os.path.join(out_dir, "helper_final.png"))
            practice.grab().save(os.path.join(out_dir, "practice_final.png"))
        except Exception as ex:
            report["errors"].append(f"screenshot: {ex}")
        report["shots"] = practice.ref.shots
        report["won"] = practice.ref.done
        report["elapsed_s"] = round(time.time() - t_start, 1)
        report["ok"] = bool(report["won"] and ok_extra and not report["errors"])
        with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=False))
        timer.stop()
        win.timer.stop()
        win.worker.stop()
        app.exit(0 if report["ok"] else 1)

    def calibrate():
        # 1) настоящий захват экрана
        try:
            ps = practice.screen() or QGuiApplication.primaryScreen()
            img = capture.grab_screen(ps)
            arr = capture.qimage_to_rgb(img)
            grids = find_grids(arr)
            smap = capture.ScreenMap(ps, (img.width(), img.height()))
            b = practice.board
            gp = b.mapToGlobal(b.rect().center())
            cx, cy = smap.to_image(gp)
            pick = [g for g in grids if g.x0 <= cx <= g.x0 + g.width and g.y0 <= cy <= g.y0 + g.height]
            report["grids_on_screen"] = len(grids)
            if pick and float(arr.std()) > 3:
                img.save(os.path.join(out_dir, "screen_at_calibration.png"))
                win.apply_calibration(ps, pick[0], None, img)
                report["mode"] = "screen"
                return True
        except Exception as ex:
            report["errors"].append(f"screen capture: {ex}")
        # 2) запасной путь (нет рабочего стола, например в CI без сессии): картинка виджета
        def frame():
            return practice.board.grab().toImage()
        img = frame()
        grids = find_grids(capture.qimage_to_rgb(img))
        if not grids:
            report["errors"].append("поле тренировки не найдено даже на картинке виджета")
            return False
        win.frame_source = frame
        win.apply_calibration(QGuiApplication.primaryScreen(), grids[0], None, img)
        report["mode"] = "widget"
        return True

    def tick():
        try:
            if time.time() - t_start > max_seconds:
                report["errors"].append("не уложились по времени")
                finish(False)
                return
            if state["phase"] == "calibrate":
                if not calibrate():
                    finish(False)
                    return
                state["phase"] = "play"
                state["since_sync"] = time.time()
                return
            ref = practice.ref
            if ref.done:
                finish(True)
                return
            truth = ref.states
            got = win.current_states()
            synced = np.array_equal(np.where(truth == SUNK, HIT, truth), np.where(got == SUNK, HIT, got))
            if not synced:
                lag = time.time() - state["since_sync"]
                report["max_lag_s"] = max(report["max_lag_s"], round(lag, 2))
                if lag > 8:
                    bad = np.argwhere(np.where(truth == SUNK, HIT, truth) != np.where(got == SUNK, HIT, got))
                    report["errors"].append(f"распознавание отстало: клетки {bad.tolist()[:6]}")
                    finish(False)
                return
            a = win.analysis
            if a is None or win.analysis_src is None or not np.array_equal(win.analysis_src, got):
                return
            if a.best is None:
                report["errors"].append("решатель не дал хода")
                finish(False)
                return
            if ref.shots == 20:
                win.grab().save(os.path.join(out_dir, "helper_midgame.png"))
                practice.grab().save(os.path.join(out_dir, "practice_midgame.png"))
                if report["mode"] == "screen":
                    capture.grab_screen(practice.screen()).save(os.path.join(out_dir, "screen_midgame.png"))
            practice.shoot(*a.best)
            state["since_sync"] = time.time()
        except Exception:
            report["errors"].append(traceback.format_exc())
            finish(False)

    timer = QTimer()
    timer.timeout.connect(tick)
    QTimer.singleShot(1500, lambda: timer.start(150))
    return app.exec()


def main():
    ap = argparse.ArgumentParser(prog="seabattle", description="Помощник для морского боя")
    ap.add_argument("--selftest", metavar="ПАПКА", help="самопроверка с отчётом и скриншотами")
    ap.add_argument("--make-icon", metavar="ФАЙЛ.ico", help="сохранить иконку для сборки exe")
    args = ap.parse_args()
    if args.make_icon:
        _qt_app()
        from .icon import save_ico
        return 0 if save_ico(args.make_icon) else 1
    if args.selftest:
        return run_selftest(args.selftest)
    return main_gui()


if __name__ == "__main__":
    sys.exit(main())
