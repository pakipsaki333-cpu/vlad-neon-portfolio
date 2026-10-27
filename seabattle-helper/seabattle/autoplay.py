"""Автоигра: когда и куда щёлкнуть, чтобы помощник сам стрелял по полю соперника.

Здесь только логика, без Qt и без мыши: главное окно раз в ~0.1 с сообщает, что видит
(Seen), и щёлкает туда, куда скажет AutoPilot.step(). Поэтому её легко проверить тестами.

Как играем:
  * стреляем только когда поле найдено и «спокойно» (ничего не меняется, нет затемнения),
    а подсказка посчитана именно для того, что сейчас на поле;
  * после щелчка ждём, пока сайт поставит отметку в эту клетку (мимо / ранил / убил);
  * сайт не ответил — скорее всего, сейчас ход соперника: ждём и щёлкаем ещё раз
    (всё реже: живой соперник может думать долго);
  * совсем долго нет ответа — останавливаемся и объясняем почему.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .rules import HIT, MISS, SUNK, UNKNOWN

RESULT_WORD = {MISS: "мимо", HIT: "ранил", SUNK: "убил"}


@dataclass
class Seen:
    """Что видит главное окно прямо сейчас."""
    ready: bool                       # поле найдено, на месте, слежение не на паузе
    states: np.ndarray | None         # отметки на поле соперника
    best: tuple | None = None         # куда советует бить решатель — посчитано для этих states
    fresh: bool = False               # подсказка посчитана для текущих states (а не для старых)
    done: bool = False                # все корабли соперника потоплены
    busy: bool = False                # на поле что-то меняется / поле затемнено (чужой ход, анимация)
    stuck: str = ""                   # решатель не понимает поле (противоречие) — объяснение


class AutoPilot:
    IDLE, READY, WAIT = "idle", "ready", "wait"

    def __init__(self, delay: float = 0.8, wait_result: float = 3.0, retry: float = 1.5,
                 give_up: float = 90.0, first_delay: float = 2.0):
        self.delay = delay                # пауза перед каждым выстрелом, с
        self.wait_result = wait_result    # сколько ждать отметку после щелчка, с
        self.retry = retry                # пауза перед повторным щелчком, если сайт не ответил, с
        self.give_up = give_up            # столько секунд сайт не отмечает выстрел → стоп
        self.first_delay = first_delay    # отсчёт перед первым выстрелом (убрать руку с мыши)
        self.phase = self.IDLE
        self.target = None
        self.shot_at = 0.0
        self.next_at = 0.0
        self.started_at = 0.0
        self.tries = 0                    # щелчков подряд в текущую цель без ответа
        self.first_try_at = 0.0           # когда впервые щёлкнули в текущую цель
        self.clicks = 0                   # всего щелчков
        self.shots = 0                    # выстрелов, на которые сайт ответил отметкой
        self.results = {MISS: 0, HIT: 0, SUNK: 0}
        self.last_result = None
        self.note = ""

    # ------------------------------------------------------------------ управление
    @property
    def active(self) -> bool:
        return self.phase != self.IDLE

    def start(self, now: float):
        self.phase = self.READY
        self.started_at = now
        self.next_at = now + self.first_delay
        self.target = None
        self.tries = 0
        self.note = "Автоигра включена — убери руку с мыши."

    def stop(self, note: str = ""):
        self.phase = self.IDLE
        self.target = None
        self.note = note

    def countdown(self, now: float) -> float:
        """Сколько секунд осталось до первого выстрела (0 — уже играем)."""
        if self.phase != self.READY or self.clicks or now >= self.next_at:
            return 0.0
        return self.next_at - now

    # ------------------------------------------------------------------ шаг
    def step(self, now: float, seen: Seen):
        """→ (строка, столбец), куда щёлкнуть прямо сейчас, или None."""
        if self.phase == self.IDLE:
            return None
        st = seen.states
        if self.phase == self.WAIT:
            r, c = self.target
            if st is not None and st[r, c] != UNKNOWN:
                res = int(st[r, c])
                self.last_result = res
                self.shots += 1
                self.results[res] = self.results.get(res, 0) + 1
                self.tries = 0
                self.phase = self.READY
                self.next_at = now + self.delay
                self.note = f"Выстрел: {RESULT_WORD.get(res, '?')}."
                return None
            if now - self.shot_at < self.wait_result:
                return None
            # сайт не поставил отметку: ход соперника, щелчок не дошёл или отметку не видно
            self.phase = self.READY
            waited = now - self.first_try_at
            if waited >= self.give_up:
                self.stop(f"Сайт {waited:.0f} с не отмечает выстрел — автоигра остановлена. "
                          "Проверь, что поле найдено верно («Что видит») и что сейчас твой ход.")
                return None
            self.next_at = now + min(5.0, self.retry * (1 + 0.5 * (self.tries - 1)))
            self.note = f"Сайт не отметил выстрел — наверное, ход соперника. Жду ({waited:.0f} с)…"
            return None

        # READY
        if seen.fresh and seen.done:
            self.stop("Все корабли соперника потоплены — автоигра выключена. Победа! 🎉")
            return None
        if seen.fresh and seen.stuck:
            self.stop("Автоигра остановлена: " + seen.stuck)
            return None
        if now < self.next_at:
            return None
        if not seen.ready or seen.busy or not seen.fresh or seen.best is None or st is None:
            return None
        r, c = seen.best
        if st[r, c] != UNKNOWN:
            return None
        if self.target != (r, c) or self.tries == 0:
            self.tries = 0
            self.first_try_at = now
        self.target = (r, c)
        self.tries += 1
        self.clicks += 1
        self.shot_at = now
        self.phase = self.WAIT
        self.note = "Стреляю…" if self.tries == 1 else f"Стреляю ещё раз (попытка {self.tries})…"
        return (r, c)
