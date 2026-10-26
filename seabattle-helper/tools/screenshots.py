"""Скриншоты окна помощника на примере партии (для README и проверки вёрстки).

    python tools/screenshots.py docs
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp())
os.environ.setdefault("APPDATA", os.environ["XDG_CONFIG_HOME"])

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from seabattle.icon import app_icon  # noqa: E402
from seabattle.rules import HIT, MISS, SUNK  # noqa: E402
from seabattle.settings import Settings  # noqa: E402
from seabattle.ui.main_window import MainWindow  # noqa: E402


def example_states():
    st = np.zeros((10, 10), np.int8)
    for r, c in [(0, 0), (0, 3), (1, 1), (2, 6), (3, 2), (4, 8), (5, 0), (6, 4), (7, 7), (8, 2),
                 (9, 5), (2, 9), (6, 9), (8, 8), (4, 4)]:
        st[r, c] = MISS
    for r, c in [(9, 0), (9, 1), (9, 2)]:          # убитый трёхпалубный
        st[r, c] = SUNK
    for r, c in [(8, 0), (8, 1), (8, 2), (8, 3)]:
        st[r, c] = MISS
    st[9, 3] = MISS
    st[3, 6] = HIT                                 # раненый
    return st


def wait(app, cond, timeout=10.0):
    t = time.time()
    while time.time() - t < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "screenshots"
    os.makedirs(out, exist_ok=True)
    app = QApplication(sys.argv)
    s = Settings()
    s.budget = 1.0
    win = MainWindow(s, app_icon())
    win.resize(380, 700)
    win.show()
    win.manual_states = example_states()
    win.refresh_states()
    wait(app, lambda: win.analysis_src is not None and np.array_equal(win.analysis_src, win.manual_states))
    wait(app, lambda: False, 0.3)
    names = ["attack", "placement", "settings", "help"]
    for i, name in enumerate(names):
        win.tabs.setCurrentIndex(i)
        wait(app, lambda: False, 0.25)
        win.grab().save(os.path.join(out, f"{name}.png"))
    win.tabs.setCurrentIndex(0)
    win.set_mini(True)
    wait(app, lambda: False, 0.3)
    win.grab().save(os.path.join(out, "mini.png"))
    win.set_mini(False)
    win.worker.stop()
    print("saved to", out)


if __name__ == "__main__":
    main()
