"""Windows autostart via HKCU\\...\\Run (no admin rights needed).

On other OSes everything degrades to "not supported" so the same code
can be imported and tested on Linux/macOS.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "PriceHunter"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_supported() -> bool:
    return sys.platform == "win32"


def _launch_command() -> str:
    if getattr(sys, "frozen", False):              # packaged .exe
        return f'"{sys.executable}" --minimized'
    script = Path(__file__).resolve().parent.parent / "desktop_app.py"
    # pythonw = no console window on Windows when running from sources.
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else exe
    return f'"{runner}" "{script}" --minimized'


def is_enabled() -> bool:
    if not is_supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except Exception:
        return False


def set_enabled(enabled: bool) -> bool:
    """Returns True on success."""
    if not is_supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                                  _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False
