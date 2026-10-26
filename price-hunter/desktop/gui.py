"""Tkinter GUI for Price Hunter (Windows desktop app).

Stdlib-only UI (tkinter ships with Python and bundles cleanly into a
PyInstaller .exe). All business logic sits in desktop.controller —
this file only renders state and forwards user actions.
"""
from __future__ import annotations

import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from app.telegram import fmt_rub

from . import autostart
from .controller import DesktopController
from .tray import TrayIcon

BG, PANEL, PANEL2 = "#070812", "#101024", "#16162e"
NEON, PINK, GREEN, AMBER = "#00f0ff", "#ff2e88", "#2bff88", "#ffb02e"
TEXT, MUT, LINE = "#eef0ff", "#8b8fb3", "#26264a"
LEVEL_ICON = {"FIRE": "!!! ОШИБКА ЦЕНЫ", "HOT": "! Резкое падение", "WATCH": "Подозрительно дёшево"}
MARKETPLACE_LABELS = {"wb": "🟣 WB", "ozon": "🔵 Ozon"}
MARKETPLACE_ID_LABELS = {"wb": "Арт. WB", "ozon": "SKU Ozon"}


def _marketplace_of(item: dict) -> str:
    marketplace = str((item or {}).get("marketplace") or "").lower()
    if marketplace in MARKETPLACE_LABELS:
        return marketplace
    return "ozon" if "ozon." in str((item or {}).get("url") or "") else "wb"


def _marketplace_label(item_or_key) -> str:
    key = _marketplace_of(item_or_key) if isinstance(item_or_key, dict) \
        else str(item_or_key or "wb").lower()
    return MARKETPLACE_LABELS.get(key, MARKETPLACE_LABELS["wb"])


def _run_source_label(source) -> str:
    raw = str(source or "")
    if raw.startswith("live:"):
        labels = [MARKETPLACE_LABELS.get(x, x.upper())
                  for x in raw[5:].split("+") if x]
        return "LIVE " + "+".join(labels)
    return raw.upper() if raw else "—"


def _fmt_ts(ts) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "—"


def _alert_reference(alert: dict) -> float:
    """Normal-price reference for display: history median when known."""
    return float(alert.get("hist_median") or alert.get("price")
                 or alert.get("price_rub") or 0)


def _alert_drop(alert: dict) -> float:
    """History drop when known; sticker discount only for legacy alerts."""
    drop = alert.get("drop_vs_median_pct")
    return float(drop if drop is not None else alert.get("discount_pct") or 0)


def _alert_history_confirmed(alert: dict) -> bool:
    if "history_confirmed" in alert:
        return bool(alert.get("history_confirmed"))
    return bool((alert.get("hist_median") or 0) > 0
                and (alert.get("drop_vs_median_pct") or 0) > 0)


def _history_only_enabled(settings: dict | None) -> bool:
    value = (settings or {}).get("history_only", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def _alert_reference_text(alert: dict) -> str:
    confirmed = _alert_history_confirmed(alert)
    label = "медиана истории" if alert.get("hist_median") else "цена карточки"
    text = f"{label} {fmt_rub(_alert_reference(alert))}"
    if not confirmed:
        text += " · без подтверждения историей"
    if alert.get("hist_min"):
        text += f" · минимум площадки {fmt_rub(float(alert['hist_min']))}"
        vs_min = alert.get("vs_hist_min_pct")
        if vs_min is not None and float(vs_min) > 0:
            text += " · новый минимум"
    return text


class PriceHunterApp:
    def __init__(self, root: tk.Tk, ctrl: DesktopController,
                 minimized: bool = False, icon_path: str | None = None):
        self.root, self.ctrl = root, ctrl
        self.tray = TrayIcon(icon_path)
        self._tray_on = self.tray.start()
        self._top_alert_id = -1
        self._alert_filter_key: tuple[bool, bool] | None = None
        self._top_run_id = -1
        self._alert_by_iid: dict[str, dict] = {}
        self.var_show_unconfirmed_alerts = tk.BooleanVar(value=False)
        cfg0 = self.ctrl.settings.get()
        try:
            lowest_pages = max(1, min(3, int(cfg0.get("pages_per_query", 2) or 2)))
        except Exception:
            lowest_pages = 2
        self.var_lowest_query = tk.StringVar(value="")
        self.var_lowest_wb = tk.BooleanVar(value=bool(cfg0.get("wb_enabled", True)))
        self.var_lowest_ozon = tk.BooleanVar(value=bool(cfg0.get("ozon_enabled", True)))
        self.var_lowest_pages = tk.StringVar(value=str(lowest_pages))
        self.var_lowest_strict = tk.BooleanVar(value=False)
        self.var_lowest_record = tk.BooleanVar(value=True)
        self.var_lowest_status = tk.StringVar(
            value="Введи товар и нажми «Найти самую низкую цену».")
        self.var_lowest_summary = tk.StringVar(value="")
        self.var_lowest_details = tk.StringVar(
            value="Здесь будут подробности выбранного предложения.")
        self._lowest_by_iid: dict[str, dict] = {}
        self._lowest_best: dict | None = None
        self._lowest_searching = False
        self._toasts: list[tk.Toplevel] = []
        self._tray_hint_shown = False
        self._dialog_busy = False
        self.v: dict[str, tk.Variable] = {}

        root.title("Price Hunter — ценовые ошибки WB + Ozon")
        root.geometry("1180x780")
        root.minsize(960, 640)
        root.configure(bg=BG)
        self._make_styles()
        self._build_header()
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._build_home()
        self._build_finds()
        self._build_lowest()
        self._build_settings()
        self._build_journal()
        self._load_settings_form()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.ctrl.start_scanner()
        if (minimized or self.ctrl.settings.get().get("start_minimized")) \
                and self._tray_on:
            root.withdraw()
        root.after(300, self._poll)

    # ------------------------------------------------------------------ #
    def _make_styles(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, bordercolor=LINE)
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Mut.TLabel", background=BG, foreground=MUT)
        s.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        s.configure("Big.TLabel", background=PANEL, foreground=TEXT,
                    font=("Segoe UI", 22, "bold"))
        s.configure("TButton", background=PANEL2, foreground=TEXT,
                    padding=(12, 7), font=("Segoe UI", 10, "bold"))
        s.map("TButton", background=[("active", "#22224a")])
        s.configure("Accent.TButton", background="#17456b", foreground="white",
                    padding=(14, 8), font=("Segoe UI", 10, "bold"))
        s.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        s.map("TCheckbutton", background=[("active", PANEL)])
        s.configure("TEntry", fieldbackground="#0b0b1c", foreground=TEXT,
                    insertcolor=TEXT, padding=5)
        s.configure("TCombobox", fieldbackground="#0b0b1c", foreground=TEXT, padding=5)
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=PANEL2, foreground=TEXT,
                    padding=(16, 8), font=("Segoe UI", 10, "bold"))
        s.map("TNotebook.Tab", background=[("selected", "#3a1c5e")])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=TEXT, rowheight=26, borderwidth=0)
        s.configure("Treeview.Heading", background=PANEL2, foreground=MUT,
                    font=("Segoe UI", 9, "bold"))
        s.map("Treeview", background=[("selected", "#3a1c5e")])

    def _build_header(self):
        h = tk.Frame(self.root, bg=PANEL, padx=16, pady=10)
        h.pack(fill="x", padx=10, pady=10)
        tk.Label(h, text="◎ PRICE HUNTER", bg=PANEL, fg=NEON,
                 font=("Segoe UI", 20, "bold")).pack(side="left")
        self.badge_mode = tk.Label(h, text="…", bg=PANEL, fg=AMBER,
                                   font=("Segoe UI", 11, "bold"), padx=10)
        self.badge_mode.pack(side="left")
        self.var_runstate = tk.StringVar(value="")
        tk.Label(h, textvariable=self.var_runstate, bg=PANEL, fg=MUT,
                 font=("Segoe UI", 10)).pack(side="left", padx=6)
        self.btn_toggle = ttk.Button(h, text="Пауза", command=self._toggle)
        self.btn_toggle.pack(side="right", padx=4)
        ttk.Button(h, text="Сканировать сейчас", style="Accent.TButton",
                   command=self.ctrl.scan_now).pack(side="right", padx=4)

    def _tab(self, title: str) -> ttk.Frame:
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text=title)
        return f

    # ------------------------------ Главная --------------------------- #
    def _build_home(self):
        f = self._tab("Главная")
        kpis = ttk.Frame(f)
        kpis.pack(fill="x")
        self.kpi_vars = {}
        self.kpi_label_vars = {}
        for key, label in [("products", "Товаров в базе"), ("alerts", "Находок за 24ч"),
                           ("best", "Лучшее падение vs истории 24ч"), ("saving", "Выгода за 24ч"),
                           ("scans", "Сканирований"), ("snaps", "Снапшотов цен")]:
            card = tk.Frame(kpis, bg=PANEL, padx=14, pady=10,
                            highlightbackground=LINE, highlightthickness=1)
            card.pack(side="left", fill="x", expand=True, padx=4)
            label_var = tk.StringVar(value=label.upper())
            tk.Label(card, textvariable=label_var, bg=PANEL, fg=MUT,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
            var = tk.StringVar(value="—")
            tk.Label(card, textvariable=var, bg=PANEL, fg=TEXT,
                     font=("Segoe UI", 20, "bold")).pack(anchor="w")
            self.kpi_vars[key] = var
            self.kpi_label_vars[key] = label_var

        self.var_status = tk.StringVar(value="")
        tk.Label(f, textvariable=self.var_status, bg=BG, fg=TEXT,
                 font=("Segoe UI", 11), wraplength=1050, justify="left",
                 anchor="w").pack(fill="x", pady=(16, 2))
        self.var_tg_warn = tk.StringVar(value="")
        tk.Label(f, textvariable=self.var_tg_warn, bg=BG, fg=AMBER,
                 font=("Segoe UI", 11, "bold"), wraplength=1050,
                 justify="left", anchor="w").pack(fill="x", pady=2)
        self.var_wb_warn = tk.StringVar(value="")
        self.var_market_warn = self.var_wb_warn
        tk.Label(f, textvariable=self.var_wb_warn, bg=BG, fg=PINK,
                 font=("Segoe UI", 11, "bold"), wraplength=1050,
                 justify="left", anchor="w").pack(fill="x", pady=2)

        actions = ttk.Frame(f)
        actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="Настроить Telegram и пороги",
                   command=lambda: self.nb.select(3)).pack(side="left", padx=4)
        ttk.Button(actions, text="Все находки",
                   command=lambda: self.nb.select(1)).pack(side="left", padx=4)
        ttk.Button(actions, text="🔎 Самая низкая цена",
                   command=lambda: self.nb.select(2)).pack(side="left", padx=4)
        ttk.Button(actions, text="Свернуть в трей",
                   command=self._hide_to_tray).pack(side="left", padx=4)

        market_actions = ttk.Frame(f)
        market_actions.pack(fill="x", pady=(0, 4))
        ttk.Button(market_actions, text="Проверить WB",
                   style="Accent.TButton",
                   command=lambda: self._diagnose_wb()).pack(side="left", padx=4)
        ttk.Button(market_actions, text="Проверить Ozon",
                   style="Accent.TButton",
                   command=lambda: self._diagnose_ozon()).pack(side="left", padx=4)
        ttk.Button(market_actions, text="Открыть браузер WB",
                   command=lambda: self._open_wb_browser()).pack(side="left", padx=4)
        ttk.Button(market_actions, text="Открыть браузер Ozon",
                   command=lambda: self._open_ozon_browser()).pack(side="left", padx=4)

        self.var_recent_title = tk.StringVar(
            value="Последние находки (двойной клик — открыть товар):")
        tk.Label(f, textvariable=self.var_recent_title,
                 bg=BG, fg=MUT, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(10, 4))
        self.lb_recent = tk.Listbox(f, bg=PANEL, fg=TEXT, height=12, borderwidth=0,
                                    highlightthickness=1, highlightbackground=LINE,
                                    selectbackground="#3a1c5e", font=("Segoe UI", 10))
        self.lb_recent.pack(fill="both", expand=True)
        self.lb_recent.bind("<Double-Button-1>", self._open_recent)
        self._recent_urls: list[str] = []

    # ------------------------------ Находки --------------------------- #
    def _build_finds(self):
        f = self._tab("Находки")
        ttk.Checkbutton(
            f,
            text="Показывать старые находки без подтверждения историей (только скидка карточки)",
            variable=self.var_show_unconfirmed_alerts,
            command=lambda: self._refresh_alerts(force=True),
        ).pack(anchor="w", pady=(0, 2))
        tk.Label(
            f, bg=BG, fg=MUT, justify="left", wraplength=1100,
            text=("ℹ Это только фильтр отображения: записи не удаляются. В строгом режиме по умолчанию "
                  "видны находки с медианой истории и реальным падением от неё; старые записи, где "
                  "основанием была только скидка продавца, скрыты и не считаются надёжными находками."),
        ).pack(anchor="w", pady=(0, 8))
        cols = ("time", "level", "marketplace", "name", "sale", "old",
                "hist_min", "disc", "saving", "score")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=18)
        for col, title, w in [("time", "Время", 90), ("level", "Уровень", 130),
                              ("marketplace", "Площадка", 105),
                              ("name", "Товар", 275), ("sale", "Цена", 105),
                              ("old", "Эталон цены", 125),
                              ("hist_min", "Минимум площадки", 125),
                              ("disc", "Падение", 80),
                              ("saving", "Выгода", 110), ("score", "Скор", 60)]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=w, anchor="w" if col == "name" else "center")
        self.tree.tag_configure("FIRE", foreground=PINK)
        self.tree.tag_configure("HOT", foreground=AMBER)
        self.tree.tag_configure("unconfirmed", foreground=MUT)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_alert)
        self.tree.bind("<Double-Button-1>", lambda e: self._open_selected())

        self.var_details = tk.StringVar(value="Выбери находку — здесь будут причины скора.")
        tk.Label(f, textvariable=self.var_details, bg=BG, fg=MUT,
                 wraplength=1080, justify="left", anchor="w").pack(fill="x", pady=6)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="Открыть / Купить", style="Accent.TButton",
                   command=self._open_selected).pack(side="left", padx=4)
        ttk.Button(row, text="История цены", command=self._show_history).pack(side="left", padx=4)
        ttk.Button(row, text="Скрыть", command=self._dismiss_selected).pack(side="left", padx=4)
        ttk.Button(row, text="↻ Обновить", command=lambda: self._refresh_alerts(force=True)).pack(side="left", padx=4)

    # ----------------------- Самая низкая цена ---------------------- #
    def _build_lowest(self):
        f = self._tab("Самая низкая цена")
        tk.Label(
            f, bg=BG, fg=TEXT, justify="left", wraplength=1100,
            font=("Segoe UI", 11, "bold"),
            text="Отдельный ручной поиск самого дешёвого текущего предложения на WB и Ozon",
        ).pack(anchor="w", pady=(0, 4))
        tk.Label(
            f, bg=BG, fg=MUT, justify="left", wraplength=1100,
            text=("Введи максимально точный товар: бренд + модель + вариант, "
                  "например «iPhone 15 128 ГБ» или «Sony WH-1000XM5». Это не "
                  "детектор ценовых ошибок и не Telegram-алерт: вкладка просто "
                  "запрашивает выбранные площадки сейчас, отсекает явные "
                  "аксессуары и сортирует подходящие карточки по текущей цене. "
                  "Перед покупкой всё равно проверь вариант, продавца, доставку "
                  "и итоговую цену в корзине."),
        ).pack(anchor="w", pady=(0, 8))

        form = ttk.Frame(f)
        form.pack(fill="x", pady=4)
        tk.Label(form, text="Товар / модель", bg=BG, fg=MUT,
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 8))
        entry = ttk.Entry(form, textvariable=self.var_lowest_query, width=58)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda _e: self._start_lowest_search())
        self.btn_lowest_search = ttk.Button(
            form, text="🔎 Найти самую низкую цену", style="Accent.TButton",
            command=self._start_lowest_search)
        self.btn_lowest_search.pack(side="left", padx=8)

        options = ttk.Frame(f)
        options.pack(fill="x", pady=4)
        tk.Label(options, text="Площадки:", bg=BG, fg=MUT,
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 6))
        ttk.Checkbutton(options, text="🟣 WB",
                        variable=self.var_lowest_wb).pack(side="left", padx=4)
        ttk.Checkbutton(options, text="🔵 Ozon",
                        variable=self.var_lowest_ozon).pack(side="left", padx=4)
        tk.Label(options, text="Страниц:", bg=BG, fg=MUT,
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(18, 4))
        ttk.Combobox(options, textvariable=self.var_lowest_pages,
                     values=["1", "2", "3"], state="readonly", width=5
                     ).pack(side="left", padx=4)
        ttk.Checkbutton(options, text="Только точные совпадения",
                        variable=self.var_lowest_strict).pack(side="left", padx=10)
        ttk.Checkbutton(options, text="Записывать цены в историю базы",
                        variable=self.var_lowest_record).pack(side="left", padx=4)
        tk.Label(
            f, bg=BG, fg=MUT, justify="left", wraplength=1100,
            text=("Пояснения: «Страниц» — глубина выдачи на каждой площадке и "
                  "в каждой сортировке; больше страниц = шире поиск, но дольше "
                  "и выше риск антибота. «Только точные» не показывает близкие "
                  "варианты, если в карточке нет всех важных слов запроса. "
                  "«Записывать цены» добавляет показанные предложения в "
                  "собственную историю Price Hunter, но не создаёт находок и "
                  "не отправляет Telegram."),
        ).pack(anchor="w", pady=(0, 6))

        tk.Label(f, textvariable=self.var_lowest_summary, bg=BG, fg=GREEN,
                 font=("Segoe UI", 12, "bold"), wraplength=1100,
                 justify="left", anchor="w").pack(fill="x", pady=2)
        tk.Label(f, textvariable=self.var_lowest_status, bg=BG, fg=MUT,
                 wraplength=1100, justify="left", anchor="w"
                 ).pack(fill="x", pady=(0, 6))

        cols = ("rank", "marketplace", "name", "sale", "old", "rating",
                "match", "db_min", "id")
        self.tree_lowest = ttk.Treeview(
            f, columns=cols, show="headings", height=15)
        for col, title, width in [
                ("rank", "№", 42), ("marketplace", "Площадка", 95),
                ("name", "Товар", 285), ("sale", "Текущая цена", 112),
                ("old", "Цена карточки", 142), ("rating", "Рейтинг / отзывы", 125),
                ("match", "Совпадение", 105),
                ("db_min", "Минимум в своей базе", 132),
                ("id", "Арт. / SKU", 112)]:
            self.tree_lowest.heading(col, text=title)
            self.tree_lowest.column(
                col, width=width, anchor="w" if col == "name" else "center")
        self.tree_lowest.tag_configure("cheapest", foreground=GREEN)
        self.tree_lowest.tag_configure("market_best", foreground=NEON)
        self.tree_lowest.pack(fill="both", expand=True)
        self.tree_lowest.bind("<<TreeviewSelect>>", self._on_select_lowest)
        self.tree_lowest.bind("<Double-Button-1>",
                              lambda _e: self._open_lowest_selected())

        tk.Label(f, textvariable=self.var_lowest_details, bg=BG, fg=MUT,
                 wraplength=1100, justify="left", anchor="w"
                 ).pack(fill="x", pady=6)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="Открыть самое дешёвое", style="Accent.TButton",
                   command=self._open_lowest_best).pack(side="left", padx=4)
        ttk.Button(row, text="Открыть выбранное",
                   command=self._open_lowest_selected).pack(side="left", padx=4)

    # ------------------------------ Настройки ------------------------- #
    def _scrollable_tab(self, title: str) -> tk.Frame:
        outer = self._tab(title)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        return inner

    def _section(self, parent, title: str) -> tk.Frame:
        lf = tk.LabelFrame(parent, text=title, bg=PANEL, fg=NEON,
                           font=("Segoe UI", 11, "bold"), padx=12, pady=10,
                           borderwidth=1, relief="solid")
        lf.pack(fill="x", pady=6)
        return lf

    def _field(self, parent, row: int, label: str, var: tk.Variable,
               width: int = 18, show: str | None = None,
               help_text: str = "") -> ttk.Entry:
        tk.Label(parent, text=label, bg=PANEL, fg=MUT,
                 font=("Segoe UI", 9, "bold")).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 8))
        e = ttk.Entry(parent, textvariable=var, width=width, show=show or "")
        e.grid(row=row, column=1, sticky="w", pady=3)
        if help_text:
            self._setting_help(parent, row, help_text)
        return e

    def _setting_help(self, parent, row: int, text: str, column: int = 2,
                      columnspan: int = 1, wraplength: int = 650):
        return tk.Label(
            parent, text=f"ℹ {text}", bg=PANEL, fg=MUT,
            wraplength=wraplength, justify="left",
        ).grid(row=row, column=column, columnspan=columnspan,
               sticky="w", pady=3, padx=8)

    def _svar(self, key: str, value: str = "") -> tk.StringVar:
        var = tk.StringVar(value=value)
        self.v[key] = var
        return var

    def _bvar(self, key: str, value: bool = False) -> tk.BooleanVar:
        var = tk.BooleanVar(value=value)
        self.v[key] = var
        return var

    def _build_settings(self):
        f = self._scrollable_tab("Настройки")
        # Telegram
        tg = self._section(f, "Telegram — куда слать алерты")
        ttk.Checkbutton(tg, text="Telegram включён",
                        variable=self._bvar("telegram_enabled", True)).grid(row=0, column=0, sticky="w", columnspan=2)
        self._setting_help(
            tg, 0, "Если выключено, находки только сохраняются в приложении; мгновенные сообщения в Telegram не отправляются.")
        self.ent_token = self._field(
            tg, 1, "Токен бота (@BotFather)",
            self._svar("telegram_token"), width=52, show="*",
            help_text="Секретный ключ твоего бота. Пустое поле при сохранении оставляет ранее сохранённый токен; никому его не отправляй.")
        self.var_token_hint = tk.StringVar(value="")
        tk.Label(tg, textvariable=self.var_token_hint, bg=PANEL, fg=MUT).grid(row=2, column=2, sticky="w", padx=8)
        show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tg, text="Показать токен", variable=show_var,
                        command=lambda: self.ent_token.configure(
                            show="" if show_var.get() else "*")).grid(row=2, column=1, sticky="w")
        self._field(
            tg, 3, "Chat ID (@userinfobot)", self._svar("telegram_chat_id"),
            width=30,
            help_text="Числовой ID чата, куда бот пришлёт ссылку на товар. Сначала открой своего бота и нажми /start, иначе Telegram не даст ему написать.")
        tk.Label(tg, bg=PANEL, fg=MUT, justify="left", wraplength=980,
                 text="1) @BotFather → /newbot → токен. 2) Открой бота и нажми /start. "
                      "3) Chat ID пришлёт @userinfobot. Пустой токен при сохранении = оставить ранее сохранённый."
                 ).grid(row=4, column=0, columnspan=3, sticky="w", pady=4)
        self.var_tg_test = tk.StringVar(value="")
        ttk.Button(tg, text="Тест Telegram", command=self._test_telegram).grid(row=5, column=1, sticky="w", pady=4)
        tk.Label(tg, textvariable=self.var_tg_test, bg=PANEL, fg=TEXT).grid(row=5, column=2, sticky="w")

        # Scanning
        sc = self._section(f, "Сканирование WB + Ozon")
        tk.Label(sc, text="Режим", bg=PANEL, fg=MUT, font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", pady=3, padx=(0, 8))
        ttk.Combobox(sc, textvariable=self._svar("mode", "auto"), state="readonly",
                     values=["auto", "live", "demo"], width=16).grid(row=0, column=1, sticky="w", pady=3)
        tk.Label(sc, text="live = только настоящие WB/Ozon; auto = живые источники, а DEMO только если недоступны все включённые; demo = симуляция, товары не ищутся",
                 bg=PANEL, fg=MUT, wraplength=620, justify="left").grid(row=0, column=2, sticky="w", padx=8)
        tk.Label(sc, text="Маркетплейсы", bg=PANEL, fg=MUT,
                 font=("Segoe UI", 9, "bold")).grid(row=1, column=0, sticky="w", pady=3, padx=(0, 8))
        markets_row = tk.Frame(sc, bg=PANEL)
        markets_row.grid(row=1, column=1, sticky="w", columnspan=2)
        ttk.Checkbutton(markets_row, text="🟣 Wildberries",
                        variable=self._bvar("wb_enabled", True)).pack(side="left", padx=6)
        ttk.Checkbutton(markets_row, text="🔵 Ozon",
                        variable=self._bvar("ozon_enabled", True)).pack(side="left", padx=6)
        tk.Label(markets_row, text="Источники независимы: падение одного не отменяет товары другого",
                 bg=PANEL, fg=MUT).pack(side="left", padx=8)
        self._field(
            sc, 2, "Интервал, сек (минимум 60)", self._svar("interval_sec"),
            help_text="Пауза после полного прохода всех запросов. Меньше — быстрее заметишь падение, но выше нагрузка и риск антибота; для Chrome/DOM спокойнее 600+ сек.")
        self._field(
            sc, 3, "Страниц на запрос", self._svar("pages_per_query"),
            help_text="Сколько страниц выдачи читать по каждому запросу и сортировке. Больше — шире охват, но проход дольше и запросов к площадке заметно больше.")
        tk.Label(sc, text="Сортировки выдачи", bg=PANEL, fg=MUT,
                 font=("Segoe UI", 9, "bold")).grid(row=4, column=0, sticky="w", pady=3, padx=(0, 8))
        sorts_row = tk.Frame(sc, bg=PANEL)
        sorts_row.grid(row=4, column=1, sticky="w", columnspan=2)
        for key, lbl in [("sort_popular", "popular — популярные"),
                         ("sort_benefit", "benefit — по выгоде"),
                         ("sort_priceup", "priceup — сначала дешёвые")]:
            ttk.Checkbutton(sorts_row, text=lbl, variable=self._bvar(key)).pack(side="left", padx=6)
        tk.Label(sorts_row, bg=PANEL, fg=MUT,
                 text="Каждая сортировка — отдельные запросы к площадке").pack(side="left", padx=8)
        self._field(
            sc, 5, "Регион WB (dest)", self._svar("dest"),
            help_text="−1257786 = Москва. Влияет на регион/цены/наличие только WB; у Ozon город выбирается в его браузерной вкладке.")
        self._field(
            sc, 6, "СПП/WB Кошелёк, %", self._svar("spp"),
            help_text="Параметр WB для цен с учётом СПП/WB Кошелька в API-запросах. Не является доказательством скидки и не заменяет историю; обычно оставляют 30.")

        tk.Label(sc, text="Транспорт WB", bg=PANEL, fg=MUT,
                 font=("Segoe UI", 9, "bold")).grid(row=7, column=0, sticky="w", pady=3, padx=(0, 8))
        ttk.Combobox(sc, textvariable=self._svar("wb_transport", "auto"),
                     state="readonly", values=["auto", "chrome", "browser", "httpx"],
                     width=16).grid(row=7, column=1, sticky="w", pady=3)
        tk.Label(sc, text="auto: httpx → curl_cffi → настоящий Chrome/Edge/DOM. На ПК, где работает только chrome-dom, удобнее сразу chrome",
                 bg=PANEL, fg=MUT, wraplength=620, justify="left").grid(row=7, column=2, sticky="w", padx=8)
        ttk.Checkbutton(sc, text="WB: в auto при блоке открывать настоящий Chrome/Edge",
                        variable=self._bvar("wb_browser_fallback", True)).grid(
                            row=8, column=1, sticky="w", pady=3)
        self._setting_help(
            sc, 8, "Если обычные HTTP-запросы WB получают блок, auto попробует живой браузер. Выключай только если принципиально не хочешь открывать окно браузера.")
        self._field(
            sc, 9, "Пауза WB, сек", self._svar("wb_request_delay_sec"),
            help_text="Задержка между запросами к WB. При 403/429 или долгих DOM-проходах увеличь до 2–3 сек.")

        tk.Label(sc, text="Транспорт Ozon", bg=PANEL, fg=MUT,
                 font=("Segoe UI", 9, "bold")).grid(row=10, column=0, sticky="w", pady=3, padx=(0, 8))
        ttk.Combobox(sc, textvariable=self._svar("ozon_transport", "auto"),
                     state="readonly", values=["auto", "chrome", "browser", "httpx"],
                     width=16).grid(row=10, column=1, sticky="w", pady=3)
        tk.Label(sc, text="Ozon чаще пропускает только настоящий браузер: composer JSON внутри вкладки, затем отрисованный DOM. Если httpx/browser дают 403/307 — выбери chrome",
                 bg=PANEL, fg=MUT, wraplength=620, justify="left").grid(row=10, column=2, sticky="w", padx=8)
        ttk.Checkbutton(sc, text="Ozon: в auto при блоке открывать настоящий Chrome/Edge",
                        variable=self._bvar("ozon_browser_fallback", True)).grid(
                            row=11, column=1, sticky="w", pady=3)
        self._setting_help(
            sc, 11, "Если Ozon отвечает 403/307 обычным запросам, auto перейдёт в настоящий браузер и его отдельную вкладку Ozon.")
        self._field(
            sc, 12, "Пауза Ozon, сек", self._svar("ozon_request_delay_sec"),
            help_text="Задержка между запросами к Ozon. Держи минимум 2–3 сек и обычно 1 страницу на запрос: Ozon заметно строже к частоте.")

        self._field(sc, 13, "Путь к Chrome/Edge (необязательно)",
                    self._svar("wb_browser_path"), width=52)
        tk.Label(sc, text="Общий браузер WB+Ozon; пусто = автопоиск chrome.exe/msedge.exe",
                 bg=PANEL, fg=MUT).grid(row=13, column=2, sticky="w", padx=8)
        self._field(
            sc, 14, "Порт браузера", self._svar("wb_browser_port"),
            help_text="Локальный порт управления отдельным Chrome/Edge Price Hunter. Меняй только при конфликте портов; после смены перезапусти браузер приложения.")
        self._field(
            sc, 15, "Ожидание браузера, сек", self._svar("wb_browser_wait_sec"),
            help_text="Сколько ждать загрузки/ответа браузерной вкладки. На медленном ПК или долгой проверке площадки увеличь; слишком большое значение замедляет проходы.")
        ttk.Checkbutton(sc, text="Скрытый браузер (headless; проверки WB/Ozon вручную не пройти)",
                        variable=self._bvar("wb_browser_headless", False)).grid(
                            row=16, column=1, sticky="w", pady=3, columnspan=2)
        tk.Label(sc, text="ℹ Видимый браузер надёжнее: можно пройти проверку площадки и выбрать город. WB и Ozon работают в одном отдельном профиле Price Hunter, но каждый в своей вкладке: скан одной площадки не уводит вкладку другой. Вход в аккаунты не нужен; куки никуда не отправляются; окно и обе вкладки не закрывай во время охоты",
                 bg=PANEL, fg=MUT, wraplength=980, justify="left").grid(row=17, column=0, columnspan=3, sticky="w", pady=3)
        self._field(
            sc, 18, "Повторная проверка LIVE, сек", self._svar("live_probe_interval_sec"),
            help_text="Как часто заново пробовать недоступную площадку в auto, пока вторая продолжает работу. Меньше — быстрее восстановление, но больше лишних запросов к упавшему источнику.")

        self.var_mp_diag = tk.StringVar(value="")
        self.var_wb_diag = self.var_mp_diag
        self.var_ozon_diag = self.var_mp_diag
        ttk.Button(sc, text="Сохранить и проверить WB", style="Accent.TButton",
                   command=lambda: self._diagnose_wb(save_first=True)).grid(row=19, column=1, sticky="w", pady=6)
        ttk.Button(sc, text="Открыть браузер WB",
                   command=lambda: self._open_wb_browser(save_first=True)).grid(row=19, column=2, sticky="w", pady=6, padx=8)
        ttk.Button(sc, text="Сохранить и проверить Ozon", style="Accent.TButton",
                   command=lambda: self._diagnose_ozon(save_first=True)).grid(row=20, column=1, sticky="w", pady=6)
        ttk.Button(sc, text="Открыть браузер Ozon",
                   command=lambda: self._open_ozon_browser(save_first=True)).grid(row=20, column=2, sticky="w", pady=6, padx=8)
        tk.Label(sc, textvariable=self.var_mp_diag, bg=PANEL, fg=TEXT,
                 wraplength=980, justify="left").grid(row=21, column=0, columnspan=3, sticky="w", pady=4)

        # Detector
        dt = self._section(f, "Детектор аномалий")
        self._field(
            dt, 0, "Мин. скидка карточки (только смешанный режим), %",
            self._svar("min_discount_pct"),
            help_text="Работает только если history-only выключен. В строгом режиме нарисованная продавцом скидка вообще не создаёт алерт и не даёт очков.")
        self._field(
            dt, 1, "Мин. падение vs истории, %", self._svar("min_drop_vs_history_pct"),
            help_text="Главный порог history-only: текущая цена должна быть минимум на столько процентов ниже медианы истории этого же товара. 35% — строго; 25–30% — больше находок и шума.")
        self._field(
            dt, 2, "Мин. обычная цена по истории, ₽", self._svar("min_original_price_rub"),
            help_text="Отсекает дешёвую мелочь: медиана обычной цены должна быть не ниже суммы. В history-only это цена по истории, а не зачёркнутая цена карточки.")
        self._field(
            dt, 3, "Мин. выгода vs истории, ₽", self._svar("min_saving_rub"),
            help_text="Минимальная разница в рублях между медианой истории и текущей ценой. Подними, если много формально дешёвых, но невыгодных находок.")
        self._field(
            dt, 4, "Мин. скор (0–100)", self._svar("min_score"),
            help_text="Финальный фильтр уверенности: падение от истории, выгода, исторический минимум и отзывы дают очки. WATCH от 35, HOT от 50, FIRE от 70.")
        self._field(
            dt, 5, "Кулдаун повторных алертов, ч", self._svar("alert_cooldown_hours"),
            help_text="Сколько часов не повторять алерт по тому же товару примерно по той же цене. Новый алерт возможен раньше, если цена упала ещё примерно на 10%+.")
        ttk.Checkbutton(dt, text="Алерты только по падению vs истории цены (скидка по карточке сама не является основанием)",
                        variable=self._bvar("history_only", True)).grid(row=6, column=0, columnspan=3, sticky="w", pady=4)
        self._setting_help(
            dt, 7, "Рекомендуется держать включённым: без надёжной истории алерта не будет. Если выключить, вернётся шумный смешанный режим, где большая скидка карточки сама может создать находку.",
            column=0, columnspan=3, wraplength=980)
        ttk.Checkbutton(dt, text="Использовать внешнюю историю WB, пока собственной истории базы мало",
                        variable=self._bvar("verify_with_price_history", True)).grid(row=8, column=0, columnspan=3, sticky="w", pady=4)
        self._setting_help(
            dt, 9, "Для новых WB-товаров берёт медиану и минимум из внешнего WB price-history, пока Price Hunter не накопил минимум три своих наблюдения. У Ozon внешней истории нет — только собственная база.",
            column=0, columnspan=3, wraplength=980)

        # Queries
        qs = self._section(f, "Поисковые запросы (каждый с новой строки)")
        self.txt_queries = tk.Text(qs, height=7, bg="#0b0b1c", fg=TEXT,
                                   insertbackground=TEXT, borderwidth=0,
                                   font=("Consolas", 10))
        self.txt_queries.pack(fill="x")
        tk.Label(qs, bg=PANEL, fg=MUT, justify="left", wraplength=980,
                 text=("ℹ Каждый запрос выполняется в каждом проходе на включённых площадках, "
                       "поэтому список напрямую влияет на длительность сканирования и риск антибота. "
                       "Ставь ликвид: то, что нужно самому или легко перепродать. Чем точнее запрос "
                       "(бренд + модель), тем меньше случайных карточек; общие категории дают шире охват, но больше шума.")
                 ).pack(anchor="w", pady=3)

        # App
        ap = self._section(f, "Приложение")
        ttk.Checkbutton(ap, text="Закрытие окна сворачивает в трей (охота продолжается)",
                        variable=self._bvar("close_to_tray", True)).pack(anchor="w")
        tk.Label(ap, bg=PANEL, fg=MUT, wraplength=980, justify="left",
                 text="ℹ Если включено, крестик не завершает программу: сканирование и Telegram продолжаются из трея. Полный выход — через меню иконки в трее.").pack(anchor="w", padx=22)
        ttk.Checkbutton(ap, text="Запускаться свёрнутым в трей",
                        variable=self._bvar("start_minimized")).pack(anchor="w")
        tk.Label(ap, bg=PANEL, fg=MUT, wraplength=980, justify="left",
                 text="ℹ При запуске главное окно не покажется, но охота начнётся в фоне. Удобно вместе с автозапуском.").pack(anchor="w", padx=22)
        self.var_autostart = self._bvar("autostart", autostart.is_enabled())
        cb = ttk.Checkbutton(ap, text="Автозапуск с Windows",
                             variable=self.var_autostart)
        cb.pack(anchor="w")
        tk.Label(ap, bg=PANEL, fg=MUT, wraplength=980, justify="left",
                 text="ℹ Price Hunter будет стартовать после входа в Windows. Настройка применяется кнопкой «Сохранить настройки».").pack(anchor="w", padx=22)
        if not autostart.is_supported():
            cb.configure(state="disabled")
            tk.Label(ap, text="(автозапуск доступен в Windows-сборке)", bg=PANEL, fg=MUT).pack(anchor="w")

        row = tk.Frame(f, bg=BG)
        row.pack(fill="x", pady=12)
        ttk.Button(row, text="Сохранить настройки", style="Accent.TButton",
                   command=self._save_settings).pack(side="left", padx=4)
        ttk.Button(row, text="Сбросить форму", command=self._load_settings_form).pack(side="left", padx=4)
        self.var_saved = tk.StringVar(value="")
        tk.Label(row, textvariable=self.var_saved, bg=BG, fg=GREEN,
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=10)
        tk.Label(
            f, bg=BG, fg=MUT, wraplength=1050, justify="left", anchor="w",
            text=("ℹ Изменения настроек применяются только после «Сохранить настройки». "
                  "«Сбросить форму» возвращает в поля уже сохранённые значения, но не меняет файл настроек. "
                  "После смены режима, площадок или транспортов следующий проход заново проверит источники."),
        ).pack(fill="x", pady=(0, 12))

    # ------------------------------ Журнал ---------------------------- #
    def _build_journal(self):
        f = self._tab("Журнал")
        cols = ("time", "mode", "queries", "scanned", "new", "alerts", "error")
        self.tree_runs = ttk.Treeview(f, columns=cols, show="headings", height=22)
        for col, title, w in [("time", "Время", 110), ("mode", "Режим / источники", 150),
                              ("queries", "Запросов", 90), ("scanned", "Проверено", 110),
                              ("new", "Новых", 80), ("alerts", "Находок", 90),
                              ("error", "Ошибка", 420)]:
            self.tree_runs.heading(col, text=title)
            self.tree_runs.column(col, width=w, anchor="center" if col != "error" else "w")
        self.tree_runs.pack(fill="both", expand=True)

    # ------------------------------ handlers -------------------------- #
    def _info_dialog(self, title: str, text: str, warn: bool = False):
        """Show at most one modal info dialog at a time.

        Windows message boxes are modal: while one is open the main
        window's buttons (Сканировать сейчас / Пауза) do not react,
        and stacked «Проверка WB» + «Проверка Ozon» dialogs made the
        app look frozen.  Extra completions still update their label
        text (done by the caller), they just skip the popup.
        """
        if self._dialog_busy:
            return
        self._dialog_busy = True
        try:
            if warn:
                messagebox.showwarning(title, text)
            else:
                messagebox.showinfo(title, text)
        finally:
            self._dialog_busy = False

    def _toggle(self):
        self.ctrl.toggle_scanner()
        self._poll(once=True)

    def _load_settings_form(self):
        cfg = self.ctrl.settings.get()
        pub = self.ctrl.settings.public()
        for key in ("mode", "interval_sec", "pages_per_query", "min_discount_pct",
                    "min_drop_vs_history_pct", "min_original_price_rub",
                    "min_saving_rub", "min_score", "alert_cooldown_hours",
                    "dest", "spp", "wb_transport", "ozon_transport",
                    "wb_browser_path", "wb_browser_port", "wb_browser_wait_sec",
                    "wb_request_delay_sec", "ozon_request_delay_sec",
                    "live_probe_interval_sec", "telegram_chat_id"):
            self.v[key].set(str(cfg.get(key, "")))
        self.v["telegram_token"].set("")   # never echo the secret back
        self.var_token_hint.set(
            f"сохранён: {pub['telegram_token_masked']}" if pub.get("telegram_token_masked")
            else "ещё не задан")
        for key in ("telegram_enabled", "history_only",
                    "verify_with_price_history",
                    "wb_enabled", "ozon_enabled",
                    "wb_browser_fallback", "ozon_browser_fallback",
                    "wb_browser_headless", "close_to_tray", "start_minimized"):
            self.v[key].set(bool(cfg.get(key)))
        sorts = set(cfg.get("sorts") or [])
        self.v["sort_popular"].set("popular" in sorts)
        self.v["sort_benefit"].set("benefit" in sorts)
        self.v["sort_priceup"].set("priceup" in sorts)
        self.txt_queries.delete("1.0", "end")
        self.txt_queries.insert("1.0", "\n".join(cfg.get("queries") or []))

    def _collect_form(self) -> dict:
        sorts = [s for key, s in [("sort_popular", "popular"), ("sort_benefit", "benefit"),
                                  ("sort_priceup", "priceup")] if self.v[key].get()]
        raw = {k: var.get() for k, var in self.v.items()
               if not k.startswith("sort_") and k != "autostart"}
        raw["sorts"] = sorts
        raw["queries"] = self.txt_queries.get("1.0", "end")
        return raw

    def _save_settings(self) -> bool:
        try:
            pub = self.ctrl.save_settings(self._collect_form())
        except ValueError as e:
            messagebox.showerror("Настройки не сохранены", str(e))
            return False
        # Controller.save_settings already reset the live/demo verdict.
        ok_auto = autostart.set_enabled(bool(self.var_autostart.get())) \
            if autostart.is_supported() else None
        self.v["telegram_token"].set("")
        self.var_token_hint.set(
            f"сохранён: {pub['telegram_token_masked']}" if pub.get("telegram_token_masked")
            else "ещё не задан")
        msg = "Сохранено"
        if ok_auto is False:
            msg += " (автозапуск не применился)"
        self.var_saved.set(msg)
        self.root.after(4000, lambda: self.var_saved.set(""))
        self._poll(once=True)
        return True

    def _test_telegram(self):
        self.var_tg_test.set("отправляю…")
        tok, chat = self.v["telegram_token"].get(), self.v["telegram_chat_id"].get()

        def work():
            _ok, msg = self.ctrl.telegram_test(tok, chat)
            self.root.after(0, lambda: self.var_tg_test.set(msg))

        threading.Thread(target=work, daemon=True).start()

    def _open_marketplace_browser(self, marketplace: str,
                                  save_first: bool = False):
        if save_first and not self._save_settings():
            return
        marketplace = marketplace if marketplace in MARKETPLACE_LABELS else "wb"
        label = _marketplace_label(marketplace)
        site = "Wildberries" if marketplace == "wb" else "Ozon"
        diag_var = getattr(self, "var_mp_diag", getattr(self, "var_wb_diag", None))
        if diag_var is not None:
            diag_var.set(f"Открываю отдельный Chrome/Edge для {site}…")

        def work():
            try:
                info = self.ctrl.open_marketplace_browser(marketplace)
                text = (f"Вкладка {label} открыта и активна. Дождись загрузки {site} и, если "
                        "появится проверка, пройди её именно в этой вкладке. Для Ozon "
                        "при необходимости выбери город доставки. Вход в аккаунт "
                        f"не нужен. Затем нажми «Проверить {site}». У каждой площадки "
                        f"в этом окне своя отдельная вкладка — не закрывай их во время охоты. "
                        f"URL: {info.get('url') or ''}")
                self.root.after(0, lambda: (
                    diag_var.set(text) if diag_var is not None else None,
                    self._info_dialog(f"Браузер {label}", text)))
            except Exception as e:
                text = f"Не удалось открыть браузер {label}: {e}"
                self.root.after(0, lambda: (
                    diag_var.set(text) if diag_var is not None else None,
                    self._info_dialog(f"Браузер {label}", text, warn=True)))

        threading.Thread(target=work, daemon=True).start()

    def _open_wb_browser(self, save_first: bool = False):
        self._open_marketplace_browser("wb", save_first=save_first)

    def _open_ozon_browser(self, save_first: bool = False):
        self._open_marketplace_browser("ozon", save_first=save_first)

    def _diagnose_marketplace(self, marketplace: str,
                              save_first: bool = False):
        if save_first and not self._save_settings():
            return
        marketplace = marketplace if marketplace in MARKETPLACE_LABELS else "wb"
        label = _marketplace_label(marketplace)
        diag_var = getattr(self, "var_mp_diag", getattr(self, "var_wb_diag", None))
        if diag_var is not None:
            diag_var.set(
                f"Проверяю {label}: пробую API/endpoint'ы, httpx, curl_cffi и "
                "при необходимости настоящий Chrome/Edge и отрисованный DOM…")
        self.var_wb_warn.set(f"Проверяю соединение с {label}…")

        def work():
            res = self.ctrl.diagnose_marketplace(marketplace)
            self.root.after(0, lambda: self._apply_diagnosis(res))

        threading.Thread(target=work, daemon=True).start()

    def _diagnose_wb(self, save_first: bool = False):
        self._diagnose_marketplace("wb", save_first=save_first)

    def _diagnose_ozon(self, save_first: bool = False):
        self._diagnose_marketplace("ozon", save_first=save_first)

    def _apply_diagnosis(self, res: dict):
        marketplace = _marketplace_of(res)
        label = _marketplace_label(marketplace)
        lines = [str(res.get("message") or "")]
        if res.get("sample"):
            lines.append(f"Пример товара: {res['sample']}")
        if marketplace == "wb" and res.get("ok") and res.get("card_ok") is False:
            lines.append("Поиск работает, но проверка карточки WB не прошла. "
                         "Для поиска это некритично.")
        if res.get("enabled") is False:
            lines.append(
                f"ВАЖНО: {label} сейчас выключен в настройках. Проверка "
                "выполнена отдельно, но в сканирование источник не войдёт, "
                "пока не включишь его и не сохранишь настройки.")
        if res.get("requested_mode") == "demo":
            lines.append("ВАЖНО: сейчас выбран режим DEMO — настоящие товары "
                         "не ищутся. Переключи режим на live или auto и сохрани.")
        elif res.get("ok"):
            lines.append(
                f"Источник {label} работает. Если находок пока нет — это "
                "нормально: товары уже пишутся в базу, алерт будет только на "
                "аномальное падение цены. Для Ozon собственная история базы "
                "накапливается с первых сканирований.")
        if res.get("hint"):
            lines.append(str(res["hint"]))
        attempts = res.get("attempts") or []
        if attempts:
            tail = []
            for a in attempts[-4:]:
                endpoint = str(a.get("endpoint") or "")
                host = endpoint.split("/")[2] if "://" in endpoint else endpoint
                tail.append(f"{host}/{a.get('transport', '?')}: "
                            f"{a.get('outcome', '?')}"
                            + (f" HTTP {a['status']}" if a.get("status") else ""))
            lines.append("Попытки: " + "; ".join(tail))
        text = "\n".join(line for line in lines if line)
        diag_var = getattr(self, "var_mp_diag", getattr(self, "var_wb_diag", None))
        if diag_var is not None:
            diag_var.set(text)
        if res.get("ok"):
            self._info_dialog(f"Проверка {label}", text)
        else:
            self._info_dialog(f"Проверка {label}", text, warn=True)
        self._poll(once=True)

    def _selected_alert(self) -> dict | None:
        sel = self.tree.selection()
        return self._alert_by_iid.get(sel[0]) if sel else None

    def _on_select_alert(self, _e=None):
        a = self._selected_alert()
        if not a:
            return
        reasons = " · ".join(a.get("reasons") or [])
        extra = ""
        if a.get("drop_vs_median_pct") and a.get("hist_median"):
            extra = (f" | на {a['drop_vs_median_pct']:.0f}% ниже медианы истории "
                     f"({fmt_rub(a['hist_median'])})")
        if a.get("hist_min"):
            vs_min = a.get("vs_hist_min_pct")
            if vs_min is not None and float(vs_min) > 0:
                extra += (f" | новый минимум площадки: ниже прежнего "
                          f"{fmt_rub(float(a['hist_min']))} на {float(vs_min):.0f}%")
            else:
                extra += f" | минимум площадки {fmt_rub(float(a['hist_min']))}"
        if not _alert_history_confirmed(a):
            extra += (" | ⚠ не подтверждено историей: это старая/смешанная "
                      "находка по скидке карточки, а не по падению от обычной цены")
        marketplace = _marketplace_of(a)
        self.var_details.set(
            f"{_marketplace_label(marketplace)} | "
            f"{MARKETPLACE_ID_LABELS[marketplace]} {a['nm']} | "
            f"скор {a['score']}/100{extra} | {reasons} | "
            f"{'DEMO' if a.get('source') == 'demo' else 'LIVE'}"
            f"{' | отправлено в Telegram' if a.get('telegram_sent') else ''}")

    def _open_selected(self):
        a = self._selected_alert()
        if a:
            webbrowser.open(a["url"])

    def _open_recent(self, _e=None):
        sel = self.lb_recent.curselection()
        if sel and sel[0] < len(self._recent_urls):
            webbrowser.open(self._recent_urls[sel[0]])

    def _start_lowest_search(self):
        if self._lowest_searching:
            return
        query = self.var_lowest_query.get().strip()
        marketplaces = []
        if self.var_lowest_wb.get():
            marketplaces.append("wb")
        if self.var_lowest_ozon.get():
            marketplaces.append("ozon")
        if len(query) < 2:
            self._info_dialog(
                "Самая низкая цена",
                "Введи бренд и модель товара, например: iPhone 15 128 ГБ.",
                warn=True)
            return
        if not marketplaces:
            self._info_dialog(
                "Самая низкая цена", "Выбери хотя бы WB или Ozon.", warn=True)
            return
        try:
            pages = max(1, min(3, int(self.var_lowest_pages.get() or 2)))
        except Exception:
            pages = 2
        strict = bool(self.var_lowest_strict.get())
        record = bool(self.var_lowest_record.get())
        self._lowest_searching = True
        self.btn_lowest_search.configure(state="disabled")
        labels = " + ".join(_marketplace_label(m) for m in marketplaces)
        self.var_lowest_summary.set("")
        self.var_lowest_status.set(
            f"Ищу «{query}» на {labels}: сначала самые дешёвые, затем популярные; "
            f"страниц: {pages}. Используются сохранённые транспорты, регион и паузы…")
        self.var_lowest_details.set("Поиск может занять от нескольких секунд до минуты, особенно через настоящий Chrome/Edge.")

        def work():
            try:
                result = self.ctrl.find_lowest_prices(
                    query, marketplaces=marketplaces, pages=pages,
                    strict_match=strict, record=record)
                self.root.after(0, lambda result=result: self._apply_lowest_result(result))
            except Exception as e:
                self.root.after(0, lambda e=e: self._apply_lowest_error(str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_lowest_error(self, text: str):
        self._lowest_searching = False
        self.btn_lowest_search.configure(state="normal")
        self.var_lowest_summary.set("Поиск не завершился")
        self.var_lowest_status.set(text)
        self._info_dialog("Самая низкая цена", text, warn=True)

    def _apply_lowest_result(self, result: dict):
        self._lowest_searching = False
        self.btn_lowest_search.configure(state="normal")
        offers = list(result.get("offers") or [])
        cheapest = result.get("cheapest") or (offers[0] if offers else None)
        self._lowest_best = cheapest
        self.tree_lowest.delete(*self.tree_lowest.get_children())
        self._lowest_by_iid = {}
        seen_market_best: set[str] = set()
        for offer in offers:
            marketplace = _marketplace_of(offer)
            iid = f"lowest-{marketplace}-{offer.get('nm')}"
            self._lowest_by_iid[iid] = offer
            basic = float(offer.get("price_rub") or offer.get("sale_price_rub") or 0)
            sale = float(offer.get("sale_price_rub") or 0)
            discount = float(offer.get("discount_pct") or 0)
            old_text = (f"{fmt_rub(basic)} (−{discount:.0f}%)"
                        if basic > sale and discount > 0 else fmt_rub(basic))
            rating = offer.get("rating") or 0
            feedbacks = offer.get("feedbacks") or 0
            rating_text = (f"{float(rating):.1f} · {feedbacks}" if rating
                           else (str(feedbacks) if feedbacks else "—"))
            match_text = (f"{offer.get('match_label') or '—'} · "
                          f"{float(offer.get('match_score') or 0) * 100:.0f}%")
            tags: tuple[str, ...] = ()
            if offer is cheapest or offer.get("is_cheapest"):
                tags = ("cheapest",)
            elif marketplace not in seen_market_best:
                tags = ("market_best",)
            seen_market_best.add(marketplace)
            self.tree_lowest.insert(
                "", "end", iid=iid, tags=tags, values=(
                    offer.get("rank") or "",
                    _marketplace_label(marketplace),
                    f"{offer.get('brand', '')} {offer.get('name', '')}".strip()[:76],
                    fmt_rub(sale), old_text, rating_text, match_text,
                    (fmt_rub(float(offer["own_history_min"]))
                     if offer.get("own_history_min") else "—"),
                    offer.get("nm") or "",
                ))

        by_market = result.get("by_marketplace") or {}
        market_summaries = []
        for marketplace in ("wb", "ozon"):
            info = by_market.get(marketplace) or {}
            best = info.get("cheapest")
            if best:
                market_summaries.append(
                    f"{_marketplace_label(marketplace)}: от "
                    f"{fmt_rub(float(best.get('sale_price_rub') or 0))}")
            elif marketplace in (result.get("marketplaces") or []):
                market_summaries.append(f"{_marketplace_label(marketplace)}: нет подходящих")
        if cheapest:
            self.var_lowest_summary.set(
                f"🏆 Общий минимум сейчас: {fmt_rub(float(cheapest.get('sale_price_rub') or 0))} "
                f"— {_marketplace_label(cheapest)} · "
                f"{cheapest.get('brand', '')} {cheapest.get('name', '')}".strip()
                + (f" | {' · '.join(market_summaries)}" if market_summaries else ""))
        else:
            self.var_lowest_summary.set("Подходящих предложений не найдено")

        mode = result.get("mode") or result.get("effective_mode")
        status_parts = [
            str(result.get("message") or ""),
            f"Режим: {mode}; получено уникальных карточек: {result.get('total_raw', 0)}; "
            f"после фильтра: {result.get('total_matched', len(offers))}; "
            f"отсечено явных аксессуаров: {result.get('excluded_accessories', 0)}.",
        ]
        if mode == "demo":
            status_parts.append("⚠ Это DEMO-результаты, не настоящие товары.")
        if result.get("recorded"):
            status_parts.append(
                f"В собственную историю записано предложений: {result['recorded']}.")
        if result.get("errors"):
            status_parts.append("Ошибки источников: " + " | ".join(
                str(e) for e in result["errors"][:3]))
        self.var_lowest_status.set(" ".join(part for part in status_parts if part))

        if offers:
            first_iid = next(iter(self._lowest_by_iid), None)
            if first_iid:
                self.tree_lowest.selection_set(first_iid)
                self.tree_lowest.focus(first_iid)
                self._on_select_lowest()
        else:
            self.var_lowest_details.set(
                "Попробуй добавить бренд, точную модель и вариант; либо сними "
                "«Только точные совпадения». Если источник недоступен — проверь WB/Ozon на главной.")

    def _selected_lowest(self) -> dict | None:
        sel = self.tree_lowest.selection()
        return self._lowest_by_iid.get(sel[0]) if sel else None

    def _on_select_lowest(self, _e=None):
        offer = self._selected_lowest()
        if not offer:
            return
        marketplace = _marketplace_of(offer)
        sale = float(offer.get("sale_price_rub") or 0)
        basic = float(offer.get("price_rub") or sale)
        above = offer.get("above_cheapest_pct")
        history_bits = []
        if offer.get("own_history_median"):
            history_bits.append(
                f"медиана своей базы {fmt_rub(float(offer['own_history_median']))}")
        if offer.get("own_history_min"):
            history_bits.append(
                f"минимум своей базы {fmt_rub(float(offer['own_history_min']))}")
        if offer.get("own_history_count"):
            history_bits.append(f"наблюдений: {offer['own_history_count']}")
        self.var_lowest_details.set(
            f"{_marketplace_label(marketplace)} | "
            f"{MARKETPLACE_ID_LABELS[marketplace]} {offer.get('nm')} | "
            f"текущая цена {fmt_rub(sale)} | цена карточки {fmt_rub(basic)} "
            f"(скидка продавца −{float(offer.get('discount_pct') or 0):.0f}%, только справка) | "
            f"совпадение: {offer.get('match_label', '—')} "
            f"{float(offer.get('match_score') or 0) * 100:.0f}% | "
            + (f"дороже общего минимума на {float(above):.0f}% | " if above else "это общий минимум | ")
            + (("история: " + ", ".join(history_bits) + " | ") if history_bits else "своей истории пока нет | ")
            + "перед покупкой проверь вариант, продавца, доставку и цену в корзине.")

    def _open_lowest_selected(self):
        offer = self._selected_lowest()
        if offer and offer.get("url"):
            webbrowser.open(offer["url"])

    def _open_lowest_best(self):
        if self._lowest_best and self._lowest_best.get("url"):
            webbrowser.open(self._lowest_best["url"])

    def _dismiss_selected(self):
        a = self._selected_alert()
        if a:
            self.ctrl.dismiss(a["id"])
            self._top_alert_id = -1
            self._refresh_alerts(force=True)

    def _show_history(self):
        a = self._selected_alert()
        if not a:
            return
        marketplace = _marketplace_of(a)
        hist = self.ctrl.product_history(a["nm"], marketplace=marketplace)
        win = tk.Toplevel(self.root)
        win.title(
            f"История цены — {_marketplace_label(marketplace)} · "
            f"{MARKETPLACE_ID_LABELS[marketplace]} {a['nm']}")
        win.geometry("660x340")
        win.configure(bg=BG)
        tk.Label(win, text=f"{a.get('brand','')} {a.get('name','')}"[:70], bg=BG,
                 fg=TEXT, font=("Segoe UI", 11, "bold"), wraplength=620).pack(pady=8)
        if len(hist) < 2:
            tk.Label(win, bg=BG, fg=MUT,
                     text="Истории пока мало — нужно несколько сканирований с разной ценой."
                     ).pack(pady=20)
            return
        c = tk.Canvas(win, width=620, height=230, bg="#0b0b1c", highlightthickness=0)
        c.pack(padx=20, pady=6)
        prices = [h["sale_price"] for h in hist]
        mn, mx = min(prices), max(prices)
        rg = (mx - mn) or 1.0
        pts = []
        for i, h in enumerate(hist):
            x = 15 + i * (590 / (len(hist) - 1))
            y = 200 - ((h["sale_price"] - mn) / rg) * 170
            pts += [x, y]
        c.create_line(*pts, fill=NEON, width=2)
        for x, y in zip(pts[::2], pts[1::2]):
            c.create_oval(x - 3, y - 3, x + 3, y + 3, fill=PINK, outline="")
        tk.Label(win, text=f"min {fmt_rub(mn)} · max {fmt_rub(mx)} · точек: {len(hist)}",
                 bg=BG, fg=MUT).pack()

    # ------------------------------ polling --------------------------- #
    def _refresh_alerts(self, force: bool = False):
        history_only = _history_only_enabled(self.ctrl.settings.get())
        show_unconfirmed = bool(self.var_show_unconfirmed_alerts.get())
        only_confirmed = history_only and not show_unconfirmed
        filter_key = (history_only, show_unconfirmed)
        if filter_key != self._alert_filter_key:
            force = True
            self._alert_filter_key = filter_key
        alerts = self.ctrl.alerts(
            200, only_history_confirmed=only_confirmed)
        top = alerts[0]["id"] if alerts else 0
        if not force and top == self._top_alert_id:
            return
        self._top_alert_id = top
        self.var_recent_title.set(
            "Последние находки, подтверждённые историей "
            "(двойной клик — открыть товар):"
            if only_confirmed else
            "Последние находки (двойной клик — открыть товар):")
        self.tree.delete(*self.tree.get_children())
        self._alert_by_iid = {}
        for a in alerts:
            iid = str(a["id"])
            self._alert_by_iid[iid] = a
            tags = (a["level"],) if _alert_history_confirmed(a) \
                else (a["level"], "unconfirmed")
            self.tree.insert("", "end", iid=iid, tags=tags, values=(
                _fmt_ts(a["ts"]), LEVEL_ICON.get(a["level"], a["level"]),
                _marketplace_label(a),
                f"{a.get('brand','')} {a.get('name','')}".strip()[:64],
                fmt_rub(a["sale_price"]), fmt_rub(_alert_reference(a)),
                (fmt_rub(float(a["hist_min"])) if a.get("hist_min") else "—"),
                f"−{_alert_drop(a):.0f}%", fmt_rub(a["saving"]), a["score"]))
        self.lb_recent.delete(0, "end")
        self._recent_urls = []
        for a in alerts[:12]:
            self._recent_urls.append(a["url"])
            self.lb_recent.insert("end",
                f"{_marketplace_label(a)} · {LEVEL_ICON.get(a['level'],'')}  "
                f"{a.get('brand','')} {a.get('name','')[:42]} — "
                f"{fmt_rub(a['sale_price'])} "
                f"({_alert_reference_text(a)}, "
                f"−{_alert_drop(a):.0f}%)")
        if not alerts and only_confirmed:
            self.var_details.set(
                "Подтверждённых историей находок пока нет. Старые находки "
                "только по скидке карточки скрыты — при необходимости "
                "покажи их галочкой в верхней части вкладки «Находки».")

    def _refresh_runs(self):
        runs = self.ctrl.runs(50)
        top = runs[0]["id"] if runs else 0
        if top == self._top_run_id and self.tree_runs.get_children():
            # still refresh when a running row finishes (same id, new numbers)
            if runs and runs[0].get("finished"):
                return
        self._top_run_id = top
        self.tree_runs.delete(*self.tree_runs.get_children())
        for r in runs:
            self.tree_runs.insert("", "end", values=(
                _fmt_ts(r["ts"]), _run_source_label(r["source"]),
                r["queries"], r["scanned"],
                r["new_products"], r["alerts"], (r["error"] or "—")[:70]))

    def _poll(self, once: bool = False):
        for cmd in self.tray.drain_commands():
            if cmd == "show":
                self.root.deiconify(); self.root.lift(); self.root.focus_force()
            elif cmd == "scan":
                self.ctrl.scan_now()
            elif cmd == "toggle":
                self.ctrl.toggle_scanner()
            elif cmd == "quit":
                return self.quit_app()

        st = self.ctrl.status()
        mode = st.get("effective_mode") or "…"
        requested = st.get("requested_mode") or st.get("mode")
        markets = st.get("marketplaces") or {}
        enabled_markets = [m for m in ("wb", "ozon")
                           if (markets.get(m) or {}).get("enabled", True)]
        available_markets = [m for m in enabled_markets
                             if (markets.get(m) or {}).get("available") is True]
        unavailable_markets = [m for m in enabled_markets
                               if (markets.get(m) or {}).get("available") is False]
        enabled_txt = " + ".join(_marketplace_label(m) for m in enabled_markets)
        if mode == "live":
            badge = "● LIVE " + (
                " + ".join(_marketplace_label(m) for m in available_markets)
                if available_markets else enabled_txt)
            if unavailable_markets and available_markets:
                badge += " · нет: " + ", ".join(
                    _marketplace_label(m) for m in unavailable_markets)
        elif requested == "demo":
            badge = "● DEMO (вручную)"
        elif mode == "demo":
            badge = "● DEMO • " + (enabled_txt or "WB + Ozon") + " недоступны"
        else:
            badge = "● ПРОВЕРКА МАРКЕТПЛЕЙСОВ…"
        self.badge_mode.configure(
            text=badge, fg=GREEN if mode == "live" else AMBER)
        if st.get("stopping"):
            run_txt = "останавливаю после текущего запроса…"
        elif st["scanning"]:
            prog = st.get("progress") or {}
            if prog:
                run_txt = (
                    f"сканирую… {_marketplace_label(prog.get('marketplace', 'wb'))} "
                    f"{prog.get('query_idx', '?')}/{prog.get('query_total', '?')} "
                    f"«{prog.get('query', '')}» · найдено {prog.get('found', 0)}")
            else:
                run_txt = "сканирую…"
        elif st["running"] and st.get("next_scan_ts"):
            run_txt = f"след. проход через {max(0, st['next_scan_ts'] - int(time.time()))} с"
        elif st["running"]:
            run_txt = "работает"
        else:
            run_txt = "на паузе"
        self.var_runstate.set(run_txt)
        self.btn_toggle.configure(text="Пауза" if st["running"] else "▶ Запуск")

        s = st["stats"]
        pub = st["settings"]
        history_only = _history_only_enabled(pub)
        self.kpi_vars["products"].set(str(s["products_seen"]))
        if history_only:
            self.kpi_label_vars["alerts"].set("ПОДТВЕРЖДЕНО ИСТОРИЕЙ 24Ч")
            self.kpi_label_vars["saving"].set("ВЫГОДА VS ИСТОРИИ 24Ч")
            alerts_kpi = s.get("alerts_today_history_confirmed", 0) or 0
            saving_kpi = s.get("potential_saving_history_today", 0) or 0
        else:
            self.kpi_label_vars["alerts"].set("НАХОДОК ЗА 24Ч")
            self.kpi_label_vars["saving"].set("ВЫГОДА ЗА 24Ч")
            alerts_kpi = s.get("alerts_today", 0) or 0
            saving_kpi = s.get("potential_saving_today", 0) or 0
        self.kpi_vars["alerts"].set(str(alerts_kpi))
        best = s.get("best_history_drop_today", 0) or 0
        self.kpi_vars["best"].set(f"−{best:.0f}%")
        self.kpi_vars["saving"].set(fmt_rub(saving_kpi))
        self.kpi_vars["scans"].set(str(s["scans_total"]))
        self.kpi_vars["snaps"].set(str(s["snapshots"]))

        tg_ok = pub.get("telegram_configured") and pub.get("telegram_enabled")
        chrome = st.get("chrome_transport") or {}
        chrome_txt = ("найден" if chrome.get("available") else
                      "не найден")
        last_by_source = " · ".join(
            f"{_marketplace_label(m)}: "
            + (f"{(markets.get(m) or {}).get('last_products', 0)} тов. "
               f"({_fmt_ts((markets.get(m) or {}).get('last_scan_ts'))})"
               if (markets.get(m) or {}).get("last_scan_ts")
               else "ещё не сканировался")
            for m in enabled_markets)
        detector_txt = (
            "детектор: только история цены"
            + (f" · скрыто без истории: {s.get('alerts_unconfirmed_total', 0)}"
               if s.get("alerts_unconfirmed_total", 0) else "")
            if history_only else
            "детектор: СМЕШАННЫЙ — скидка карточки тоже создаёт алерты")
        self.var_status.set(
            f"Режим: запрошен {requested}, фактически {mode} · "
            f"{detector_txt} · "
            f"источники: {enabled_txt or '—'} · "
            f"товаров WB/Ozon: {s.get('products_wb', 0)}/{s.get('products_ozon', 0)} · "
            f"последний проход — {last_by_source} · "
            f"запросов: {len(pub.get('queries') or [])} · "
            f"интервал: {pub.get('interval_sec')} с · "
            f"транспорт WB: {st.get('wb_transport', 'auto')} · "
            f"транспорт Ozon: {st.get('ozon_transport', 'auto')} · "
            f"Chrome/Edge: {chrome_txt} · "
            f"последнее сканирование: {_fmt_ts(st.get('last_scan_ts'))} · "
            f"Telegram: {'настроен' if tg_ok else 'НЕ настроен'}"
            + (f" · ошибка: {st['last_error'][:90]}" if st.get("last_error") else ""))
        self.var_tg_warn.set(
            "" if tg_ok else "⚠ Telegram не настроен: без него не узнаешь о находке вовремя. "
                             "Вкладка «Настройки» → токен @BotFather + chat_id.")
        reason = str(st.get("mode_reason") or "")
        if requested == "demo":
            wb_warn = ("⚠ Сейчас включён DEMO вручную: настоящие товары WB/Ozon НЕ ищутся, "
                       "находки сгенерированы. Для охоты: Настройки → Режим → live "
                       "(или auto) → сохрани и проверь включённые маркетплейсы.")
        elif mode == "demo":
            nxt = st.get("next_live_probe_ts")
            retry = (f" Повторная проверка LIVE примерно через "
                     f"{max(0, int(nxt) - int(time.time()))} с."
                     if nxt else "")
            wb_warn = ("⚠ Все включённые живые источники сейчас недоступны, приложение "
                       "временно в DEMO и настоящие товары не ищет. Причина: "
                       f"{reason}{retry} Проверь WB и Ozon кнопками выше.")
        elif mode == "live" and st.get("last_error"):
            wb_warn = (f"⚠ LIVE работает частично: один или несколько запросов/источников "
                       f"дали ошибку: {st['last_error'][:180]} "
                       "Успешные товары другого источника при этом сохраняются.")
        elif mode == "live" and not history_only:
            wb_warn = ("⚠ Включён смешанный детектор: большая скидка по карточке "
                       "сама создаёт находки, поэтому возможны фейковые «−95%». "
                       "Для режима по реальной истории включи галочку "
                       "«Алерты только по падению vs истории цены».")
        elif mode == "live":
            wb_warn = ""
        else:
            wb_warn = "Проверяю доступность включённых маркетплейсов…"
        self.var_wb_warn.set(wb_warn)

        self._refresh_alerts()
        self._refresh_runs()
        for a in self.ctrl.drain_alerts()[-3:]:
            self._notify_alert(a)
        if not once:
            self.root.after(2000, self._poll)

    # ------------------------------ notifications --------------------- #
    def _notify_alert(self, a: dict):
        title = (f"Price Hunter · {_marketplace_label(a)}: "
                 f"{LEVEL_ICON.get(a['level'], a['level'])}")
        msg = (f"{a.get('brand','')} {a.get('name','')[:50]} — {fmt_rub(a['sale_price_rub'])} "
               f"({_alert_reference_text(a)}, −{_alert_drop(a):.0f}%)")
        self.tray.notify(title, msg)
        try:
            self.root.bell()
        except Exception:
            pass
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        outer = tk.Frame(top, bg=PINK if a["level"] == "FIRE" else NEON, padx=2, pady=2)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=PANEL, padx=14, pady=10)
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text=title, bg=PANEL, fg=PINK if a["level"] == "FIRE" else NEON,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")
        tk.Label(inner, text=msg, bg=PANEL, fg=TEXT, wraplength=330,
                 justify="left").pack(anchor="w", pady=3)
        btns = tk.Frame(inner, bg=PANEL)
        btns.pack(anchor="w")
        tk.Button(btns, text="Открыть товар", bg="#17456b", fg="white", borderwidth=0,
                  padx=10, pady=4,
                  command=lambda u=a["url"], w=top: (webbrowser.open(u), w.destroy())
                  ).pack(side="left")
        tk.Button(btns, text="Закрыть", bg=PANEL2, fg=TEXT, borderwidth=0,
                  padx=10, pady=4, command=top.destroy).pack(side="left", padx=6)
        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        x = top.winfo_screenwidth() - w - 18
        y = top.winfo_screenheight() - h - 60 - len(self._toasts) * (h + 10)
        top.geometry(f"+{x}+{max(10, y)}")
        self._toasts.append(top)
        top.after(25_000, lambda w=top: w.destroy() if w.winfo_exists() else None)

    # ------------------------------ window lifecycle ------------------- #
    def _hide_to_tray(self):
        if self._tray_on:
            self.root.withdraw()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.notify("Price Hunter в трее",
                                 "Охота продолжается. Алерты придут сюда и в Telegram.")
        else:
            self.root.iconify()

    def _on_close(self):
        if self._tray_on and self.ctrl.settings.get().get("close_to_tray", True):
            self._hide_to_tray()
        else:
            self.quit_app()

    def quit_app(self):
        try:
            self.tray.stop()
            self.ctrl.shutdown()
        finally:
            self.root.destroy()


def run_app(ctrl: DesktopController, minimized: bool = False,
            icon_path: str | None = None, smoke: bool = False) -> int:
    root = tk.Tk()
    if icon_path and icon_path.lower().endswith(".ico") and sys.platform == "win32":
        try:
            root.iconbitmap(icon_path)
        except Exception:
            pass
    app = PriceHunterApp(root, ctrl, minimized=minimized, icon_path=icon_path)
    if smoke:  # CI smoke test: build the full window, then close cleanly.
        root.after(900, app.quit_app)
    root.mainloop()
    return 0
