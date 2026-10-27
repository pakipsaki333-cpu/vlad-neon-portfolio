"""Состав флота: готовые правила «Морского Боя классического» (Яндекс Игры, Пикабу), проверка,
что поле сходится с флотом, и автоматическое переключение правил.

Откуда это: три проигрыша автоигры подряд на games.pikabu.ru. Распознавание было верным, но на
сайте флот 1×5, 1×4, 2×3, 3×2, 3×1 (24 палубы), а помощник считал по классике (20 палуб):
четыре попадания в ряд принимал за потопленный четырёхпалубный, не добивал пятипалубный и искал
лишний однопалубный.
"""
import os
import random
import sys
import time

import numpy as np
import pytest

from seabattle import fleetcheck
from seabattle.board import prepare, validate_layout
from seabattle.game import Referee
from seabattle.rules import (CLASSIC_FLEET, HIT, MISS, PRESETS, SUNK, UNKNOWN, YANDEX_FLEET, Rules, parse_fleet,
                             preset_key)
from seabattle.solver import analyze, random_layout

CLASSIC = Rules()
YANDEX = PRESETS["yandex"][1]

# Расстановка со скриншота Яндекс Игр (своё поле игрока; строки — буквы, столбцы — цифры):
# пятипалубный Д3–Д7, четырёхпалубный Б10–Д10, трёхпалубные В6–В8 и И2–И4,
# двухпалубные А2–Б2, Ж2–Ж3, И9–И10, однопалубные Ж5, Ж9, И6.
YANDEX_SCREENSHOT_LAYOUT = [
    [(4, c) for c in range(2, 7)],
    [(r, 9) for r in range(1, 5)],
    [(2, 5), (2, 6), (2, 7)], [(8, 1), (8, 2), (8, 3)],
    [(0, 1), (1, 1)], [(6, 1), (6, 2)], [(8, 8), (8, 9)],
    [(6, 4)], [(6, 8)], [(8, 5)],
]


def board(cells, value=HIT):
    st = np.full((10, 10), UNKNOWN, np.int8)
    for rc in cells:
        st[rc] = value
    return st


# ---------------------------------------------------------------------------- правила
def test_yandex_preset_matches_the_real_game():
    assert YANDEX.fleet == YANDEX_FLEET == (5, 4, 3, 3, 2, 2, 2, 1, 1, 1)
    assert YANDEX.total_cells == 24 and YANDEX.touch == "none" and YANDEX.size == 10
    assert parse_fleet("1x5 1x4 2x3 3x2 3x1") == YANDEX.fleet
    assert validate_layout(YANDEX_SCREENSHOT_LAYOUT, YANDEX) == []
    assert validate_layout(YANDEX_SCREENSHOT_LAYOUT, CLASSIC) != []      # с классикой не сходится
    assert preset_key(YANDEX) == "yandex" and preset_key(CLASSIC) == "classic"
    assert preset_key(Rules(fleet=(3, 3, 2))) is None
    random_layout(YANDEX, random.Random(1))                               # такой флот расставляется


def test_five_in_a_row_contradicts_classic_fleet_but_not_yandex():
    st = board([(7, c) for c in range(2, 7)])
    p = prepare(st, CLASSIC)
    assert p.fatal and "5 попаданий в ряд" in p.fleet_issue
    assert any("состав флота" in s for s in p.issues)
    q = prepare(st, YANDEX)
    assert not q.fatal and not q.fleet_issue
    a = analyze(st, CLASSIC, budget=0.2)
    assert a.fleet_issue and a.issues


def test_more_hits_than_decks_is_a_fleet_issue():
    cells = [(r, c) for r in range(0, 10, 2) for c in range(0, 4)] + [(1, 9)]   # 21 попадание
    p = prepare(board(cells), CLASSIC)
    assert p.fatal and "попаданий 21" in p.fleet_issue
    assert not prepare(board(cells), YANDEX).fleet_issue


def test_four_in_a_row_is_sunk_only_by_length_with_classic():
    """Ровно та ошибка из проигранных партий: с классикой четыре попадания — «потоплен», и
    решатель уходит искать дальше; с флотом Яндекса добивает пятую палубу."""
    st = board([(7, c) for c in range(2, 6)])
    p = prepare(st, CLASSIC)
    assert p.by_length == [[(7, 2), (7, 3), (7, 4), (7, 5)]]
    a = analyze(st, CLASSIC, budget=0.2, seed=1)
    assert a.mode == "hunt" and a.best not in ((7, 1), (7, 6))
    b = analyze(st, YANDEX, budget=0.3, seed=1)
    assert b.mode == "target" and b.best in ((7, 1), (7, 6))
    assert prepare(st, YANDEX).by_length == []


# ---------------------------------------------------------------------------- проверка флота
def test_inspect_and_suggest_for_classic_on_a_yandex_board():
    st = board([(7, c) for c in range(2, 6)])
    fc = fleetcheck.inspect(st, CLASSIC)
    assert not fc.hard and fc.suspects == [[(7, 2), (7, 3), (7, 4), (7, 5)]]
    assert fleetcheck.suggest(st, CLASSIC, fc.suspects)[0] == "yandex"
    assert fleetcheck.inspect(st, YANDEX).suspects == []
    st5 = board([(7, c) for c in range(2, 7)])
    fc5 = fleetcheck.inspect(st5, CLASSIC)
    assert fc5.hard and fleetcheck.suggest(st5, CLASSIC)[0] == "yandex"


# Конец партии с классикой на сайте Яндекса (пойман бенчмарком): девять кораблей найдены, а первое
# попадание в пятипалубный (строка 6) помощник счёл «последним однопалубным» и объявил победу.
ENDGAME = """
###ooooo##
oooooooooo
#ooo#oo#oo
#o#oooooo#
#o#oo..oo#
oooo..o.o#
oX......o#
oooo...ooo
oo#o..oo#o
ooooo.oo#o
"""


def parse_board(text):
    rows = text.strip().splitlines()
    code = {".": UNKNOWN, "o": MISS, "X": HIT, "#": SUNK}
    return np.array([[code[ch] for ch in row] for row in rows], np.int8)


def test_last_single_that_is_really_a_five_decker():
    st = parse_board(ENDGAME)
    assert analyze(st, CLASSIC, budget=0.2).mode == "done"            # ложная «победа»
    fc = fleetcheck.inspect(st, CLASSIC)
    assert fc.suspects == [[(6, 1)]] and not fc.hard
    key, rules = fleetcheck.suggest(st, CLASSIC, fc.suspects)
    assert key == "yandex"
    a = analyze(st, rules, budget=0.3, seed=1)
    # пятипалубный может стоять только в (6,1)–(6,5): любая из четырёх клеток — верное попадание
    assert a.mode == "target" and a.best in ((6, 2), (6, 3), (6, 4), (6, 5)) and a.best_prob > 0.99


def test_suggest_nothing_when_no_preset_fits():
    st = board([(7, c) for c in range(0, 6)])                    # шесть в ряд — такого флота нет
    assert fleetcheck.suggest(st, CLASSIC) is None
    st = board([(7, c) for c in range(2, 6)])
    # подозрительный корабль и с флотом Яндекса обязан быть потопленным (пятипалубный уже убит)
    for c in range(0, 5):
        st[1, c] = SUNK
    assert fleetcheck.suggest(st, YANDEX, fleetcheck.inspect(st, YANDEX).suspects) is None


def test_auto_halo_marks():
    st = board([(4, 4)], SUNK)
    assert fleetcheck.auto_halo_marks([(3, 3, MISS), (3, 4, MISS), (4, 4, SUNK)], st)
    assert not fleetcheck.auto_halo_marks([(0, 0, MISS)], st)
    assert not fleetcheck.auto_halo_marks([(0, 0, MISS), (9, 9, MISS)], st)   # не рядом с кораблём


def test_halo_watch_sees_blots_appearing_one_by_one():
    st = board([(4, 4)], SUNK)
    w = fleetcheck.HaloWatch()
    assert not w.feed(0.0, [(3, 3, MISS)], st)
    assert not w.feed(0.4, [(3, 4, MISS)], st)
    assert w.feed(0.8, [(3, 5, MISS)], st)                       # три точки у корабля за 0.8 с
    w = fleetcheck.HaloWatch()
    for t, rc in [(0.0, (3, 3)), (2.0, (3, 4)), (4.0, (3, 5))]:  # обычные промахи игрока: ход за ходом
        assert not w.feed(t, [(*rc, MISS)], st)
    assert fleetcheck.HaloWatch().feed(0.0, [(3, 3, MISS), (5, 5, MISS)], st)   # две за кадр — сайт


def _play(layout, site_rules, start_rules, seed, budget=0.05, limit=110):
    """Партия, как её ведёт главное окно: после каждого выстрела проверяем флот («мягкий»
    признак — сайт не обвёл корабль за SOFT_WAIT секунд; между выстрелами ~1.5 с)."""
    ref = Referee(layout, site_rules, auto_mark=True)          # сайт сам ставит точки вокруг убитых
    rules, since, t, switched = start_rules, {}, 0.0, None
    while not ref.done and ref.shots < limit:
        st = ref.visible()
        fc = fleetcheck.inspect(st, rules)
        live = {frozenset(g) for g in fc.suspects}
        since = {k: v for k, v in since.items() if k in live}
        for k in live:
            since.setdefault(k, t)
        old = [sorted(k) for k, v in since.items() if t - v >= fleetcheck.SOFT_WAIT]
        if (fc.hard or old) and switched is None:
            sug = fleetcheck.suggest(st, rules, old)
            if sug is not None:
                switched, rules = (ref.shots, sug[0]), sug[1]
        a = analyze(st, rules, budget=budget, seed=seed * 1000 + ref.shots)
        if a.best is None:                    # решатель считает, что всё потоплено (или запутался)
            break
        ref.shoot(*a.best)
        t += 1.5
    return ref, switched, rules


def test_replay_of_the_lost_pikabu_game_switches_fleet_and_wins():
    ref, switched, rules = _play(YANDEX_SCREENSHOT_LAYOUT, YANDEX, CLASSIC, seed=3)
    assert switched is not None and switched[1] == "yandex", switched
    assert rules.fleet == YANDEX_FLEET
    assert ref.done, f"не добили флот за {ref.shots} выстрелов"
    assert ref.shots <= 85                                       # бот сайта тратил ~85 выстрелов


def test_right_fleet_never_switches():
    for seed in range(2):
        layout = random_layout(CLASSIC, random.Random(seed))
        ref, switched, _ = _play(layout, CLASSIC, CLASSIC, seed)
        assert ref.done and switched is None, (seed, switched)
    ref, switched, _ = _play(YANDEX_SCREENSHOT_LAYOUT, YANDEX, YANDEX, 5)
    assert ref.done and switched is None


# ---------------------------------------------------------------------------- главное окно
def _qt_app():
    pytest.importorskip("PySide6")
    if not sys.platform.startswith("win") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as ex:                 # нет системных библиотек Qt (голый Linux-сервер)
        pytest.skip(f"Qt недоступен: {ex}")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _qt_app()
    from seabattle.settings import Settings
    from seabattle.ui.main_window import MainWindow
    st = Settings()
    st.budget = 0.3
    st.first_run = False
    st.hide_from_capture = False
    win = MainWindow(st)
    yield app, win
    win.stop_auto()
    win.timer.stop()
    win.worker.stop()
    for w in (win.overlay, win.ghost, win.debug, win):
        w.hide()
    app.processEvents()


def _calibrate(win, style, cell=40):
    """Откалибровать окно по синтетическому «скриншоту» (без настоящего экрана)."""
    from PySide6.QtGui import QGuiApplication

    from seabattle import capture
    from seabattle.vision import GridGeom
    from synth import make_screen
    off = (cell * 14, cell * 3)
    arr = make_screen(np.zeros((10, 10), np.int8), style=style, cell=cell, size=(cell * 28, cell * 16),
                      offset=off)
    img = capture.rgb_to_qimage(np.ascontiguousarray(arr))
    win.frame_source = lambda: img
    enemy = GridGeom(off[0], off[1], cell, cell)
    my = GridGeom(off[0] - cell * (11 if style == "paper" else 14), off[1], cell, cell)
    win.apply_calibration(QGuiApplication.primaryScreen(), enemy, my, img)
    win.timer.stop()
    return img


def test_calibration_on_yandex_style_board_switches_fleet(window):
    app, win = window
    assert win.rules.fleet == CLASSIC_FLEET and win.s.rules_source == "default"
    _calibrate(win, "paper")
    assert win.rules.fleet == YANDEX_FLEET and win.s.rules_source == "auto_site"
    assert win.s.fleet == list(YANDEX_FLEET)
    assert win.cmb_preset.currentData() == "yandex"
    assert "Морской Бой классический" in win._fleet_note
    # другой сайт (буквы сверху, без тетрадного листа) — возвращаем классику
    _calibrate(win, "notebook")
    assert win.rules.fleet == CLASSIC_FLEET and win.cmb_preset.currentData() == "classic"


def test_calibration_respects_rules_chosen_by_user(window):
    app, win = window
    win.cmb_preset.setCurrentIndex(win.cmb_preset.findData("classic"))
    win._preset_chosen(win.cmb_preset.currentIndex())
    assert win.s.rules_source == "user"
    _calibrate(win, "paper")
    assert win.rules.fleet == CLASSIC_FLEET                      # сам выбрал — не трогаем
    assert "выбери эти правила" in win._fleet_note               # но подсказываем


def test_board_unconfirmed_sinking_switches_fleet_and_keeps_autoplay(window):
    app, win = window
    _calibrate(win, "notebook")
    assert win.rules.fleet == CLASSIC_FLEET
    win.autopilot.start(time.monotonic())
    win.tracker.states[7, 2:6] = HIT                              # четыре в ряд, концы не обведены
    win.tracker.pending[:] = -1
    win.last_status = "ok"
    win.halo_watch.seen = True                                    # сайт обводит потопленные точками
    win._check_fleet(now=100.0)
    assert win.rules.fleet == CLASSIC_FLEET                       # ждём: сайт мог не успеть обвести
    win._check_fleet(now=100.0 + fleetcheck.SOFT_WAIT + 0.5)
    assert win.rules.fleet == YANDEX_FLEET and win.s.rules_source == "auto_board"
    assert "другой" in win._fleet_note and win.autopilot.active


def test_unconfirmed_sinking_ignored_until_site_shows_auto_halo(window):
    app, win = window
    _calibrate(win, "notebook")
    win.tracker.states[7, 2:6] = HIT
    win.tracker.pending[:] = -1
    win.last_status = "ok"
    win._check_fleet(now=100.0)
    win._check_fleet(now=200.0)
    assert win.rules.fleet == CLASSIC_FLEET                       # сайт без точек вокруг — не довод


def test_hard_contradiction_with_custom_fleet_stops_autoplay(window):
    app, win = window
    win.ed_fleet.setText("1x4 2x3 3x2 3x1")                       # свой флот — сами не меняем
    win._rules_edited()
    assert preset_key(win.rules) is None
    win.autopilot.start(time.monotonic())
    win.manual_states[7, 2:7] = HIT                               # пять в ряд
    win._check_fleet()
    assert win.rules.fleet == (4, 3, 3, 2, 2, 2, 1, 1, 1)
    assert not win.autopilot.active and "не сходится" in win.autopilot.note


def test_hard_contradiction_with_stock_fleet_switches_in_manual_mode(window):
    app, win = window
    win.manual_states[7, 2:7] = HIT
    win._check_fleet()
    assert win.rules.fleet == YANDEX_FLEET


def test_autoplay_does_not_claim_victory_on_an_unconfirmed_sinking(window):
    """Ложная «победа» (последний «однопалубный» — на самом деле пятипалубный) не выключает
    автоигру: сначала проверка флота, потом правила переключаются и игра продолжается."""
    app, win = window
    _calibrate(win, "notebook")
    st = parse_board(ENDGAME)
    win.tracker.states = st.copy()
    win.tracker.pending[:] = -1
    win.last_status = "ok"
    win.halo_watch.seen = True
    win.analysis, win.analysis_src = analyze(st, win.rules, budget=0.2), st.copy()
    assert win.analysis.mode == "done"
    win._check_fleet(now=50.0)
    seen = win._auto_seen()
    assert seen.fresh and not seen.done and seen.best is None       # не «победа»: ждём проверки
    win._check_fleet(now=50.0 + fleetcheck.SOFT_WAIT + 0.5)
    assert win.rules.fleet == YANDEX_FLEET


def test_conflict_warning_clears_when_board_is_fixed(window):
    app, win = window
    win.ed_fleet.setText("1x4 2x3 3x2 3x1")
    win._rules_edited()
    win.manual_states[7, 2:7] = HIT
    win._check_fleet()
    assert win._fleet_note_kind == "conflict" and win.warn.text().startswith("⚠")
    win.manual_states[7, 6] = MISS                                # ошибся с клеткой — исправил
    win._check_fleet()
    assert win._fleet_note == ""
