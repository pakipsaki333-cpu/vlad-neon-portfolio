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
def run_selftest(out_dir: str, max_seconds: float = 170.0, auto: bool = False) -> int:
    """Тренировочная партия: помощник распознаёт поле с экрана и доигрывает её по своим подсказкам.

    Обычный режим: выстрелы — через внутренний метод тренировки (без мыши).
    auto=True: играет автоигра — настоящими щелчками мыши (Windows, SendInput), а тренировка
    после каждого промаха «отдаёт ход сопернику» на секунду и не принимает щелчки.
    Пишет report.json и скриншоты. Код возврата 0 — всё в порядке.
    """
    os.makedirs(out_dir, exist_ok=True)
    # Если что-то повиснет (например, модальное окно или бесконечный цикл), через max_seconds+30 с
    # стеки всех потоков окажутся в hang.txt, и процесс завершится — а не будет ждать до таймаута CI.
    import faulthandler
    hang_file = open(os.path.join(out_dir, "hang.txt"), "w", encoding="utf-8")
    faulthandler.dump_traceback_later(max_seconds + 30, exit=True, file=hang_file)
    progress = open(os.path.join(out_dir, "progress.log"), "w", encoding="utf-8")
    t_log0 = time.time()

    def log(msg):
        progress.write(f"{time.time() - t_log0:7.2f} {msg}\n")
        progress.flush()

    tmp = tempfile.mkdtemp(prefix="seabattle_selftest_")
    os.environ["APPDATA"] = tmp                 # не трогаем настройки пользователя
    os.environ["XDG_CONFIG_HOME"] = tmp
    app = _qt_app()
    import numpy as np
    from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QTimer
    from PySide6.QtGui import QGuiApplication, QMouseEvent
    from PySide6.QtWidgets import QApplication

    from . import capture, mouse as mouse_mod
    from .icon import app_icon, save_ico
    from .rules import HIT, SUNK, Rules
    from .settings import Settings
    from .ui.main_window import MainWindow
    from .ui.practice import PracticeWindow
    from .vision import find_grids

    from . import __version__
    report = {"ok": False, "version": __version__, "mode": None, "shots": 0, "won": False, "errors": [],
              "max_lag_s": 0.0, "platform": sys.platform, "screens": [], "auto": None}
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

    if auto:
        practice.opp_turn.setChecked(True)          # после промаха — секунда «хода соперника»
        practice.opponent_delay = 1.0
        if not win.mouse.available:
            # не Windows: «щелчки» — это события мыши Qt прямо в поле тренировки
            def fake_click(x, y):
                b = practice.board
                local = b.mapFromGlobal(QPoint(x, y))
                if not b.rect().contains(local):
                    return
                for et in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
                    ev = QMouseEvent(et, QPointF(local), QPointF(x, y), Qt.LeftButton,
                                     Qt.LeftButton if et == QEvent.MouseButtonPress else Qt.NoButton,
                                     Qt.NoModifier)
                    QApplication.sendEvent(b, ev)
            win.mouse = mouse_mod.FakeMouse(fake_click)
        report["auto"] = {"mouse": type(win.mouse).__name__}
        orig_auto_click = win._auto_click

        def logged_click(*args):
            log(f"click args={args} cursor={win.mouse.pos()}")
            ok = orig_auto_click(*args)
            log(f"  -> ok={ok} cursor={win.mouse.pos()} note={win.autopilot.note!r}")
            return ok
        win._auto_click = logged_click
    log(f"start auto={auto} mouse={type(win.mouse).__name__}")

    t_start = time.time()
    state = {"phase": "calibrate", "since_sync": time.time(), "shot_taken_at": None, "shots": 0,
             "unsynced_since": None}

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
        if state.get("finished"):
            return
        state["finished"] = True
        try:
            _finish(ok_extra)
        except Exception:
            report["errors"].append("finish: " + traceback.format_exc())
            report["ok"] = False
            try:
                with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            except Exception:
                pass
        finally:
            log(f"finish ok={report.get('ok')} errors={len(report['errors'])}")
            timer.stop()
            win.timer.stop()
            win.auto_timer.stop()
            win.worker.stop()
            app.exit(0 if report.get("ok") else 1)

    def _finish(ok_extra=True):
        capture_hiding_check()
        try:
            win.grab().save(os.path.join(out_dir, "helper_final.png"))
            practice.grab().save(os.path.join(out_dir, "practice_final.png"))
        except Exception as ex:
            report["errors"].append(f"screenshot: {ex}")
        report["shots"] = practice.ref.shots
        report["won"] = practice.ref.done
        report["elapsed_s"] = round(time.time() - t_start, 1)
        if auto:
            ap = win.autopilot
            targets = [t[1] for t in win.auto_log]
            got_clicks = list(practice.board.clicks)
            report["auto"].update({
                "clicks": ap.clicks, "answered": ap.shots, "note": ap.note,
                "ignored_by_practice": practice.ignored_clicks,
                "board_clicks": len(got_clicks),
                "click_cell_mismatch": sum(1 for a, b in zip(targets, got_clicks) if tuple(a) != tuple(b))
                + abs(len(targets) - len(got_clicks)),
                "first_clicks": [[list(t[1]), list(t[2])] for t in win.auto_log[:3]],
            })
            try:
                ps = practice.screen() or QGuiApplication.primaryScreen()
                g = ps.geometry()
                report["auto"]["qt_origin"] = [g.x(), g.y()]
                report["auto"]["native_origin"] = list(capture.native_origin(ps))
                from . import winutil
                report["auto"]["screen_name"] = ps.name()
                report["auto"]["monitors"] = {k: list(v) for k, v in winutil.monitor_rects().items()}
            except Exception as ex:
                report["errors"].append(f"origin: {ex}")
            if report["auto"]["click_cell_mismatch"]:
                report["errors"].append("автоигра щёлкнула не в ту клетку: "
                                        f"{targets[:5]} vs {got_clicks[:5]}")
        report["ok"] = bool(report["won"] and ok_extra and not report["errors"])
        with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        try:
            print(json.dumps(report, ensure_ascii=False, default=str))
        except Exception:                    # exe без консоли: печатать некуда
            pass

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
                if auto:
                    if report["mode"] != "screen":
                        report["errors"].append("автоигре нужен настоящий захват экрана")
                        finish(False)
                        return
                    win.autopilot.first_delay = 0.6
                    win.s.auto_delay = 0.3
                    if not win.start_auto():
                        report["errors"].append("автоигра не включилась: " + win.auto_info.text())
                        finish(False)
                return
            ref = practice.ref
            now = time.time()
            if now - state.get("logged_at", 0) >= 1.0:
                state["logged_at"] = now
                ap = win.autopilot
                log(f"shots={ref.shots} done={ref.done} busy={practice.busy} track={win.last_status} "
                    f"lost={win.lost} paused={win.paused} analysis={'yes' if win.analysis is not None else 'no'} "
                    f"auto={ap.phase} clicks={ap.clicks} answered={ap.shots} note={ap.note!r}")
            if ref.done:
                finish(True)
                return
            truth = ref.states
            got = win.current_states()
            if auto:
                synced = np.array_equal(np.where(truth == SUNK, HIT, truth), np.where(got == SUNK, HIT, got))
                if synced:
                    state["unsynced_since"] = None
                else:
                    if state["unsynced_since"] is None:
                        state["unsynced_since"] = time.time()
                    lag = time.time() - state["unsynced_since"]
                    report["max_lag_s"] = max(report["max_lag_s"], round(lag, 2))
                    if lag > 8:
                        bad = np.argwhere(np.where(truth == SUNK, HIT, truth) != np.where(got == SUNK, HIT, got))
                        report["errors"].append(f"распознавание отстало: клетки {bad.tolist()[:6]}")
                        finish(False)
                        return
                if ref.shots == 20 and not state.get("mid_saved"):
                    state["mid_saved"] = True
                    win.grab().save(os.path.join(out_dir, "helper_midgame.png"))
                    capture.grab_screen(practice.screen()).save(os.path.join(out_dir, "screen_midgame.png"))
                if not win.autopilot.active:
                    report["errors"].append("автоигра остановилась: " + win.autopilot.note)
                    finish(False)
                return
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
    code = app.exec()
    log(f"exit code={code}")
    faulthandler.cancel_dump_traceback_later()
    progress.close()
    hang_file.close()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    # Выходим сразу, без разборки объектов Qt: самопроверке она не нужна, а на CI зависание или
    # падение при выходе выглядело бы как провал проверки.
    os._exit(code)


def main():
    ap = argparse.ArgumentParser(prog="seabattle", description="Помощник для морского боя")
    ap.add_argument("--selftest", metavar="ПАПКА", help="самопроверка с отчётом и скриншотами")
    ap.add_argument("--auto", action="store_true",
                    help="самопроверка автоигры: настоящие щелчки мыши по полю тренировки")
    ap.add_argument("--make-icon", metavar="ФАЙЛ.ico", help="сохранить иконку для сборки exe")
    args = ap.parse_args()
    if args.make_icon:
        _qt_app()
        from .icon import save_ico
        return 0 if save_ico(args.make_icon) else 1
    if args.selftest:
        return run_selftest(args.selftest, auto=args.auto)
    return main_gui()


if __name__ == "__main__":
    sys.exit(main())
