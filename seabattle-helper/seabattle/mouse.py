"""Мышь для автоигры: настоящие щелчки в Windows (SendInput) и заглушка для других систем.

Координаты — физические пиксели рабочего стола (те же, что на снимке экрана).
"""
from __future__ import annotations

import sys
import time

IS_WIN = sys.platform.startswith("win")

INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
SM_SWAPBUTTON = 23


class Mouse:
    """Мыши нет (не Windows): автоигра недоступна, всё остальное работает."""
    available = False
    real = False                      # настоящий системный курсор (можно сверить положение через Qt)
    why = "Автоигра умеет щёлкать мышью только в Windows."

    def pos(self):
        return None

    def move(self, x: int, y: int) -> bool:
        return False

    def click(self, x: int, y: int) -> bool:
        return False


class WinMouse(Mouse):
    available = True
    real = True
    why = ""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self._INPUT, self._MOUSEINPUT, self._POINT = INPUT, MOUSEINPUT, wintypes.POINT
        u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u.SendInput.restype = wintypes.UINT
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.SetCursorPos.restype = wintypes.BOOL
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.GetCursorPos.restype = wintypes.BOOL
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        self._u = u

    def pos(self):
        pt = self._POINT()
        if self._u.GetCursorPos(self._ct.byref(pt)):
            return int(pt.x), int(pt.y)
        return None

    def move(self, x: int, y: int) -> bool:
        return bool(self._u.SetCursorPos(int(round(x)), int(round(y))))

    def _button(self, flags: int) -> bool:
        inp = self._INPUT()
        inp.type = INPUT_MOUSE
        inp.mi = self._MOUSEINPUT(0, 0, 0, flags, 0, 0)
        return self._u.SendInput(1, self._ct.byref(inp), self._ct.sizeof(self._INPUT)) == 1

    def click(self, x: int, y: int) -> bool:
        """Навести, подождать (сайт должен «увидеть» курсор над клеткой), нажать и отпустить."""
        if not self.move(x, y):
            return False
        # SendInput шлёт «физическую» кнопку: у левшей (кнопки поменяны местами) основная — правая
        swapped = bool(self._u.GetSystemMetrics(SM_SWAPBUTTON))
        down, up = ((MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if swapped
                    else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
        time.sleep(0.04)
        self.move(x, y)
        ok = self._button(down)
        time.sleep(0.06)
        ok = self._button(up) and ok
        return ok


class FakeMouse(Mouse):
    """Для тестов и самопроверки без Windows: «щелчок» уходит в функцию on_click(x, y)."""
    available = True
    why = ""

    def __init__(self, on_click):
        self.on_click = on_click
        self._pos = (0, 0)

    def pos(self):
        return self._pos

    def move(self, x: int, y: int) -> bool:
        self._pos = (int(round(x)), int(round(y)))
        return True

    def click(self, x: int, y: int) -> bool:
        self.move(x, y)
        self.on_click(*self._pos)
        return True


def default_mouse() -> Mouse:
    if IS_WIN:
        try:
            return WinMouse()
        except Exception as ex:           # очень старая Windows или урезанная среда
            m = Mouse()
            m.why = f"Не удалось подключиться к мыши: {ex}"
            return m
    return Mouse()
