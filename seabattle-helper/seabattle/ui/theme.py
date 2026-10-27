"""Неоновая тема оформления."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont

BG = "#0b0f1a"
PANEL = "#111829"
PANEL2 = "#161f36"
BORDER = "#22305a"
TEXT = "#e6f1ff"
MUTED = "#8aa0c8"
CYAN = "#00f0ff"
MAGENTA = "#ff2bd6"
GREEN = "#39ff14"
AMBER = "#ffb020"
RED = "#ff3b5c"

HEAT_STOPS = [
    (0.00, QColor("#121b33")),
    (0.35, QColor("#2a1f6e")),
    (0.65, QColor("#8a2be2")),
    (0.85, QColor("#ff2bd6")),
    (1.00, QColor("#ffc4f2")),
]


def heat_color(t: float) -> QColor:
    t = max(0.0, min(1.0, float(t)))
    for (t0, c0), (t1, c1) in zip(HEAT_STOPS, HEAT_STOPS[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return QColor(int(c0.red() + (c1.red() - c0.red()) * k),
                          int(c0.green() + (c1.green() - c0.green()) * k),
                          int(c0.blue() + (c1.blue() - c0.blue()) * k))
    return HEAT_STOPS[-1][1]


def ui_font(size: int = 10, bold: bool = False) -> QFont:
    f = QFont("Segoe UI")
    f.setStyleHint(QFont.SansSerif)
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif;
    font-size: 10pt;
}}
QToolTip {{
    background: {PANEL2}; color: {TEXT}; border: 1px solid {CYAN}; padding: 4px;
}}
#Frame {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
#TitleBar {{ background: transparent; }}
#Title {{ color: {CYAN}; font-weight: 700; font-size: 10.5pt; background: transparent; }}
#TitleBtn {{
    background: transparent; border: none; color: {MUTED};
    font-size: 11pt; min-width: 26px; max-width: 26px; min-height: 22px; border-radius: 6px;
}}
#TitleBtn:hover {{ background: {PANEL2}; color: {TEXT}; }}
#TitleBtn:checked {{ color: {CYAN}; }}
#CloseBtn {{
    background: transparent; border: none; color: {MUTED};
    font-size: 11pt; min-width: 26px; max-width: 26px; min-height: 22px; border-radius: 6px;
}}
#CloseBtn:hover {{ background: {RED}; color: white; }}
#Hint {{ font-size: 22pt; font-weight: 800; color: {CYAN}; background: transparent; }}
#HintPct {{ font-size: 15pt; font-weight: 700; color: {MAGENTA}; background: transparent; }}
#Sub {{ color: {MUTED}; background: transparent; }}
#Warn {{ color: {AMBER}; background: transparent; }}
#Status {{ color: {MUTED}; background: transparent; font-size: 9pt; }}
#Card {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px;
}}
QLabel {{ background: transparent; }}
QPushButton {{
    background: {PANEL2};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 10px;
    color: {TEXT};
}}
QPushButton:hover {{ border-color: {CYAN}; color: white; }}
QPushButton:pressed {{ background: #0f2a3a; }}
QPushButton:checked {{ border-color: {MAGENTA}; color: {MAGENTA}; }}
QPushButton:disabled {{ color: #4d5b7a; border-color: #1a2440; }}
QPushButton#Primary {{
    background: #06323a; border: 1px solid {CYAN}; color: {CYAN}; font-weight: 700;
}}
QPushButton#Primary:hover {{ background: #0a4652; color: white; }}
QPushButton#AutoBtn {{
    background: #2a0b2c; border: 1px solid {MAGENTA}; color: {MAGENTA}; font-weight: 700;
    padding: 4px 8px; border-radius: 8px;
}}
QPushButton#AutoBtn:hover {{ background: #3d1040; color: white; }}
QPushButton#AutoBtn:checked {{ background: {MAGENTA}; color: #12051a; }}
#AutoInfo {{ color: {MAGENTA}; background: transparent; font-size: 9pt; }}
QTabWidget::pane {{ border: none; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; padding: 5px 8px; margin-right: 1px;
    border: none; border-bottom: 2px solid transparent; font-weight: 600;
}}
QTabBar::tab:selected {{ color: {CYAN}; border-bottom: 2px solid {CYAN}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
    background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 3px 6px;
    selection-background-color: {MAGENTA};
}}
QComboBox:hover, QLineEdit:hover, QSpinBox:hover {{ border-color: {CYAN}; }}
QComboBox QAbstractItemView {{ background: {PANEL2}; border: 1px solid {CYAN}; }}
QCheckBox, QRadioButton {{ spacing: 6px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px; border: 1px solid {MUTED}; background: {PANEL2};
}}
QCheckBox::indicator {{ border-radius: 3px; }}
QRadioButton::indicator {{ border-radius: 7px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {CYAN}; border-color: {CYAN};
}}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {CYAN}; width: 14px; margin: -6px 0; border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {MAGENTA}; border-radius: 2px; }}
QGroupBox {{
    border: 1px solid {BORDER}; border-radius: 10px; margin-top: 14px; padding: 8px 8px 6px 8px;
    font-weight: 700; color: {CYAN};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QMenu {{ background: {PANEL2}; border: 1px solid {CYAN}; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: #0a4652; }}
QPlainTextEdit {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; }}
"""
