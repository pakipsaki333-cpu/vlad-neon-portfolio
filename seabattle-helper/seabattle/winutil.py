"""Мелочи, специфичные для Windows (на других системах — безопасные заглушки)."""
from __future__ import annotations

import sys

IS_WIN = sys.platform.startswith("win")

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
WDA_NONE = 0x00
WDA_EXCLUDEFROMCAPTURE = 0x11   # Windows 10 2004+. НИКОГДА не 0x01 (WDA_MONITOR) — окно станет чёрным в захвате

HOTKEYS = {
    1: ("H", "Показать/спрятать помощника"),
    2: ("O", "Подсказка поверх игры вкл/выкл"),
    3: ("N", "Новая партия"),
    4: ("R", "Найти поле заново"),
}


def set_app_id(app_id: str = "SeaBattleHelper.App") -> None:
    """Своя иконка в панели задач, а не иконка python.exe."""
    if not IS_WIN:
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def exclude_from_capture(hwnd: int, on: bool = True) -> bool:
    """Спрятать окно от захвата экрана (и от самого помощника, и от записи/стримов)."""
    if not IS_WIN or not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        return bool(user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd),
                                                    WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE))
    except Exception:
        return False


def register_hotkeys(hwnd: int) -> list:
    """Ctrl+Alt+<буква>. Возвращает список id, которые удалось зарегистрировать."""
    if not IS_WIN or not hwnd:
        return []
    ok = []
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        for hid, (key, _) in HOTKEYS.items():
            if user32.RegisterHotKey(ctypes.c_void_p(hwnd), hid,
                                     MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord(key)):
                ok.append(hid)
    except Exception:
        pass
    return ok


def unregister_hotkeys(hwnd: int, ids) -> None:
    if not IS_WIN or not hwnd:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        for hid in ids:
            user32.UnregisterHotKey(ctypes.c_void_p(hwnd), hid)
    except Exception:
        pass


def hotkey_from_native(event_type, message):
    """Разбор nativeEvent: id горячей клавиши или None."""
    if not IS_WIN:
        return None
    try:
        if bytes(event_type) != b"windows_generic_MSG":
            return None
        import ctypes
        from ctypes import wintypes
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY:
            return int(msg.wParam)
    except Exception:
        return None
    return None


def make_click_through(hwnd: int) -> None:
    """Окно-оверлей пропускает мышь насквозь (дублируем флаг Qt через WinAPI для надёжности)."""
    if not IS_WIN or not hwnd:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
        get = user32.GetWindowLongPtrW if hasattr(user32, "GetWindowLongPtrW") else user32.GetWindowLongW
        set_ = user32.SetWindowLongPtrW if hasattr(user32, "SetWindowLongPtrW") else user32.SetWindowLongW
        get.restype = ctypes.c_ssize_t
        get.argtypes = [ctypes.c_void_p, ctypes.c_int]
        set_.restype = ctypes.c_ssize_t
        set_.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        style = get(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
        set_(ctypes.c_void_p(hwnd), GWL_EXSTYLE,
             style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    except Exception:
        pass
