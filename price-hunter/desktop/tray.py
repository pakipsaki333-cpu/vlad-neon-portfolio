"""System-tray icon via pystray (optional dependency).

If pystray/Pillow are unavailable the app still works — it just closes
instead of minimizing to tray. Tray menu clicks are delivered as string
commands into a queue the GUI polls (thread-safe, unlike touching Tk
widgets from the tray thread).
"""
from __future__ import annotations

import queue
import threading


class TrayIcon:
    def __init__(self, icon_path: str | None = None):
        self.commands: queue.Queue[str] = queue.Queue()
        self._icon = None
        self._icon_path = icon_path

    @property
    def available(self) -> bool:
        try:
            import PIL.Image  # noqa: F401
            import pystray  # noqa: F401
            return True
        except Exception:
            return False

    def _image(self):
        from PIL import Image, ImageDraw
        if self._icon_path:
            try:
                return Image.open(self._icon_path)
            except Exception:
                pass
        img = Image.new("RGBA", (64, 64), (7, 8, 18, 255))
        d = ImageDraw.Draw(img)
        d.ellipse([6, 6, 58, 58], outline=(0, 240, 255), width=5)
        d.ellipse([22, 22, 42, 42], outline=(255, 46, 136), width=4)
        d.ellipse([29, 29, 35, 35], fill=(43, 255, 136))
        return img

    def start(self) -> bool:
        if not self.available:
            return False
        import pystray

        def cmd(name):
            return lambda icon, item: self.commands.put(name)

        menu = pystray.Menu(
            pystray.MenuItem("Открыть Price Hunter", cmd("show"), default=True),
            pystray.MenuItem("Сканировать сейчас", cmd("scan")),
            pystray.MenuItem("Старт / Пауза", cmd("toggle")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", cmd("quit")),
        )
        self._icon = pystray.Icon("PriceHunter", self._image(),
                                  "Price Hunter — охотник за ценами WB", menu)
        try:
            # Preferred on Windows next to another mainloop (Tkinter).
            self._icon.run_detached()
        except Exception:
            threading.Thread(target=self._icon.run, daemon=True).start()
        return True

    def notify(self, title: str, message: str) -> None:
        if self._icon is not None:
            try:
                self._icon.notify(message[:250], title)
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def drain_commands(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.commands.get_nowait())
            except queue.Empty:
                return out
