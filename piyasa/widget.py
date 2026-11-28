"""The compact desktop widget and application controller."""

import ctypes
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from queue import Empty, SimpleQueue
from tkinter import filedialog, messagebox, ttk

import requests
import yfinance as yf
from PIL import Image, ImageDraw, ImageTk

from piyasa.market.cache import market_data_for_save, normalize_market_data
from piyasa.market.history import MarketHistoryDB
from piyasa.market.instruments import (
    SILVER_HISTORY_PROXY_SYMBOL,
    SILVER_SPOT_API_URL,
    SILVER_SPOT_KEYS,
    SILVER_SPOT_SOURCES,
    SILVER_SPOT_SYMBOL,
    TROY_OUNCE_GRAMS,
    calculate_reordered_keys,
    default_watchlist,
    normalize_instrument,
    portfolio_instruments,
    safe_float,
)
from piyasa.market.quotes import (
    merge_observations,
    observation,
    positive_price,
    quote_status,
    session_from_metadata,
)
from piyasa.market.series import (
    calculate_panned_range,
    calculate_zoomed_range,
    downsample_history_rows,
    history_rows_in_window,
    normalize_history_timestamp,
    parse_history_timestamp,
    price_changes,
    sample_chart_rows,
)
from piyasa.market.watchlist import WatchlistManager
from piyasa.paths import ASSET_DIR, app_path, log_message
from piyasa.portfolio.transactions import TransactionManager
from piyasa.ui.components import (
    HoverButton,
    IconButton,
    SegmentedControl,
    ThemedPopupMenu,
    monitor_work_area,
)
from piyasa.ui.desktop_workspace import DesktopWorkspace, quantity_text
from piyasa.ui.formatting import format_instrument_value
from piyasa.ui.portfolio_dialog import PortfolioManagerDialog
from piyasa.ui.rendering import render_asset_ring, render_chart_line
from piyasa.ui.theme import (
    SPACING,
    THEME,
    configure_ttk_styles,
    enable_high_dpi,
    font,
    turkish_number,
)
from piyasa.ui.watchlist_dialog import WatchlistDialog
from piyasa.windows import AutoStartManager, GITHUB_REPO, UpdateManager, VERSION


class PiyasaWidget:
    ROW_DRAG_HOLD_MS = 350
    ROW_DRAG_MOVE_TOLERANCE_PX = 6
    SUMMARY_WATCH_COUNT = 5
    DEFAULT_WIDGET_WIDTH = 368
    DEFAULT_WIDGET_HEIGHT = 536
    DEFAULT_RIGHT_MARGIN = 30
    DEFAULT_TOP_MARGIN = 50
    CHART_PERIOD_OPTIONS = (
        ("30 dakika", 30 * 60),
        ("1 saat", 60 * 60),
        ("6 saat", 6 * 60 * 60),
        ("12 saat", 12 * 60 * 60),
        ("24 saat", 24 * 60 * 60),
        ("7 gün", 7 * 24 * 60 * 60),
        ("30 gün", 30 * 24 * 60 * 60),
        ("Tümü", 0),
    )

    @classmethod
    def calculate_default_geometry(cls, screen_width, screen_height):
        width = min(cls.DEFAULT_WIDGET_WIDTH, max(300, screen_width - 20))
        available_height = max(360, screen_height - 100)
        height = min(cls.DEFAULT_WIDGET_HEIGHT, available_height)
        x = max(0, screen_width - width - cls.DEFAULT_RIGHT_MARGIN)
        y = max(0, min(cls.DEFAULT_TOP_MARGIN, screen_height - height))
        return width, height, x, y

    def _initial_widget_geometry(self, screen_width, screen_height):
        default = self.calculate_default_geometry(screen_width, screen_height)
        saved = self._preferences.get("widget_geometry")
        if not isinstance(saved, dict):
            return default
        width, height, x, y = (saved.get(key) for key in ("width", "height", "x", "y"))
        if (
            not all(type(value) is int for value in (width, height, x, y))
            or width <= 0 or height <= 0
            or not all(-2**30 <= value <= 2**30 for value in (x, y))
        ):
            return default
        # Use the visible header when checking whether the previous display still exists.
        area = monitor_work_area(x, y, min(128, width), 28)
        if area is None:
            return default
        left, top, right, bottom = area
        width = min(max(300, width), right - left)
        height = min(max(360, height), bottom - top)
        return width, height, min(max(left, x), right - width), min(max(top, y), bottom - height)

    def __init__(self):
        enable_high_dpi()
        self.root = tk.Tk()
        self.root.title("Market Widget")

        # --- AYARLAR ---
        self.bg_color = "#1e1e1e"  # Koyu Gri Arka Plan
        self.text_color = "#00ff41" # Matrix Yeşili
        self.alpha = 1.0           # Saydamlık kapalı (tam opak)
        self.refresh_rate = 60     # Saniye cinsinden yenileme
        self.preferences_path = app_path("preferences.json")
        self._preferences = self._load_preferences()
        self.default_topmost = self._preferences.get("topmost") is True

        # Pencere Ayarları
        self.root.overrideredirect(True) # Çerçevesiz
        if self.alpha < 1.0:
            self.root.attributes("-alpha", self.alpha)
        # self.root.attributes("-topmost", True) # Her zaman üstte - Widget modunda her zaman üstte olması istenmeyebilir, ama widget mantığı genelde masaüstünde durur. Kullanıcı "arkaplanda" dedi.
        # Kullanıcı "üstte" demedi, "arkaplanda" dedi. Genellikle widgetlar masaüstünde durur (altta).
        # Ancak "Topmost" açık olursa diğer pencerelerin üstünde durur. Kullanıcı bunu istemiyor olabilir.
        # "Programın altta uygulama olarak gözükerek değil" -> Taskbar'da görünmesin.

        if self.default_topmost:
            self.root.attributes("-topmost", True)

        self.root.configure(bg=self.bg_color)
        self.brand_logo_path = str(ASSET_DIR / "icon.ico")
        self._brand_logo = None
        try:
            with Image.open(self.brand_logo_path) as logo:
                self._brand_logo = ImageTk.PhotoImage(logo.ico.getimage((24, 24)), master=self.root)
            self.root.iconbitmap(default=self.brand_logo_path)
        except (OSError, tk.TclError):
            pass

        # Taskbar'dan gizleme (Windows Widget Modu)
        self.make_toolwindow()

        # Başlangıç Konumu (Sağ Üst)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width, height, x, y = self._initial_widget_geometry(
            screen_width, screen_height
        )
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        # Managers
        self.tm = TransactionManager()
        self.asm = AutoStartManager()
        self.um = UpdateManager(VERSION, GITHUB_REPO)
        self.watchlist_manager = WatchlistManager()
        self.watchlist = self.watchlist_manager.instruments
        self.history_db = MarketHistoryDB()
        self.current_page = 0
        self._fetch_lock = threading.Lock()
        self._history_fetch_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._history_thread = None
        self._history_resync_requested = False
        self._last_prices = {}
        self.workspace = None
        self._row_hold_after_id = None
        self._row_press_key = None
        self._row_press_origin = None
        self._row_press_last = None
        self._row_drag_key = None
        self._row_drag_order = None
        self._row_drag_original_order = None
        self.price_row_widgets = {}
        self.sparkline_period = tk.IntVar(master=self.root, value=self._preferences["sparkline_period"])

        # UI Elemanları
        self.setup_ui()
        self._update_portfolio_view()

        # Sürükleme Özelliği
        self._is_resizing = False
        self._window_move_origin = None
        self.root.bind("<Button-1>", self.start_move)
        self.root.bind("<B1-Motion>", self.do_move)
        self.root.bind("<ButtonRelease-1>", self._on_button_release)

        # Sağ Tık Menüsü
        menu_style = {
            "bg": self.color_card_alt,
            "fg": self.color_text_main,
            "activebackground": self.color_accent,
            "activeforeground": THEME["primary_ink"],
            "disabledforeground": self.color_text_muted,
            "bd": 0,
            "relief": "flat",
        }
        self.menu = tk.Menu(self.root, tearoff=0, **menu_style)
        self.menu.add_command(label="Kapat", command=self.kapat)
        self.root.bind("<Button-3>", self.show_menu)

        # Ayarlar Menüsü (çark simgesi)
        self.var_autostart = tk.BooleanVar(value=self.asm.is_enabled())
        self.var_topmost = tk.BooleanVar(value=self.default_topmost)
        self.settings_menu = ThemedPopupMenu(self.root, width=340)
        self.settings_menu.add_command(label="Portföyü yönet", command=self.open_add_transaction)
        self.settings_menu.add_command(label="İşlemleri içe aktar", command=self.import_transactions)
        self.settings_menu.add_command(label="Veri ve yedek", command=lambda: self.open_workspace("settings"))
        self.settings_menu.add_separator()
        self.settings_menu.add_checkbutton(
            label="Windows ile başlat",
            variable=self.var_autostart,
            command=self.toggle_autostart,
        )
        self.settings_menu.add_checkbutton(
            label="Her zaman üstte",
            variable=self.var_topmost,
            command=self.toggle_topmost,
        )
        self.settings_menu.add_separator()
        self.sparkline_period_control = self.settings_menu.add_choice(
            label="Mini grafik zaman aralığı",
            variable=self.sparkline_period,
            options=(("30 dk", 1800), ("1 sa", 3600), ("6 sa", 21600), ("12 sa", 43200),
                     ("24 sa", 86400), ("7 gün", 604800), ("30 gün", 2592000), ("Tümü", 0)),
            command=self._on_sparkline_period_changed,
        )
        self.settings_menu.add_separator()
        self.settings_menu.add_command(label="Detayları aç", command=self.open_workspace)
        self.settings_menu.add_command(label="İstatistikler", command=lambda: self.show_page(2))
        self.settings_menu.add_command(
            label="İzlenenleri düzenle",
            command=self.open_watchlist_settings,
        )
        self.settings_menu.add_command(
            label="Güncellemeleri kontrol et",
            command=self.check_updates,
        )
        self.settings_menu.add_separator()
        self.settings_menu.add_command(
            label="Kapat",
            command=self.kapat,
            danger=True,
        )

        cached = self._compatible_cached_data(self.load_last_data())
        if cached:
            self.guncelle_arayuz(cached)
        self.update_thread = threading.Thread(target=self.veri_dongusu, daemon=True)
        self.update_thread.start()

    def _load_preferences(self):
        try:
            with open(self.preferences_path, "r", encoding="utf-8") as source:
                preferences = json.load(source)
            if not isinstance(preferences, dict):
                preferences = {}
        except (OSError, ValueError):
            preferences = {}
        allowed = {value for _label, value in self.CHART_PERIOD_OPTIONS}
        period = preferences.get("sparkline_period")
        if type(period) is not int or period not in allowed:
            preferences["sparkline_period"] = 86400
        return preferences

    def _save_preferences(self):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(self.preferences_path),
                prefix=".preferences-", suffix=".json", delete=False,
            ) as target:
                temporary = target.name
                json.dump(self._preferences, target, ensure_ascii=False, indent=2)
            os.replace(temporary, self.preferences_path)
        except OSError as exc:
            log_message(f"Pencere veya grafik ayarı kaydedilemedi: {exc}")
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError as exc:
                    log_message(f"Geçici ayar dosyası silinemedi: {exc}")

    def _on_sparkline_period_changed(self):
        self._preferences["sparkline_period"] = self.sparkline_period.get()
        self._save_preferences()
        if hasattr(self, "var_watch_period"):
            self.var_watch_period.set(self._widget_watch_period())
        self._refresh_history_views()

    def _save_widget_geometry(self, previous_position=None):
        if not self.root.winfo_ismapped():
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if (x, y) == previous_position:
            return
        geometry = {
            "width": self.root.winfo_width(), "height": self.root.winfo_height(),
            "x": x, "y": y,
        }
        if self._preferences.get("widget_geometry") != geometry:
            self._preferences["widget_geometry"] = geometry
            self._save_preferences()

    def setup_ui(self):
        """Build the compact desktop widget without changing its state contract."""
        configure_ttk_styles(self.root)

        # Shared theme tokens are mirrored onto the legacy attributes used by
        # the existing render/update callbacks.
        self.bg_color = THEME["background"]
        self.color_card = THEME["surface"]
        self.color_card_alt = THEME["surface_alt"]
        self.color_border = THEME["border"]
        self.color_text_main = THEME["text_primary"]
        self.color_text_dim = THEME["text_secondary"]
        self.color_text_muted = THEME["text_muted"]
        self.color_accent = THEME["primary"]
        self.color_success = THEME["positive"]
        self.color_danger = THEME["negative"]
        self.color_gold = THEME["gold"]
        self.color_success_bg = THEME["positive_soft"]
        self.color_danger_bg = THEME["negative_soft"]
        self.root.configure(bg=self.bg_color)

        # Dynamic fonts are retained because resize handling updates these
        # exact tkfont.Font instances in-place.
        self.base_fonts = {
            "header": 9,
            "label": 9,
            "value": 11,
            "portfolio": 25,
            "profit": 9,
            "market_status": 14,
            "icon": 10,
            "small": 8,
            "badge": 8,
            "chart_text": 10,
            "chart_title": 8,
            "chart_tick": 6,
            "chart_val": 7,
            "stats_title": 8,
            "stats_val": 7
        }

        font_family = font(self.root, "body")[0]
        self.font_family = font_family
        self.font_header = tkfont.Font(family=font_family, size=self.base_fonts["header"])
        self.font_label = tkfont.Font(family=font_family, size=self.base_fonts["label"], weight="bold")
        self.font_value = tkfont.Font(family=font_family, size=self.base_fonts["value"], weight="bold")
        self.font_portfolio = tkfont.Font(family=font_family, size=self.base_fonts["portfolio"], weight="bold")
        self.font_profit = tkfont.Font(family=font_family, size=self.base_fonts["profit"], weight="bold")
        self.font_market_status = tkfont.Font(family=font_family, size=self.base_fonts["market_status"], weight="bold")
        self.font_icon = tkfont.Font(family=font_family, size=10)
        self.font_small = tkfont.Font(family=font_family, size=8)
        self.font_badge = tkfont.Font(family=font_family, size=8, weight="bold")

        # Responsive tasarım için takip
        self.last_width = 320
        self._resize_after_id = None  # Debounce timer
        self.root.bind("<Configure>", self.on_resize)

        # Ana Konteyner
        self.frame = tk.Frame(
            self.root,
            bg=self.bg_color,
            padx=SPACING["sm"],
            pady=SPACING["sm"],
        )
        self.frame.pack(fill="both", expand=True)

        # 1. ÜST HEADER (durum + saat + sayfa navigasyonu)
        header_frame = tk.Frame(self.frame, bg=self.bg_color)
        header_frame.pack(fill="x", pady=(0, SPACING["xs"]))

        brand_frame = tk.Frame(header_frame, bg=self.bg_color)
        brand_frame.pack(fill="x", pady=(0, SPACING["xxs"]))
        if self._brand_logo is not None:
            tk.Label(brand_frame, image=self._brand_logo, bg=self.bg_color, bd=0).pack(side="left", padx=(0, 4))
        tk.Label(
            brand_frame, text="Piyasa", bg=self.bg_color, fg=self.color_text_main,
            font=font(self.root, "section", "bold"), anchor="w",
        ).pack(side="left")
        self.btn_open_workspace = HoverButton(
            brand_frame, text="Detaylar", command=self._open_workspace_from_widget,
            bg=self.color_card_alt, fg=self.color_accent,
            hover_bg=THEME["surface_hover"], pressed_bg=THEME["surface_pressed"],
            padx=SPACING["sm"], pady=3,
        )
        self.btn_open_workspace.pack(side="right")

        status_bar = tk.Frame(header_frame, bg=self.bg_color)
        status_bar.pack(fill="x")
        status_frame = tk.Frame(status_bar, bg=self.bg_color)
        status_frame.pack(side="left", fill="x", expand=True)

        self.var_market_status = tk.StringVar(value="●")
        self.lbl_market_status = tk.Label(
            status_frame,
            textvariable=self.var_market_status,
            bg=self.bg_color,
            fg=self.color_text_dim,
            font=self.font_header,
            anchor="center",
            pady=0,
        )
        self.lbl_market_status.pack(side="left", fill="y", padx=(0, SPACING["xxs"]))

        self.var_market_label = tk.StringVar(value="Piyasa bekleniyor")
        self.lbl_market_label = tk.Label(
            status_frame,
            textvariable=self.var_market_label,
            bg=self.bg_color,
            fg=self.color_text_dim,
            font=self.font_header,
            anchor="w",
        )
        self.lbl_market_label.pack(side="left", fill="y")

        # Navigasyon çerçevesi
        nav_frame = tk.Frame(status_bar, bg=self.bg_color)
        nav_frame.pack(side="right")

        self.var_time = tk.StringVar(value="--:--")
        self.lbl_time = tk.Label(nav_frame, textvariable=self.var_time, bg=self.bg_color, fg=self.color_text_dim, font=self.font_header, anchor="center")
        self.lbl_time.pack(side="left", fill="y", padx=4)
        status_frame.pack_configure(fill="both")

        navigation = tk.Frame(header_frame, bg=self.bg_color)
        navigation.pack(fill="x", pady=(SPACING["xxs"], 0))
        self.widget_nav = {}
        for mode, label in (("summary", "Özet"), ("watch", "İzleme"), ("portfolio", "Portföy")):
            button = HoverButton(
                navigation, text=label, command=lambda target=mode: self._show_widget_overview(target),
                bg=self.color_card_alt, fg=self.color_text_main, hover_bg=THEME["surface_hover"],
                pressed_bg=THEME["surface_pressed"], padx=7, pady=2,
            )
            button.pack(side="left", padx=(0, 4))
            self.widget_nav[mode] = button
        self._overview_mode = "summary"
        self._summary_breakdown_open = False

        # 4. FOOTER (kompakt araç çubuğu) - footer önce pack edilir (side=bottom)
        footer_frame = tk.Frame(
            self.frame,
            bg=self.color_card,
            padx=SPACING["xs"],
            pady=SPACING["xxs"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        footer_frame.pack(side="bottom", fill="x", pady=(SPACING["xs"], 0))

        self.btn_quick_trade = HoverButton(
            footer_frame, text="+  İşlem ekle", command=lambda: self.open_add_transaction(),
            padx=SPACING["sm"], pady=4,
        )
        self.btn_quick_trade.pack(side="left")
        self.var_trade_feedback = tk.StringVar(master=self.root)
        tk.Label(footer_frame, textvariable=self.var_trade_feedback, bg=self.color_card,
                 fg=self.color_accent, font=self.font_small).pack(side="left", padx=4)
        self.root.bind("<Destroy>", self._cancel_trade_feedback, add="+")
        self.footer_buttons = {}
        for key, icon_name, callback in (
            ("settings", "settings", self.open_settings),
            ("refresh", "refresh", self.request_data_refresh),
        ):
            button = IconButton(
                footer_frame,
                icon_name,
                callback,
                size=32,
                bg=self.color_card,
                hover_bg=THEME["surface_hover"],
                fg=self.color_text_dim,
                hover_fg=self.color_text_main,
                show_border=False,
            )
            button.pack(side="right", padx=(SPACING["xs"], 0))
            self.footer_buttons[key] = button

        # 2. İÇERİK KONTEYNERİ (Sayfa bazlı geçiş)
        self.content_container = tk.Frame(self.frame, bg=self.bg_color)
        self.content_container.pack(fill="both", expand=True)

        # --- Sayfa 0: Ana Sayfa ---
        self.page_main = tk.Frame(self.content_container, bg=self.bg_color)
        self.page_main.grid_rowconfigure(0, weight=1)
        self.page_main.grid_columnconfigure(0, weight=1)
        self.overview_canvas = tk.Canvas(
            self.page_main, bg=self.bg_color, highlightthickness=0, bd=0,
            yscrollincrement=28,
        )
        self.overview_canvas.grid(row=0, column=0, sticky="nsew")
        self.overview_scrollbar = ttk.Scrollbar(
            self.page_main, command=self.overview_canvas.yview, orient="vertical",
            style="Modern.Vertical.TScrollbar",
        )
        self.overview_canvas.configure(yscrollcommand=self.overview_scrollbar.set)
        self.overview_body = tk.Frame(self.overview_canvas, bg=self.bg_color)
        self._overview_window = self.overview_canvas.create_window(
            (0, 0), window=self.overview_body, anchor="nw",
        )
        self.overview_canvas.bind("<Configure>", self._resize_overview)
        self.overview_body.bind("<Configure>", self._sync_overview_scroll)
        self.root.bind("<MouseWheel>", self._on_overview_mousewheel, add="+")

        self.portfolio_frame = portfolio_frame = tk.Frame(
            self.overview_body,
            bg=self.color_card,
            padx=SPACING["sm"],
            pady=SPACING["xs"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        portfolio_frame.pack(fill="x", pady=(0, SPACING["xs"]))

        portfolio_header = tk.Frame(portfolio_frame, bg=self.color_card)
        portfolio_header.pack(fill="x")
        tk.Label(portfolio_header, text="Portföy değeri", bg=self.color_card, fg=self.color_text_dim, font=self.font_header).pack(side="left")

        self.var_portfolio = tk.StringVar(value="₺...")
        tk.Label(portfolio_frame, textvariable=self.var_portfolio, bg=self.color_card, fg=self.color_text_main, font=self.font_portfolio, anchor="w").pack(fill="x", pady=(0, SPACING["xxs"]))

        self.var_profit = tk.StringVar(value="...")
        portfolio_footer = tk.Frame(portfolio_frame, bg=self.color_card)
        portfolio_footer.pack(fill="x")
        self.lbl_profit = tk.Label(portfolio_footer, textvariable=self.var_profit, bg=THEME["surface_hover"], fg=self.color_text_dim, font=self.font_profit, anchor="w", padx=SPACING["xs"], pady=2)
        self.lbl_profit.pack(side="left")
        self.var_portfolio_coverage = tk.StringVar(master=self.root)
        tk.Label(portfolio_footer, textvariable=self.var_portfolio_coverage, bg=self.color_card,
                 fg=self.color_text_dim, font=self.font_small).pack(side="right")

        self.portfolio_breakdown_frame = tk.Frame(portfolio_frame, bg=self.color_card)
        self.btn_portfolio_breakdown = HoverButton(
            portfolio_header, text="Pozisyonlar", command=self._toggle_widget_breakdown,
            bg=self.color_card_alt, fg=self.color_text_main, hover_bg=THEME["surface_hover"],
            pressed_bg=THEME["surface_pressed"], padx=8, pady=2,
        )
        self.btn_portfolio_breakdown.pack(side="right")

        watch_header = self.watch_header = tk.Frame(self.overview_body, bg=self.bg_color)
        watch_header.pack(fill="x", pady=(0, SPACING["xxs"]))
        self.var_watch_period = tk.StringVar(master=self.root, value=self._widget_watch_period())
        tk.Label(
            watch_header, text="İzleme listesi", bg=self.bg_color,
            fg=self.color_text_main, font=self.font_label, anchor="w",
        ).pack(side="left")
        tk.Label(watch_header, textvariable=self.var_watch_period, bg=self.bg_color,
                 fg=self.color_text_dim, font=self.font_small).pack(side="right")
        self.price_rows_frame = tk.Frame(self.overview_body, bg=self.bg_color)
        self.price_rows_frame.pack(fill="x")
        self.price_vars = {}
        self.price_change_vars = {}
        self.price_change_labels = {}
        self.price_spark_canvases = {}
        self.price_row_widgets = {}
        self.rebuild_price_rows()

        # --- Sayfa 1: Grafik Sayfası ---
        self.page_chart = tk.Frame(self.content_container, bg=self.bg_color)
        self._build_chart_page()

        # --- Sayfa 2: İstatistik Sayfası ---
        self.page_stats = tk.Frame(self.content_container, bg=self.bg_color)
        self._build_stats_page()

        # Sayfa listesi ve ilk sayfa
        self.pages = [self.page_main, self.page_chart, self.page_stats]
        self.show_page(0)

        # --- Yeniden Boyutlandırma (Resize Grip) ---
        self.grip = IconButton(self.root, "grip", None, size=14,
                               bg=self.bg_color, fg=self.color_border, show_border=False)
        self.grip.configure(cursor="sizing")
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.grip.bind("<ButtonPress-1>", self.start_resize)
        self.grip.bind("<B1-Motion>", self.do_resize)
        self.grip.bind("<ButtonRelease-1>", self._on_resize_end)

    def _resize_overview(self, event):
        self.overview_canvas.itemconfigure(self._overview_window, width=event.width)
        self._sync_overview_scroll()

    def _sync_overview_scroll(self, _event=None):
        self.overview_canvas.configure(scrollregion=self.overview_canvas.bbox("all"))
        needs_scrollbar = self.overview_body.winfo_reqheight() > self.overview_canvas.winfo_height()
        if needs_scrollbar and not self.overview_scrollbar.winfo_manager():
            self.overview_scrollbar.grid(row=0, column=1, sticky="ns", padx=(SPACING["xxs"], 0))
        elif not needs_scrollbar and self.overview_scrollbar.winfo_manager():
            self.overview_scrollbar.grid_remove()
            self.overview_canvas.yview_moveto(0)

    def _on_overview_mousewheel(self, event):
        if self.current_page == 0 and self.overview_scrollbar.winfo_ismapped() and event.delta:
            self.overview_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

    def _draw_asset_icon(self, canvas, instrument):
        """Combine an antialiased ring with native, DPI-aware font rendering."""
        canvas.delete("all")
        color = instrument.get("color", self.color_accent)
        canvas._asset_image = ImageTk.PhotoImage(render_asset_ring(24, color, None), master=canvas)
        canvas.create_image(0, 0, anchor="nw", image=canvas._asset_image)
        label = str(instrument.get("label") or instrument.get("key") or "?")
        mark = {"gumus_ons": "Ag", "gumus_tl": "Ag", "altin_tl": "Au", "dolar": "$", "thyao": "TH"}
        text = mark.get(instrument.get("key"), label[:2].upper())
        canvas._asset_font = tkfont.Font(root=canvas, family=self.font_small.actual("family"), size=-10, weight="bold")
        while canvas._asset_font.measure(text) > 15 and canvas._asset_font.cget("size") < -6:
            canvas._asset_font.configure(size=canvas._asset_font.cget("size") + 1)
        canvas.create_text(12, 12, text=text, fill=color, font=canvas._asset_font)

    def _set_price_row_hover(self, row, surface_widgets, key, entered):
        """Apply an event-only row hover without masking drag feedback."""
        if not row.winfo_exists():
            return
        bg = THEME["surface_hover"] if entered else self.color_card_alt
        row.configure(bg=bg)
        for widget in surface_widgets:
            if widget.winfo_exists():
                widget.configure(bg=bg)
        if self._row_drag_key != key:
            row.configure(
                highlightbackground=THEME["border_strong"] if entered else self.color_border
            )

    def create_price_row(self, instrument, parent=None):
        if parent is None:
            parent = self.frame
        row = tk.Frame(
            parent,
            bg=self.color_card_alt,
            padx=SPACING["xs"],
            pady=2,
            takefocus=True,
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        row.pack(fill="x", pady=SPACING["xxs"] // 2)
        row.grid_columnconfigure(1, weight=1)

        icon = tk.Canvas(row, width=24, height=24, bg=self.color_card_alt, highlightthickness=0, bd=0)
        icon.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 6))
        self._draw_asset_icon(icon, instrument)

        name_label = tk.Label(
            row, text=instrument["label"], width=1, bg=self.color_card_alt,
            fg=self.color_text_main, font=self.font_label, anchor="w",
        )
        name_label.grid(row=0, column=1, sticky="sew", padx=(0, SPACING["xs"]))
        quote_label = tk.Label(row, text="—", width=1, bg=self.color_card_alt,
                               fg=self.color_text_dim, font=self.font_small, anchor="w", pady=0)
        quote_label.grid(row=1, column=1, sticky="new", padx=(0, 4))
        self.widget_quote_labels[instrument["key"]] = quote_label

        spark = tk.Canvas(row, width=62, height=22, bg=self.color_card_alt, highlightthickness=0, bd=0)
        spark.grid(row=0, column=2, rowspan=2, sticky="e", padx=(0, SPACING["xs"]))

        placeholder = f"{instrument.get('currency', '')}..."
        var = tk.StringVar(value=placeholder)
        self.price_vars[instrument["key"]] = var
        if instrument["key"] == "gumus_ons":
            self.var_gumus_ons = var
        elif instrument["key"] == "gumus_tl":
            self.var_gumus_tl = var
        elif instrument["key"] == "altin_tl":
            self.var_altin_tl = var
        price_label = tk.Label(row, textvariable=var, bg=self.color_card_alt, fg=self.color_text_main, font=self.font_value, anchor="e")
        price_label.grid(row=0, column=3, rowspan=2, sticky="e", padx=(0, 6))

        change_var = tk.StringVar(value="--")
        self.price_change_vars[instrument["key"]] = change_var
        badge = tk.Label(row, textvariable=change_var, bg=THEME["surface_pressed"], fg=self.color_text_muted, font=self.font_badge, padx=6, pady=2)
        badge.grid(row=0, column=4, rowspan=2, sticky="e")
        self.price_change_labels[instrument["key"]] = badge
        self.price_spark_canvases[instrument["key"]] = spark
        self.price_row_widgets[instrument["key"]] = row
        drag_widgets = (row, icon, name_label, quote_label, spark, price_label, badge)
        self._bind_price_row_drag(drag_widgets, instrument["key"])
        row.bind("<Return>", lambda _event: self._open_price_row_chart(instrument["key"]))

        surface_widgets = (icon, name_label, quote_label, spark, price_label)
        for widget in drag_widgets:
            widget.bind(
                "<Enter>",
                lambda event, r=row, ws=surface_widgets, key=instrument["key"]: self._set_price_row_hover(r, ws, key, True),
                add="+",
            )
            widget.bind(
                "<Leave>",
                lambda event, r=row, ws=surface_widgets, key=instrument["key"]: self._set_price_row_hover(r, ws, key, False),
                add="+",
            )

    def rebuild_price_rows(self):
        if not hasattr(self, "price_rows_frame"):
            return
        self._cancel_price_row_drag(restore=True)
        for child in self.price_rows_frame.winfo_children():
            child.destroy()
        self.price_vars = {}
        self.price_change_vars = {}
        self.price_change_labels = {}
        self.price_spark_canvases = {}
        self.price_row_widgets = {}
        self.widget_quote_labels = {}
        for instrument in self.watchlist:
            self.create_price_row(instrument, self.price_rows_frame)
        self._layout_price_rows([instrument["key"] for instrument in self.watchlist])
        self._update_widget_quote_labels()

    def _bind_price_row_drag(self, widgets, key):
        for widget in widgets:
            widget.config(cursor="hand2")
            widget.bind(
                "<ButtonPress-1>",
                lambda event, item_key=key: self._on_price_row_press(event, item_key),
                add="+"
            )
            widget.bind(
                "<B1-Motion>",
                lambda event, item_key=key: self._on_price_row_motion(event, item_key),
                add="+"
            )
            widget.bind(
                "<ButtonRelease-1>",
                lambda event, item_key=key: self._on_price_row_release(event, item_key),
                add="+"
            )

    def _on_price_row_press(self, event, key):
        self._cancel_price_row_drag(restore=True)
        self._row_press_key = key
        self._row_press_origin = (event.x_root, event.y_root)
        self._row_press_last = self._row_press_origin
        self._row_hold_after_id = self.root.after(
            self.ROW_DRAG_HOLD_MS,
            lambda item_key=key: self._activate_price_row_drag(item_key),
        )
        # Every part of a quote row owns this gesture, including its padding.
        return "break"

    def _activate_price_row_drag(self, key):
        self._row_hold_after_id = None
        if self._row_press_key != key or not self._row_press_origin:
            return
        self._row_drag_key = key
        self._row_drag_original_order = [item["key"] for item in self.watchlist]
        self._row_drag_order = list(self._row_drag_original_order)
        row = self.price_row_widgets.get(key)
        if row:
            row.config(highlightbackground=self.color_accent)
            for widget in (row, *row.winfo_children()):
                widget.configure(cursor="fleur")
        self.root.config(cursor="fleur")

    def _on_price_row_motion(self, event, key):
        if self._row_press_key != key:
            return None
        self._row_press_last = (event.x_root, event.y_root)

        if self._row_drag_key != key:
            origin = self._row_press_origin
            if origin:
                dx = event.x_root - origin[0]
                dy = event.y_root - origin[1]
                tolerance = self.ROW_DRAG_MOVE_TOLERANCE_PX
                if (dx * dx) + (dy * dy) > tolerance * tolerance:
                    if self._row_hold_after_id is not None:
                        self.root.after_cancel(self._row_hold_after_id)
                    self._activate_price_row_drag(key)
            if self._row_drag_key != key:
                return "break"

        self.root.update_idletasks()
        midpoints = {}
        for item_key in self._row_drag_order or []:
            if item_key == key:
                continue
            row = self.price_row_widgets.get(item_key)
            if row and row.winfo_ismapped():
                midpoints[item_key] = row.winfo_rooty() + (row.winfo_height() / 2)
        new_order = calculate_reordered_keys(
            self._row_drag_order,
            key,
            event.y_root,
            midpoints
        )
        if new_order != self._row_drag_order:
            self._row_drag_order = new_order
            self._layout_price_rows(new_order)
        return "break"

    def _on_price_row_release(self, event, key):
        if self._row_drag_key != key:
            origin = self._row_press_origin
            is_click = self._row_press_key == key and origin is not None
            if is_click:
                dx = event.x_root - origin[0]
                dy = event.y_root - origin[1]
                tolerance = self.ROW_DRAG_MOVE_TOLERANCE_PX
                is_click = (dx * dx) + (dy * dy) <= tolerance * tolerance
            self._cancel_price_row_drag()
            if is_click:
                self._open_price_row_chart(key)
                return "break"
            return "break"

        original_order = list(self._row_drag_original_order or [])
        final_order = list(self._row_drag_order or original_order)
        try:
            self.watchlist_manager.reorder(final_order)
        except Exception as e:
            log_message(f"İzleme listesi sırası kaydedilemedi: {e}")
            self._layout_price_rows(original_order)
        else:
            self.watchlist = self.watchlist_manager.instruments
            self._layout_price_rows([item["key"] for item in self.watchlist])
            try:
                if hasattr(self, "chart_symbol_frame"):
                    self._rebuild_chart_symbol_buttons()
                if hasattr(self, "stats_frame"):
                    self._rebuild_stats_sections()
            except Exception as e:
                log_message(f"Bağlı görünüm sırası yenilenemedi: {e}")
        finally:
            self._cancel_price_row_drag()
        return "break"

    def _open_price_row_chart(self, key):
        """Open the chart page with the clicked watchlist instrument selected."""
        valid_keys = {item["key"] for item in self.watchlist}
        if key not in valid_keys or not hasattr(self, "chart_var"):
            return
        self.chart_var.set(key)
        self.show_page(1)

    def _layout_price_rows(self, ordered_keys):
        rows = []
        for key in ordered_keys:
            row = self.price_row_widgets.get(key)
            if row and row.winfo_exists():
                rows.append(row)
        for row in rows:
            row.pack_forget()
        visible = rows[:self.SUMMARY_WATCH_COUNT] if self._overview_mode == "summary" else rows
        for row in visible:
            row.pack(fill="x", pady=SPACING["xxs"] // 2)

    def _widget_watch_period(self):
        return next((label for label, seconds in self.CHART_PERIOD_OPTIONS if seconds == self.sparkline_period.get()), "")

    def _update_widget_quote_labels(self):
        for key, label in getattr(self, "widget_quote_labels", {}).items():
            status = getattr(self, "workspace_quote_status", None)
            if status is not None:
                label.configure(text=status(key, compact=True))

    def _toggle_widget_breakdown(self):
        self._summary_breakdown_open = not self._summary_breakdown_open
        self._sync_widget_breakdown()

    def _sync_widget_breakdown(self):
        if self._overview_mode == "portfolio":
            self.btn_portfolio_breakdown.pack_forget()
        else:
            self.btn_portfolio_breakdown.pack(side="right")
        if self._overview_mode == "portfolio" or self._summary_breakdown_open:
            self.portfolio_breakdown_frame.pack(fill="x", pady=(SPACING["xs"], 0))
            self.btn_portfolio_breakdown.config(text="Pozisyonları gizle")
        else:
            self.portfolio_breakdown_frame.pack_forget()
            self.btn_portfolio_breakdown.config(text="Pozisyonlar")

    def _show_widget_overview(self, mode):
        self._cancel_price_row_drag(restore=True)
        self._overview_mode = mode
        self.portfolio_frame.pack_forget()
        self.watch_header.pack_forget()
        self.price_rows_frame.pack_forget()
        if mode != "watch":
            self.portfolio_frame.pack(fill="x", pady=(0, SPACING["xs"]))
        if mode != "portfolio":
            self.watch_header.pack(fill="x", pady=(0, SPACING["xxs"]))
            self.price_rows_frame.pack(fill="x")
        self._sync_widget_breakdown()
        self._layout_price_rows([instrument["key"] for instrument in self.watchlist])
        self.show_page(0)
        self.overview_canvas.yview_moveto(0)

    def _cancel_price_row_drag(self, restore=False):
        after_id = getattr(self, "_row_hold_after_id", None)
        if after_id is not None and hasattr(self, "root"):
            try:
                self.root.after_cancel(after_id)
            except (tk.TclError, ValueError):
                pass
        if restore and getattr(self, "_row_drag_original_order", None):
            self._layout_price_rows(self._row_drag_original_order)
        drag_key = getattr(self, "_row_drag_key", None)
        row = getattr(self, "price_row_widgets", {}).get(drag_key)
        if row and row.winfo_exists():
            row.config(highlightbackground=self.color_border)
            for widget in (row, *row.winfo_children()):
                widget.configure(cursor="hand2")
        if hasattr(self, "root"):
            try:
                self.root.config(cursor="")
            except tk.TclError:
                pass
        self._row_hold_after_id = None
        self._row_press_key = None
        self._row_press_origin = None
        self._row_press_last = None
        self._row_drag_key = None
        self._row_drag_order = None
        self._row_drag_original_order = None

    def _format_change(self, change_pct):
        if change_pct is None:
            return "--"
        sign = "+" if change_pct >= 0 else ""
        return f"{sign}{turkish_number(change_pct, 1)}%"

    def _sparkline_data(self, key, current_value):
        """Use one history series for the line and its change badge."""
        try:
            rows, _source, interval = self._cached_history_series(
                key, self._sparkline_period_seconds()
            )
        except Exception:
            rows = []
        if len(rows) < 2:
            if not rows and (price := positive_price(current_value)):
                rows = [(None, price)]
            return rows, None, ()
        first_value = float(rows[0][1])
        last_value = float(rows[-1][1])
        change_pct = ((last_value - first_value) / first_value) * 100 if first_value > 0 else None
        sampled, breaks = sample_chart_rows(rows, 40, interval)
        return sampled, change_pct, breaks

    def _sparkline_period_seconds(self):
        variable = getattr(self, "sparkline_period", None)
        return variable.get() if variable is not None else 86400

    @staticmethod
    def _history_x_ratios(times):
        if len(times) <= 1:
            return [0.5] * len(times)
        if all(time is not None for time in times) and times[-1] > times[0]:
            span = (times[-1] - times[0]).total_seconds()
            return [(time - times[0]).total_seconds() / span for time in times]
        return [index / (len(times) - 1) for index in range(len(times))]

    def _draw_sparkline(self, canvas, rows, color, breaks=()):
        if not canvas:
            return
        try:
            canvas._sparkline_payload = (tuple(rows), color, breaks)
            if not getattr(canvas, "_sparkline_resize_bound", False):
                canvas.bind(
                    "<Configure>",
                    lambda _event, target=canvas: self._draw_sparkline(
                        target, *target._sparkline_payload
                    ),
                    add="+",
                )
                canvas._sparkline_resize_bound = True
            width = canvas.winfo_width()
            height = canvas.winfo_height()
            if width <= 1:
                width = max(canvas.winfo_reqwidth(), 62)
            if height <= 1:
                height = max(canvas.winfo_reqheight(), 22)
            render_key = (width, height, canvas._sparkline_payload)
            if render_key == getattr(canvas, "_sparkline_render_key", None):
                return
            values = [float(row[1]) for row in rows]
            if not values or min(values) == max(values):
                points = [(width / 2, height / 2)] if values else []
                image = render_chart_line(width, height, points, self.color_text_muted, endpoint=1.75)
            else:
                times = [parse_history_timestamp(row[0]) for row in rows]
                min_v, max_v = min(values), max(values)
                value_range = max_v - min_v
                points = []
                for ratio, value in zip(self._history_x_ratios(times), values):
                    x = (width - 4) * ratio + 2
                    y = (height - 6) * (max_v - value) / value_range + 3
                    points.append((x, y))
                image = render_chart_line(width, height, points, color, endpoint=1.75, breaks=breaks)
            photo = ImageTk.PhotoImage(image, master=canvas)
            if hasattr(canvas, "_line_item"):
                canvas.itemconfigure(canvas._line_item, image=photo)
            else:
                canvas._line_item = canvas.create_image(0, 0, anchor="nw", image=photo)
            canvas._line_image = photo
            canvas._sparkline_render_key = render_key
        except Exception:
            pass

    def _update_price_row_visuals(self, prices):
        if not hasattr(self, "price_change_vars"):
            return
        for instrument in self.watchlist:
            key = instrument["key"]
            rows, change_pct, breaks = self._sparkline_data(key, prices.get(key))
            change_var = self.price_change_vars.get(key)
            badge = self.price_change_labels.get(key)
            if change_var:
                change_var.set(self._format_change(change_pct))
            if badge:
                if not change_pct:
                    badge.config(bg=THEME["surface_pressed"], fg=self.color_text_muted)
                elif change_pct >= 0:
                    badge.config(bg=self.color_success_bg, fg=self.color_success)
                else:
                    badge.config(bg=self.color_danger_bg, fg=self.color_danger)
            color = self.color_danger if change_pct is not None and change_pct < 0 else self.color_success
            self._draw_sparkline(self.price_spark_canvases.get(key), rows, color, breaks)

    def _format_tl(self, value, decimals=0):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "₺0"
        return f"₺{turkish_number(value, decimals)}"

    def rebuild_portfolio_breakdown(self, summary):
        if not hasattr(self, "portfolio_breakdown_frame"):
            return
        rows = tuple(summary.get("rows", ())) if summary else ()
        if rows == getattr(self, "_portfolio_rows_rendered", None):
            return
        self._portfolio_rows_rendered = rows
        for child in self.portfolio_breakdown_frame.winfo_children():
            child.destroy()

        if not rows:
            tk.Label(
                self.portfolio_breakdown_frame,
                text="Portföy boş",
                bg=self.color_card,
                fg=self.color_text_muted,
                font=self.font_small,
                anchor="w",
            ).pack(fill="x")
            return

        for row_data in rows:
            row = tk.Frame(
                self.portfolio_breakdown_frame,
                bg=self.color_card_alt,
                padx=SPACING["xs"],
                pady=4,
            )
            row.pack(fill="x", pady=SPACING["xxs"] // 2)

            marker = tk.Canvas(
                row,
                width=10,
                height=10,
                bg=self.color_card_alt,
                highlightthickness=0,
                bd=0,
            )
            marker.grid(row=0, column=0, sticky="w", padx=(0, 6))
            marker._marker_image = ImageTk.PhotoImage(render_chart_line(
                10, 10, [(5, 5)], row_data.get("color", self.color_accent), endpoint=3
            ), master=marker)
            marker.create_image(0, 0, anchor="nw", image=marker._marker_image)

            row.grid_columnconfigure(1, weight=1)
            tk.Label(row, text=row_data["label"], width=1, bg=self.color_card_alt, fg=self.color_text_main,
                     font=self.font_label, anchor="w").grid(row=0, column=1, sticky="ew")
            quantity = f"{quantity_text(row_data['quantity'])} {row_data['unit']}"
            average = self._format_tl(row_data["average_cost_tl"], 2)
            tk.Label(row, text=quantity, bg=self.color_card_alt,
                     fg=self.color_text_dim, font=self.font_small, anchor="w").grid(
                         row=1, column=1, sticky="ew")
            tk.Label(row, text=f"Ort. {average}", bg=self.color_card_alt,
                     fg=self.color_text_dim, font=self.font_small, anchor="w").grid(
                         row=2, column=1, columnspan=2, sticky="ew")

            if row_data["has_price"]:
                value_text = self._format_tl(row_data["value_tl"], 2)
            else:
                value_text = "Fiyat yok"
            tk.Label(row, text=value_text, bg=self.color_card_alt, fg=self.color_text_main, font=self.font_small, anchor="e").grid(row=0, column=2, sticky="e", padx=(SPACING["xs"], SPACING["xs"]))

            profit = row_data.get("profit_tl")
            if profit is None:
                profit_text = "--"
                profit_color = self.color_text_muted
            else:
                sign = "+" if profit > 0 else "−" if profit < 0 else ""
                profit_text = f"{sign}{self._format_tl(abs(profit), 2)}"
                profit_color = self.color_success if profit >= 0 else self.color_danger
            tk.Label(row, text=profit_text, bg=self.color_card_alt, fg=profit_color, font=self.font_badge, anchor="e").grid(row=1, column=2, sticky="e", padx=SPACING["xs"])

    # --- Sayfa Navigasyon Sistemi ---
    def show_page(self, index):
        if index != 1:
            self._cancel_chart_redraw()
        for p in self.pages:
            p.pack_forget()
        self.pages[index].pack(fill="both", expand=True)
        self.current_page = index
        for mode, button in self.widget_nav.items():
            selected = index == 0 and mode == self._overview_mode
            button.configure(bg=THEME["primary_soft"] if selected else self.color_card_alt,
                             fg=self.color_accent if selected else self.color_text_main)
        # Sayfa değiştiğinde içeriği güncelle
        if index == 1:
            self._update_chart()
        elif index == 2:
            self._update_stats()

    # --- Grafik Sayfası ---
    def _build_chart_page(self):
        selector_frame = tk.Frame(
            self.page_chart,
            bg=self.color_card,
            padx=SPACING["xs"],
            pady=SPACING["xs"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        selector_frame.pack(fill="x", pady=(0, SPACING["xs"]))

        default_key = "gumus_tl" if any(item["key"] == "gumus_tl" for item in self.watchlist) else self.watchlist[0]["key"]
        self.chart_var = tk.StringVar(value=default_key)
        self.chart_period = tk.IntVar(value=24 * 60 * 60)
        self._chart_view_start = 0.0
        self._chart_view_end = 1.0
        self._chart_pan_origin = None
        self._chart_rows = []
        self._chart_time_ordered = False
        self._chart_frame_job = None
        self._chart_settle_job = None
        self._chart_rendered_frame = None
        self._chart_instrument = None
        self._chart_history_source = None
        self._chart_history_interval = None
        self._chart_plot_bounds = (48, 14, 230, 170)

        selector_header = tk.Frame(selector_frame, bg=self.color_card)
        selector_header.pack(fill="x", pady=(0, SPACING["xs"]))
        tk.Label(
            selector_header,
            text="FİYAT GEÇMİŞİ",
            bg=self.color_card,
            fg=self.color_text_dim,
            font=self.font_header,
        ).pack(side="left")

        zoom_toolbar = tk.Frame(selector_header, bg=self.color_card)
        zoom_toolbar.pack(side="right")
        self.chart_zoom_reset_button = IconButton(
            zoom_toolbar,
            "reset",
            self.reset_chart_view,
            size=24,
            bg=self.color_card,
            hover_bg=THEME["surface_hover"],
            fg=self.color_text_muted,
            hover_fg=self.color_text_main,
            show_border=False,
        )
        self.chart_zoom_reset_button.pack(side="right", padx=(SPACING["xxs"], 0))
        self.chart_zoom_out_button = IconButton(
            zoom_toolbar,
            "minus",
            lambda: self._zoom_chart(False, 0.5),
            size=24,
            bg=self.color_card,
            hover_bg=THEME["surface_hover"],
            fg=self.color_text_muted,
            hover_fg=self.color_text_main,
            show_border=False,
        )
        self.chart_zoom_out_button.pack(side="right", padx=(SPACING["xxs"], 0))
        self.chart_zoom_in_button = IconButton(
            zoom_toolbar,
            "plus",
            lambda: self._zoom_chart(True, 0.5),
            size=24,
            bg=self.color_card,
            hover_bg=THEME["surface_hover"],
            fg=self.color_text_muted,
            hover_fg=self.color_text_main,
            show_border=False,
        )
        self.chart_zoom_in_button.pack(side="right")

        self.chart_period_control = SegmentedControl(
            selector_frame,
            self.chart_period,
            self.CHART_PERIOD_OPTIONS,
            command=self._on_chart_filter_changed,
            columns=4,
        )
        self.chart_period_control.pack(fill="x", pady=(0, SPACING["xs"]))

        self.chart_symbol_frame = tk.Frame(selector_frame, bg=self.color_card)
        self.chart_symbol_frame.pack(fill="x")
        self._rebuild_chart_symbol_buttons()

        tk.Label(
            selector_frame,
            text="Tekerlek: yakınlaştır  •  Sürükle: kaydır  •  Çift tık: sıfırla",
            bg=self.color_card,
            fg=self.color_text_muted,
            font=self.font_small,
            anchor="w",
        ).pack(fill="x", pady=(SPACING["xs"], 0))

        self.chart_canvas = tk.Canvas(
            self.page_chart,
            bg=self.color_card,
            highlightthickness=1,
            highlightbackground=self.color_border,
            bd=0,
        )
        self.chart_canvas.pack(fill="both", expand=True)
        self.chart_canvas.configure(cursor="hand2")
        self.chart_canvas.bind("<Configure>", self._on_chart_canvas_configure, add="+")
        self.chart_canvas.bind("<MouseWheel>", self._on_chart_mousewheel, add="+")
        self.chart_canvas.bind("<Button-4>", lambda event: self._on_chart_mousewheel(event, 120), add="+")
        self.chart_canvas.bind("<Button-5>", lambda event: self._on_chart_mousewheel(event, -120), add="+")
        self.chart_canvas.bind("<ButtonPress-1>", self._on_chart_pan_start, add="+")
        self.chart_canvas.bind("<B1-Motion>", self._on_chart_pan_motion, add="+")
        self.chart_canvas.bind("<ButtonRelease-1>", self._on_chart_pan_end, add="+")
        self.chart_canvas.bind("<Double-Button-1>", self.reset_chart_view, add="+")

    def _instrument_by_key(self, key):
        watched = next((item for item in self.watchlist if item["key"] == key), None)
        if watched is not None:
            return watched
        return next((item for item in self.workspace_instruments() if item["key"] == key), None)

    @staticmethod
    def _chart_selector_label(instrument):
        """Keep dynamic symbol selectors readable at the compact widget width."""
        key = instrument.get("key")
        aliases = {
            "gumus_ons": "G. ONS",
            "gumus_tl": "G. TL",
            "altin_tl": "Altın",
            "dolar": "USD",
        }
        if key in aliases:
            return aliases[key]
        label = str(instrument.get("label") or key or "Varlık")
        return label if len(label) <= 8 else f"{label[:7]}…"

    def _history_source_symbol(self, key):
        instrument = self._instrument_by_key(key)
        if (
            instrument
            and key in SILVER_SPOT_KEYS
            and instrument.get("symbol") == SILVER_SPOT_SYMBOL
        ):
            return SILVER_SPOT_SYMBOL
        return None

    def _history_source_symbols(self, key):
        """Include the live spot source and its explicit historical proxy."""
        source = self._history_source_symbol(key)
        if source == SILVER_SPOT_SYMBOL:
            return (SILVER_SPOT_SYMBOL, SILVER_HISTORY_PROXY_SYMBOL)
        return source

    def _cached_history_series(self, key, period_seconds=0):
        """Select one source and one resolution so a line cannot zig-zag.

        Provider 5-minute bars are authoritative for finite periods. Runtime
        samples are only a fallback. For the all-time view, daily bars are
        authoritative. Silver spot uses the explicitly recorded SI=F proxy
        until a genuine spot-history source is available.
        """
        instrument = self._instrument_by_key(key)
        if not instrument:
            return [], None, None
        provider_source = self._history_cache_source(instrument)
        live_source = instrument.get("symbol") or provider_source

        if not period_seconds:
            rows = self.history_db.get_all_history(
                key,
                source_symbol=provider_source,
                intervals=("1d",),
            )
            if rows:
                return self._merge_history_rows(rows), provider_source, "1d"
            for source in dict.fromkeys((live_source, provider_source)):
                rows = self.history_db.get_all_history(
                    key,
                    source_symbol=source,
                    intervals=("live",),
                )
                if rows:
                    return price_changes(self._merge_history_rows(rows)), source, "live"
            return [], provider_source, "1d"

        hours = float(period_seconds) / 3600
        rows = self.history_db.get_history(
            key,
            source_symbol=provider_source,
            hours=hours,
            intervals=("5m",),
            anchor_to_latest=True,
        )
        if rows:
            return (
                history_rows_in_window(rows, period_seconds),
                provider_source,
                "5m",
            )

        for source in dict.fromkeys((live_source, provider_source)):
            rows = self.history_db.get_history(
                key,
                source_symbol=source,
                hours=hours,
                intervals=("live",),
                anchor_to_latest_change=True,
            )
            if rows:
                return (
                    price_changes(history_rows_in_window(rows, period_seconds)),
                    source,
                    "live",
                )
        return [], provider_source, "5m"

    def _rebuild_chart_symbol_buttons(self):
        if not hasattr(self, "chart_symbol_frame"):
            return
        previous_control = getattr(self, "chart_symbol_control", None)
        if previous_control is not None:
            try:
                self.chart_var.trace_remove("write", previous_control._trace_id)
            except (AttributeError, tk.TclError):
                pass
        for child in self.chart_symbol_frame.winfo_children():
            child.destroy()
        valid_keys = {item["key"] for item in self.watchlist}
        if self.chart_var.get() not in valid_keys and self.watchlist:
            self.chart_var.set(self.watchlist[0]["key"])
        if not self.watchlist:
            return
        selection_colors = {
            instrument["key"]: (
                THEME["primary_soft"],
                instrument.get("color", self.color_accent),
            )
            for instrument in self.watchlist
        }
        self.chart_symbol_control = SegmentedControl(
            self.chart_symbol_frame,
            self.chart_var,
            tuple(
                (self._chart_selector_label(instrument), instrument["key"])
                for instrument in self.watchlist
            ),
            command=self._on_chart_filter_changed,
            selection_colors=selection_colors,
        )
        self.chart_symbol_control.pack(fill="x")

    @staticmethod
    def _filter_outliers(values, timestamps):
        """Discard malformed values without deleting valid market moves."""
        clean_vals = []
        clean_ts = []
        for v, t in zip(values, timestamps):
            try:
                numeric = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric) and numeric > 0:
                clean_vals.append(numeric)
                clean_ts.append(t)
        return clean_vals, clean_ts

    def _on_chart_filter_changed(self):
        self._chart_view_start = 0.0
        self._chart_view_end = 1.0
        self._chart_pan_origin = None
        self._update_chart()

    def _chart_period_label(self):
        selected = self.chart_period.get()
        return next(
            (label for label, value in self.CHART_PERIOD_OPTIONS if value == selected),
            "Geçmiş",
        )

    @staticmethod
    def _merge_history_rows(*collections):
        by_timestamp = {}
        for rows in collections:
            for timestamp, value in rows or []:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    by_timestamp[normalize_history_timestamp(timestamp)] = value
        return sorted(by_timestamp.items(), key=lambda row: row[0])

    def _load_selected_chart_history(self):
        selected = self.chart_var.get()
        period_seconds = self.chart_period.get()
        data, source, interval = self._cached_history_series(
            selected, period_seconds
        )
        self._chart_history_source = source
        self._chart_history_interval = interval
        return data

    def _draw_chart_message(self, text):
        self.chart_canvas.delete("all")
        self.chart_canvas._chart_line_image = None
        self._chart_rendered_frame = None
        cw = max(self.chart_canvas.winfo_width(), 230)
        ch = max(self.chart_canvas.winfo_height(), 170)
        scale = max(1.0, min(1.5, cw / 230.0))
        self.chart_canvas.create_text(
            cw // 2,
            ch // 2,
            text=text,
            fill=self.color_text_muted,
            font=(self.font_family, int(10 * scale)),
            justify="center",
        )

    def _update_chart(self):
        self._cancel_chart_redraw()
        self._chart_rows = []
        self._chart_time_ordered = False
        self._chart_rendered_frame = None
        try:
            data = self._load_selected_chart_history()
            instrument = self._instrument_by_key(self.chart_var.get())
            self._chart_instrument = instrument
            if not data or not instrument:
                self._draw_chart_message(
                    "Bu aralık için henüz veri yok\nGeçmiş arka planda tamamlanıyor"
                )
                return

            raw_values = [row[1] for row in data]
            raw_timestamps = [row[0] for row in data]
            values, timestamps = self._filter_outliers(
                raw_values, raw_timestamps
            )
            times = [parse_history_timestamp(timestamp) for timestamp in timestamps]
            self._chart_rows = list(zip(times, values))
            self._chart_time_ordered = (
                len(times) > 1
                and all(time is not None for time in times)
                and times[-1] > times[0]
                and all(left <= right for left, right in zip(times, times[1:]))
            )
            self._draw_chart()
        except Exception as exc:
            log_message(f"Chart error: {exc}")
            self._draw_chart_message("Grafik verisi okunamadı")

    def _visible_chart_rows(self):
        rows = self._chart_rows
        if len(rows) <= 2:
            return rows
        if self._chart_time_ordered:
            first = rows[0][0]
            span = rows[-1][0] - first
            start_time = first + span * self._chart_view_start
            end_time = first + span * self._chart_view_end
            start_index = bisect_left(rows, start_time, key=lambda row: row[0])
            end_index = bisect_right(rows, end_time, key=lambda row: row[0])
            if end_index - start_index < 2:
                # A viewport inside a market closure should stay near that time.
                start_index = max(0, min(start_index - 1, len(rows) - 2))
                end_index = start_index + 2
            return rows[start_index:end_index]
        last_index = len(rows) - 1
        start_index = int(self._chart_view_start * last_index)
        end_index = int(self._chart_view_end * last_index) + 1
        end_index = max(start_index + 2, min(end_index, len(rows)))
        return rows[start_index:end_index]

    def _draw_chart(self, *, preview=False):
        try:
            cw = self.chart_canvas.winfo_width()
            ch = self.chart_canvas.winfo_height()
            if cw < 10 or ch < 10:
                cw, ch = 230, 170
            frame = (cw, ch, self._chart_view_start, self._chart_view_end, preview)
            previous = self._chart_rendered_frame
            if previous and previous[:4] == frame[:4] and (preview or not previous[4]):
                return
            if not self._chart_rows or not self._chart_instrument:
                self._draw_chart_message("Bu aralık için henüz veri yok")
                return

            rows, breaks = sample_chart_rows(
                self._visible_chart_rows(),
                max(120, cw if preview else cw * 2),
                self._chart_history_interval,
            )
            if not rows:
                self._draw_chart_message("Bu aralık için henüz veri yok")
                return
            times = [row[0] for row in rows]
            values = [float(row[1]) for row in rows]
            instrument = self._chart_instrument
            color = self.color_danger if values[-1] < values[0] else self.color_success

            scale = max(1.0, min(1.5, cw / 230.0))
            f_title = int(9 * scale)
            f_tick = int(8 * scale)
            f_val = int(8 * scale)
            pad_l, pad_r, pad_t, pad_b = max(52, int(43 * scale)), 18, 46, 28
            gw = max(1, cw - pad_l - pad_r)
            gh = max(1, ch - pad_t - pad_b)
            self._chart_plot_bounds = (pad_l, pad_t, cw - pad_r, pad_t + gh)

            min_v = min(values)
            max_v = max(values)
            margin = (
                (max_v - min_v) * 0.05
                if max_v != min_v
                else max(abs(max_v) * 0.02, 0.01)
            )
            chart_min = min_v - margin
            chart_max = max_v + margin
            val_range = max(chart_max - chart_min, 0.000001)

            points = []
            for ratio, value in zip(self._history_x_ratios(times), values):
                x = pad_l + gw * ratio
                y = pad_t + gh * (1 - (value - chart_min) / val_range)
                points.append((x, y))

            # Compositing before uploading avoids Tk's costly transparent region updates.
            plot = Image.new("RGB", (cw, ch), self.color_card)
            grid = ImageDraw.Draw(plot)
            grid_rows = [pad_t + int(gh * index / 4) for index in range(5)]
            for y in grid_rows:
                grid.line((pad_l, y, cw - pad_r, y), fill=self.color_border)
            curve = render_chart_line(
                cw, ch, points, color, stroke=2, endpoint=3, fill_to=pad_t + gh,
                preview=preview, breaks=breaks,
            )
            plot.paste(curve, (0, 0), curve.getchannel("A"))
            photo = ImageTk.PhotoImage(plot, master=self.chart_canvas)

            self._chart_rendered_frame = None
            self.chart_canvas.delete("all")
            self.chart_canvas._chart_line_image = photo
            self.chart_canvas.create_image(0, 0, anchor="nw", image=photo)

            zoom_pct = int(round(100 / max(
                self._chart_view_end - self._chart_view_start, 0.001
            )))
            title = f"{instrument['label']} · {self._chart_period_label()}"
            if zoom_pct > 100:
                title += f" · %{zoom_pct}"
            self.chart_canvas.create_text(
                pad_l,
                15,
                text=title,
                fill=self.color_text_main,
                font=(self.font_family, f_title, "bold"),
                anchor="w",
            )
            latest = times[-1]
            source_note = ""
            if (
                self._chart_history_source == SILVER_HISTORY_PROXY_SYMBOL
                and instrument.get("source") in SILVER_SPOT_SOURCES
            ):
                source_note = "SI=F · "
            if latest is not None:
                self.chart_canvas.create_text(
                    cw - pad_r,
                    30,
                    text=f"{source_note}{latest.strftime('%d/%m %H:%M')}",
                    fill=self.color_text_muted,
                    font=(self.font_family, f_tick),
                    anchor="e",
                )

            for index, y in enumerate(grid_rows):
                value = chart_max - (val_range * index / 4)
                if value >= 10000:
                    formatted = f"{value:,.0f}"
                elif value >= 100:
                    formatted = f"{value:.1f}"
                else:
                    formatted = f"{value:.2f}"
                self.chart_canvas.create_text(
                    pad_l - 6,
                    y,
                    text=formatted,
                    fill=self.color_text_muted,
                    font=(self.font_family, f_tick),
                    anchor="e",
                )

            if self._chart_time_ordered:
                visible_span = times[-1] - times[0]
                # Label evenly spaced time positions so gaps do not bunch up labels.
                label_count = min(4, len(times), max(2, gw // 70))
                if visible_span <= timedelta(days=2):
                    time_format = "%H:%M"
                elif visible_span <= timedelta(days=45):
                    time_format = "%d/%m"
                else:
                    time_format = "%m/%Y"
                for index in range(label_count):
                    ratio = index / (label_count - 1)
                    time = times[0] + visible_span * ratio
                    self.chart_canvas.create_text(
                        pad_l + gw * ratio,
                        ch - 10,
                        text=time.strftime(time_format),
                        fill=self.color_text_muted,
                        font=(self.font_family, f_tick),
                    )

            last_x, last_y = points[-1]
            last_value = values[-1]
            self.chart_canvas.create_text(
                min(last_x - 5, cw - pad_r - 2),
                max(last_y - 12, pad_t + 5),
                text=format_instrument_value(instrument, last_value),
                fill=color,
                font=(self.font_family, f_val, "bold"),
                anchor="e",
            )
            self._chart_rendered_frame = frame
        except Exception as exc:
            log_message(f"Chart draw error: {exc}")

    def reset_chart_view(self, event=None):
        self._chart_view_start = 0.0
        self._chart_view_end = 1.0
        self._chart_pan_origin = None
        self._cancel_chart_redraw()
        self._draw_chart()
        return "break" if event is not None else None

    def _cancel_chart_redraw(self):
        if self._chart_frame_job is not None:
            self.root.after_cancel(self._chart_frame_job)
            self._chart_frame_job = None
        if self._chart_settle_job is not None:
            self.root.after_cancel(self._chart_settle_job)
            self._chart_settle_job = None

    def _queue_chart_redraw(self):
        if self._chart_frame_job is None:
            self._chart_frame_job = self.root.after(16, self._draw_chart_preview)
        if self._chart_settle_job is not None:
            self.root.after_cancel(self._chart_settle_job)
        self._chart_settle_job = self.root.after(120, self._finish_chart_redraw)

    def _draw_chart_preview(self):
        self._chart_frame_job = None
        self._draw_chart(preview=True)

    def _finish_chart_redraw(self):
        self._chart_settle_job = None
        self._cancel_chart_redraw()
        self._draw_chart()

    def _zoom_chart(self, zoom_in, anchor=0.5):
        point_count = len(self._chart_rows)
        if point_count < 3:
            return
        min_span = max(2 / max(point_count - 1, 1), 0.01)
        view = calculate_zoomed_range(
            self._chart_view_start,
            self._chart_view_end,
            zoom_in,
            anchor,
            min_span,
        )
        if view != (self._chart_view_start, self._chart_view_end):
            self._chart_view_start, self._chart_view_end = view
            self._queue_chart_redraw()

    def _on_chart_mousewheel(self, event, delta=None):
        delta = delta if delta is not None else getattr(event, "delta", 0)
        left, _top, right, _bottom = self._chart_plot_bounds
        width = max(right - left, 1)
        anchor = (getattr(event, "x", left + width / 2) - left) / width
        if delta:
            self._zoom_chart(delta > 0, anchor)
        return "break"

    def _on_chart_pan_start(self, event):
        self._chart_pan_origin = (
            event.x,
            self._chart_view_start,
            self._chart_view_end,
        )
        self.chart_canvas.configure(cursor="fleur")
        return "break"

    def _on_chart_pan_motion(self, event):
        if self._chart_pan_origin is None:
            return "break"
        origin_x, start, end = self._chart_pan_origin
        left, _top, right, _bottom = self._chart_plot_bounds
        width = max(right - left, 1)
        span = end - start
        shift = -((event.x - origin_x) / width) * span
        view = calculate_panned_range(start, end, shift)
        if view != (self._chart_view_start, self._chart_view_end):
            self._chart_view_start, self._chart_view_end = view
            self._queue_chart_redraw()
        return "break"

    def _on_chart_pan_end(self, _event=None):
        self._chart_pan_origin = None
        self.chart_canvas.configure(cursor="hand2")
        return "break"

    def _on_chart_canvas_configure(self, _event=None):
        if self._chart_rows:
            self._queue_chart_redraw()

    # --- İstatistik Sayfası ---
    def _build_stats_page(self):
        period_frame = tk.Frame(
            self.page_stats,
            bg=self.color_card,
            padx=SPACING["xs"],
            pady=SPACING["xs"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        period_frame.pack(fill="x", pady=(0, SPACING["xs"]))

        self.stats_period = tk.IntVar(value=7)

        tk.Label(
            period_frame,
            text="PİYASA İSTATİSTİKLERİ",
            bg=self.color_card,
            fg=self.color_text_dim,
            font=self.font_header,
        ).pack(side="left")
        self.stats_period_control = SegmentedControl(
            period_frame,
            self.stats_period,
            (("7G", 7), ("30G", 30), ("Tümü", 0)),
            command=self._update_stats,
        )
        self.stats_period_control.pack(side="right")

        stats_container = tk.Frame(self.page_stats, bg=self.bg_color)
        stats_container.pack(fill="both", expand=True)
        self.stats_canvas = tk.Canvas(
            stats_container,
            bg=self.bg_color,
            highlightthickness=0,
            bd=0,
        )
        self.stats_scrollbar = ttk.Scrollbar(
            stats_container,
            orient="vertical",
            command=self.stats_canvas.yview,
            style="Modern.Vertical.TScrollbar",
        )
        self.stats_canvas.configure(yscrollcommand=self.stats_scrollbar.set)
        self.stats_scrollbar.pack(side="right", fill="y")
        self.stats_canvas.pack(side="left", fill="both", expand=True)
        self.stats_frame = tk.Frame(self.stats_canvas, bg=self.bg_color)
        self._stats_window_id = self.stats_canvas.create_window(
            (0, 0), window=self.stats_frame, anchor="nw"
        )
        self.stats_frame.bind(
            "<Configure>",
            lambda _event: self.stats_canvas.configure(
                scrollregion=self.stats_canvas.bbox("all")
            ),
        )
        self.stats_canvas.bind(
            "<Configure>",
            lambda event: self.stats_canvas.itemconfigure(
                self._stats_window_id, width=event.width
            ),
        )
        self.stats_canvas.bind("<MouseWheel>", self._on_stats_mousewheel)

        self.stats_labels = {}
        self._rebuild_stats_sections()

    def _on_stats_mousewheel(self, event):
        self.stats_canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

    def _bind_stats_mousewheel(self, widget):
        widget.bind("<MouseWheel>", self._on_stats_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_stats_mousewheel(child)

    def _rebuild_stats_sections(self):
        if not hasattr(self, "stats_frame"):
            return
        for child in self.stats_frame.winfo_children():
            child.destroy()

        self.stats_labels = {}
        for instrument in self.watchlist:
            key = instrument["key"]
            section = tk.Frame(
                self.stats_frame,
                bg=self.color_card_alt,
                padx=SPACING["xs"],
                pady=6,
                highlightthickness=1,
                highlightbackground=self.color_border,
            )
            section.pack(fill="x", pady=SPACING["xxs"] // 2)

            tk.Label(
                section,
                text=instrument["label"],
                bg=self.color_card_alt,
                fg=instrument.get("color", self.color_accent),
                font=self.font_label,
            ).pack(anchor="w", pady=(0, SPACING["xxs"]))

            metrics = tk.Frame(section, bg=self.color_card_alt)
            metrics.pack(fill="x")
            for column in (0, 1):
                metrics.grid_columnconfigure(column, weight=1, uniform="stats")

            self.stats_labels[key] = {}
            for metric, text, grid_row, grid_column in (
                ("min", "Min: --", 0, 0),
                ("max", "Max: --", 0, 1),
                ("avg", "Ort: --", 1, 0),
                ("chg", "Değişim: --", 1, 1),
            ):
                label = tk.Label(
                    metrics,
                    text=text,
                    bg=self.color_card_alt,
                    fg=self.color_text_dim,
                    font=self.font_small,
                    anchor="w",
                )
                label.grid(row=grid_row, column=grid_column, sticky="ew", pady=1)
                self.stats_labels[key][metric] = label
            self._bind_stats_mousewheel(section)

        self.stats_canvas.yview_moveto(0)

    def _update_stats(self):
        try:
            days = self.stats_period.get()

            for instrument in self.watchlist:
                key = instrument["key"]
                labels = self.stats_labels.get(key)
                if not labels:
                    continue

                rows, _source, _interval = self._cached_history_series(
                    key,
                    days * 24 * 60 * 60 if days > 0 else 0,
                )
                values = [float(row[1]) for row in rows]
                if not values:
                    labels["min"].config(text="Min: --")
                    labels["max"].config(text="Max: --")
                    labels["avg"].config(text="Ort: --")
                    labels["chg"].config(text="Değişim: --", fg=self.color_text_muted)
                    continue

                mn, mx = min(values), max(values)
                avg = sum(values) / len(values)
                labels["min"].config(text=f"Min: {format_instrument_value(instrument, mn)}")
                labels["max"].config(text=f"Max: {format_instrument_value(instrument, mx)}")
                labels["avg"].config(text=f"Ort: {format_instrument_value(instrument, avg)}")

                first_value = values[0]
                last_value = values[-1]
                if first_value > 0:
                    chg = ((last_value - first_value) / first_value) * 100
                    sign = "+" if chg >= 0 else ""
                    color = self.color_success if chg >= 0 else self.color_danger
                    labels["chg"].config(text=f"Değişim: {sign}{chg:.1f}%", fg=color)
                else:
                    labels["chg"].config(text="Değişim: --", fg=self.color_text_muted)
        except Exception as e:
            log_message(f"Stats error: {e}")

    def toggle_autostart(self):
        self.asm.set_autostart(self.var_autostart.get())

    def toggle_topmost(self):
        enabled = self.var_topmost.get()
        self.root.attributes("-topmost", enabled)
        self._preferences["topmost"] = enabled
        self._save_preferences()

    def check_updates(self, *, parent=None):
        parent = parent or self.root
        if VERSION.endswith("-dev"):
            messagebox.showinfo("Güncelleme", "Bu yerel yapı kurulum paketiyle güncellenir.", parent=parent)
            return

        def show_result(result):
            available, version, url = result
            if not available:
                messagebox.showinfo("Güncelleme", "Uygulama güncel.", parent=parent)
            elif messagebox.askyesno("Güncelleme mevcut", f"v{version} indirilsin ve kurulum açılsın mı?", parent=parent):
                self._run_update_task(lambda: self.um.update_application(url), launch, parent)

        def launch(path):
            try:
                subprocess.Popen([path])
            except OSError as error:
                messagebox.showerror("Kurulum başlatılamadı", str(error), parent=parent)
                return
            self.kapat()

        self._run_update_task(self.um.check_for_updates, show_result, parent)

    def _run_update_task(self, task, on_success, parent):
        if getattr(self, "_update_task_running", False):
            return
        self._update_task_running = True
        result = SimpleQueue()

        def worker():
            try:
                result.put((task(), None))
            except Exception as error:
                result.put((None, error))

        def poll():
            try:
                value, error = result.get_nowait()
            except Empty:
                self.root.after(100, poll)
                return
            self._update_task_running = False
            if not parent.winfo_exists():
                return
            if error is not None:
                messagebox.showerror("Güncelleme tamamlanamadı", str(error), parent=parent)
            else:
                on_success(value)

        threading.Thread(target=worker, daemon=True, name="piyasa-update").start()
        self.root.after(100, poll)

    def save_last_data(self, data):
        temporary = None
        try:
            normalized = normalize_market_data(data)
            if not normalized:
                return
            filepath = getattr(self, "market_data_path", app_path("market_data.json"))
            directory = os.path.dirname(os.path.abspath(filepath))
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".market-data-", delete=False) as target:
                temporary = target.name
                json.dump(normalized, target, indent=4, ensure_ascii=False)
            os.replace(temporary, filepath)
        except Exception as e:
            log_message(f"Veri kaydetme hatası: {e}")
        finally:
            if temporary and os.path.exists(temporary):
                os.remove(temporary)

    def load_last_data(self):
        try:
            filepath = getattr(self, "market_data_path", app_path("market_data.json"))
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    return normalize_market_data(json.load(f))
        except Exception as e:
            log_message(f"Veri okuma hatası: {e}")
        return None

    @staticmethod
    def _extract_price(ticker):
        try:
            fast_info = getattr(ticker, "fast_info", None)
            if fast_info:
                for key in (
                    "last_price", "lastPrice",
                    "regular_market_price", "regularMarketPrice",
                ):
                    try:
                        val = fast_info.get(key) if hasattr(fast_info, "get") else getattr(fast_info, key, None)
                        price = positive_price(val)
                        if price:
                            return price
                    except:
                        pass
        except:
            pass

        try:
            info = ticker.info
            return positive_price(info.get("regularMarketPrice"))
        except:
            return 0

    def workspace_instruments(self):
        """Keep valuing positions independently of the visible watchlist."""
        instruments = {item["key"]: item for item in self.watchlist}
        manager = getattr(self, "tm", None)
        if manager is None:
            return list(instruments.values())
        positions = manager.get_positions()
        hidden = {key for key, position in positions.items()
                  if key not in instruments and position.get("quantity", 0) > 1e-9}
        if not hidden:
            return list(instruments.values())
        known = {item["key"]: item for item in default_watchlist()}
        known.update(getattr(getattr(self, "watchlist_manager", None), "archived", {}))
        unresolved = hidden - known.keys()
        if unresolved:
            for record in reversed(manager.transactions):
                if not isinstance(record, dict):
                    continue
                key = record.get("instrument_key")
                if key in unresolved and record.get("instrument_symbol"):
                    known[key] = normalize_instrument({
                        "key": key, "symbol": record["instrument_symbol"],
                        "label": record.get("instrument_label"), "currency": record.get("instrument_currency", "₺"),
                        "source": record.get("instrument_source"), "decimals": record.get("instrument_decimals", 2),
                    })
                    unresolved.remove(key)
                    if not unresolved:
                        break
        for key in positions:
            if key in hidden and key in known:
                instruments[key] = known[key]
        return list(instruments.values())

    def _quote_instruments(self):
        instruments = self.workspace_instruments()
        if (any(item.get("currency") == "$" or item.get("source") in ("metal_try", "silver_spot_try") for item in instruments)
                and not any(item["key"] == "dolar" for item in instruments)):
            instruments.append(next(item for item in default_watchlist() if item["key"] == "dolar"))
        return instruments

    def _required_yahoo_symbols(self, instruments=None):
        symbols = set()
        for instrument in self._quote_instruments() if instruments is None else instruments:
            source = instrument.get("source")
            if source not in SILVER_SPOT_SOURCES:
                symbols.add(instrument["symbol"])
            if source in ("metal_try", "silver_spot_try") or instrument.get("currency") == "$":
                symbols.add("TRY=X")
        return sorted(symbols)

    @staticmethod
    def _fetch_spot_silver_price(*, metadata=None):
        try:
            response = requests.get(
                SILVER_SPOT_API_URL,
                headers={"User-Agent": "Disa-Finans-Widget/1.0"},
                timeout=10
            )
            response.raise_for_status()
            payload = response.json()
            if (
                str(payload.get("symbol", "")).upper() != SILVER_SPOT_SYMBOL
                or str(payload.get("currency", "")).upper() != "USD"
            ):
                return 0
            price = safe_float(payload.get("price"), 0)
            if metadata is not None and 1 < price < 1000:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                metadata[SILVER_SPOT_SYMBOL] = observation("Gold API", now, price_at=payload.get("updatedAt"))
            return price if 1 < price < 1000 else 0
        except Exception as e:
            log_message(f"Spot gümüş verisi alınamadı: {e}")
            return 0

    def _watchlist_price_sources(self, instruments=None):
        return {
            instrument["key"]: instrument["symbol"]
            for instrument in (self._quote_instruments() if instruments is None else instruments)
        }

    @staticmethod
    def _history_provider_symbol(instrument):
        if instrument.get("source") in SILVER_SPOT_SOURCES:
            return SILVER_HISTORY_PROXY_SYMBOL
        return instrument.get("symbol")

    @staticmethod
    def _combine_history_with_fx(base_rows, fx_rows, interval="1d"):
        """Convert ounce/USD rows to gram/TRY using the latest known FX row."""
        parsed_fx = []
        for timestamp, value in fx_rows or []:
            parsed = parse_history_timestamp(timestamp)
            if parsed is not None:
                parsed_fx.append((parsed, float(value)))
        parsed_fx.sort(key=lambda item: item[0])
        if not parsed_fx:
            return []

        max_fx_age = (
            timedelta(minutes=15)
            if interval == "5m"
            else timedelta(days=4)
        )
        combined = []
        fx_index = 0
        latest_fx = None
        for timestamp, value in base_rows or []:
            parsed = parse_history_timestamp(timestamp)
            if parsed is None:
                continue
            while (
                fx_index < len(parsed_fx)
                and parsed_fx[fx_index][0] <= parsed
            ):
                latest_fx = parsed_fx[fx_index]
                fx_index += 1
            if latest_fx is None:
                continue
            if parsed - latest_fx[0] > max_fx_age:
                continue
            converted = (float(value) * latest_fx[1]) / TROY_OUNCE_GRAMS
            if converted > 0:
                combined.append((normalize_history_timestamp(parsed), converted))
        return combined

    @staticmethod
    def _history_rows_for_instrument(instrument, raw_series, interval="1d"):
        provider_symbol = PiyasaWidget._history_provider_symbol(instrument)
        base_rows = raw_series.get(provider_symbol, [])
        source = instrument.get("source")
        if source in ("metal_try", "silver_spot_try"):
            return PiyasaWidget._combine_history_with_fx(
                base_rows,
                raw_series.get("TRY=X", []),
                interval,
            )
        return list(base_rows)

    @staticmethod
    def _history_cache_source(instrument):
        if instrument.get("source") in SILVER_SPOT_SOURCES:
            return SILVER_HISTORY_PROXY_SYMBOL
        return instrument.get("symbol") or ""

    @staticmethod
    def _extract_history_rows(frame):
        if frame is None or getattr(frame, "empty", True):
            return []
        try:
            close = frame["Close"].dropna()
        except Exception:
            return []
        rows = []
        for timestamp, value in close.items():
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value > 0 and value == value:
                rows.append((normalize_history_timestamp(timestamp), value))
        return rows

    def _download_yahoo_history(self, symbol, interval):
        kwargs = {
            "interval": interval,
            "auto_adjust": True,
            "actions": False,
            "repair": True,
            "timeout": 15,
        }
        if interval == "5m":
            end = datetime.now().astimezone()
            kwargs.update(start=end - timedelta(days=31), end=end)
        else:
            kwargs["period"] = "max"
        frame = yf.Ticker(symbol).history(**kwargs)
        return self._extract_history_rows(frame)

    def _sync_history_profile(self, interval, max_age):
        due_instruments = []
        for instrument in self.workspace_instruments():
            source_symbol = self._history_cache_source(instrument)
            if self.history_db.history_sync_due(
                instrument["key"],
                source_symbol,
                interval,
                max_age,
            ):
                due_instruments.append(instrument)
        if not due_instruments:
            return 0

        required_symbols = {
            self._history_provider_symbol(instrument)
            for instrument in due_instruments
            if self._history_provider_symbol(instrument)
        }
        if any(
            instrument.get("source") in ("metal_try", "silver_spot_try")
            for instrument in due_instruments
        ):
            required_symbols.add("TRY=X")

        raw_series = {}
        for symbol in sorted(required_symbols):
            if self._stop_event.is_set():
                return 0
            try:
                raw_series[symbol] = self._download_yahoo_history(
                    symbol, interval
                )
            except Exception as exc:
                log_message(
                    f"{symbol} {interval} geçmiş verisi alınamadı: {exc}"
                )
                raw_series[symbol] = []

        inserted = 0
        for instrument in due_instruments:
            rows = self._history_rows_for_instrument(
                instrument, raw_series, interval
            )
            if not rows:
                continue
            source_symbol = self._history_cache_source(instrument)
            inserted += self.history_db.insert_history_rows(
                instrument,
                rows,
                interval,
                source_symbol=source_symbol,
            )
            self.history_db.mark_history_synced(
                instrument["key"],
                source_symbol,
                interval,
            )
        return inserted

    def sync_historical_data(self):
        """Fill missing intraday/daily history without blocking the Tk thread."""
        if not self._history_fetch_lock.acquire(blocking=False):
            return 0
        inserted = 0
        try:
            inserted = self._sync_history_profile("5m", timedelta(minutes=30))
            if not self._stop_event.is_set():
                inserted += self._sync_history_profile("1d", timedelta(hours=24))
            if inserted and hasattr(self, "root"):
                try:
                    self.root.after(0, self._refresh_history_views)
                except tk.TclError:
                    pass
        except Exception as exc:
            log_message(f"Geçmiş veri eşitleme hatası: {exc}")
        finally:
            self._history_fetch_lock.release()
        return inserted

    def _history_backfill_worker(self):
        while not self._stop_event.is_set():
            self._history_resync_requested = False
            self.sync_historical_data()
            if not self._history_resync_requested:
                break

    def request_history_backfill(self):
        active = getattr(self, "_history_thread", None)
        if active is not None and active.is_alive():
            self._history_resync_requested = True
            return active
        self._history_resync_requested = False
        self._history_thread = threading.Thread(
            target=self._history_backfill_worker,
            name="market-history-backfill",
            daemon=True,
        )
        self._history_thread.start()
        return self._history_thread

    def _refresh_history_views(self):
        if self._last_prices:
            self._update_price_row_visuals(self._last_prices)
        if getattr(self, "current_page", 0) == 1:
            self._update_chart()
        self._refresh_workspace(history_changed=True)

    def _compatible_cached_data(self, data, instruments=None):
        data = normalize_market_data(data)
        if not data:
            return None
        prices = dict(data.get("prices", {}))
        sources = dict(data.get("sources", {}))
        available = self._quote_instruments() if instruments is None else instruments
        for instrument in available:
            key = instrument["key"]
            source = sources.get(key)
            if ((source and source != instrument["symbol"])
                    or (instrument.get("source") in SILVER_SPOT_SOURCES and source != SILVER_SPOT_SYMBOL)):
                prices.pop(key, None)
                sources.pop(key, None)
        return market_data_for_save(
            prices,
            timestamp=data.get("timestamp"),
            sources=sources,
            quotes={key: {**meta, "state": "cached", "market_state": ""}
                    for key, meta in data["quotes"].items()},
        )

    def _fetch_raw_prices(self, symbols, *, metadata=None):
        if not symbols:
            return {}
        tickers = yf.Tickers(" ".join(symbols))
        ticker_map = getattr(tickers, "tickers", {})
        prices = {}
        for symbol in symbols:
            ticker = None
            try:
                ticker = ticker_map.get(symbol) if hasattr(ticker_map, "get") else ticker_map[symbol]
            except:
                ticker = None
            if ticker is None:
                continue
            provider_data = {}
            try:
                provider_data = ticker.get_history_metadata() if metadata is not None else {}
            except Exception:
                # Metadata availability must not suppress the price fallback.
                provider_data = {}
            if not isinstance(provider_data, dict):
                provider_data = {}
            provider_price = positive_price(provider_data.get("regularMarketPrice"))
            price = provider_price or self._extract_price(ticker)
            if price:
                prices[symbol] = price
                if metadata is not None:
                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    metadata[symbol] = observation(
                        "Yahoo Finance", now,
                        price_at=provider_data.get("regularMarketTime") if provider_price else None,
                        market_state=session_from_metadata(provider_data, now),
                    )
        return prices

    def validate_yahoo_symbol(self, symbol):
        symbol = symbol.strip().upper()
        prices = self._fetch_raw_prices([symbol])
        return prices.get(symbol, 0)

    def _resolve_watchlist_prices(self, raw_prices, last_prices=None, *, instruments=None):
        last_prices = last_prices or {}
        prices = {}
        fresh_keys = set()
        dolar = raw_prices.get("TRY=X") or last_prices.get("dolar")

        for instrument in self._quote_instruments() if instruments is None else instruments:
            key = instrument["key"]
            symbol = instrument["symbol"]
            value = 0

            if instrument.get("source") in ("metal_try", "silver_spot_try"):
                base_price = raw_prices.get(symbol)
                if base_price and dolar:
                    value = (base_price * dolar) / TROY_OUNCE_GRAMS
            else:
                value = raw_prices.get(symbol, 0)

            if value and value > 0:
                prices[key] = float(value)
                fresh_keys.add(key)
            elif key in last_prices:
                prices[key] = last_prices[key]

        return prices, fresh_keys

    def request_data_refresh(self):
        threading.Thread(target=self.veri_getir, daemon=True).start()

    def veri_getir(self):
        if not self._fetch_lock.acquire(blocking=False):
            return
        try:
            instruments = self._quote_instruments()
            last_data = self._compatible_cached_data(self.load_last_data(), instruments) or {}
            last_prices = last_data.get("prices", {})
            last_quotes = last_data.get("quotes", {})
            raw_quotes = {}
            raw_prices = {}
            if any(item.get("source") in SILVER_SPOT_SOURCES for item in instruments):
                spot_silver = self._fetch_spot_silver_price(metadata=raw_quotes)
                if spot_silver:
                    raw_prices[SILVER_SPOT_SYMBOL] = spot_silver
                    raw_quotes.setdefault(SILVER_SPOT_SYMBOL, observation("Gold API", datetime.now(timezone.utc).isoformat(timespec="seconds")))
            try:
                raw_prices.update(self._fetch_raw_prices(self._required_yahoo_symbols(instruments), metadata=raw_quotes))
            except Exception as error:
                log_message(f"Yahoo Finance verisi alınamadı: {error}")
            prices, computed = self._resolve_watchlist_prices(raw_prices, last_prices, instruments=instruments)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            by_key = {item["key"]: item for item in instruments}
            quotes = merge_observations(prices, computed, by_key, raw_quotes, last_quotes, now)
            for key in computed:
                old_quote = last_quotes.get(key, {})
                current_quote = quotes[key]
                old_time = old_quote.get("price_at")
                price_time = current_quote.get("price_at")
                if old_time and price_time and price_time < old_time:
                    prices[key] = last_prices[key]
                    quotes[key] = old_quote
                elif not price_time and prices[key] == last_prices.get(key):
                    current_quote["price_at"] = old_time or ""
                    current_quote["observed_at"] = old_quote.get("observed_at") or current_quote["observed_at"]

            received = {key for key in computed if quotes[key]["state"] == "received"}
            market_data = market_data_for_save(
                prices, timestamp=None if computed else last_data.get("timestamp"),
                sources=self._watchlist_price_sources(instruments), quotes=quotes,
            )
            if not computed:
                market_data["timestamp"] = last_data.get("timestamp", "")
            samples = {}
            for key in received:
                quote = quotes[key]
                stamp = quote["price_at"]
                if not stamp and quote["market_state"] == "CLOSED":
                    continue
                derived = by_key[key].get("source") in ("metal_try", "silver_spot_try")
                if derived:
                    # The derived price can change when either component trades.
                    base_stamp = raw_quotes.get(by_key[key]["symbol"], {}).get("price_at")
                    fx_stamp = raw_quotes.get("TRY=X", {}).get("price_at")
                    stamp = max(base_stamp, fx_stamp) if base_stamp and fx_stamp else ""
                samples.setdefault((stamp or now, not stamp), {})[key] = prices[key]
            for (stamp, changes_only), batch in samples.items():
                self.history_db.insert_prices(batch, instruments, timestamp=stamp, changes_only=changes_only)
            if computed:
                self.save_last_data(market_data)
            self.root.after(0, lambda: self._publish_quotes(market_data, received))
        except Exception as error:
            log_message(f"Fiyat verisi yenilenemedi: {error}")
            cached = self._compatible_cached_data(self.load_last_data())
            self.root.after(0, lambda: self._publish_quotes(cached or {}, set()))
        finally:
            self._fetch_lock.release()

    def _publish_quotes(self, data, received):
        visible = {item["key"] for item in self.watchlist}
        fresh_count = len(visible & received)
        if fresh_count and fresh_count == len(visible):
            status, color = "Veri alındı", self.color_success
        elif fresh_count:
            status, color = f"{fresh_count}/{len(visible)} fiyat alındı", self.color_text_dim
        elif data.get("prices"):
            status, color = "Son kayıtlar", self.color_text_dim
        else:
            status, color = "Fiyat alınamadı", self.color_danger
        self.var_market_status.set("●")
        self.lbl_market_status.config(fg=color)
        self.var_market_label.set(status)
        self.guncelle_arayuz(data)

    def guncelle_arayuz(self, data_or_ons=None, gram_g=None, gram_a=None):
        if isinstance(data_or_ons, dict):
            data = normalize_market_data(data_or_ons)
        else:
            prices = {}
            if data_or_ons is not None:
                prices["gumus_ons"] = data_or_ons
            if gram_g is not None:
                prices["gumus_tl"] = gram_g
            if gram_a is not None:
                prices["altin_tl"] = gram_a
            data = market_data_for_save(prices)

        if not data:
            return

        available_keys = {item["key"] for item in self._quote_instruments()}
        prices = {key: price for key, price in data["prices"].items() if key in available_keys}
        self._last_prices = dict(prices)
        self._last_quotes = data["quotes"]
        for instrument in self.watchlist:
            var = getattr(self, "price_vars", {}).get(instrument["key"])
            if var:
                var.set(format_instrument_value(instrument, prices.get(instrument["key"])))

        self._update_price_row_visuals(prices)
        if hasattr(self, "_update_widget_quote_labels"):
            self._update_widget_quote_labels()

        if prices.get("dolar"):
            self.last_dolar_rate = prices["dolar"]

        self.var_time.set(data.get("timestamp") or "—")

        self._update_portfolio_view()

    def _update_portfolio_view(self, *, ledger_changed=False):
        summary = self.tm.get_portfolio_summary(self._last_prices, self.workspace_instruments())
        self.rebuild_portfolio_breakdown(summary)
        self._refresh_workspace(summary=summary, ledger_changed=ledger_changed)
        rows = summary["rows"]
        priced_positions = sum(row["has_price"] for row in rows)
        coverage = getattr(self, "var_portfolio_coverage", None)
        load_error = getattr(self.tm, "load_error", None)
        if coverage is not None:
            coverage.set(f"{priced_positions}/{len(rows)} fiyatlandı" if priced_positions < len(rows) else "")
        if isinstance(load_error, str) and load_error:
            self.var_portfolio.set("Kayıt okunamadı")
            self.var_profit.set("Ayarlar → Yedeği geri yükle")
            self.lbl_profit.config(fg=self.color_danger, bg=self.color_card_alt)
            return
        if not rows or not priced_positions:
            self.var_portfolio.set("Fiyat yok" if rows else "₺0")
            self.var_profit.set("Fiyat bekleniyor" if rows else "%0.0 (₺0)")
            self.lbl_profit.config(fg=self.color_text_dim, bg=self.color_card_alt)
            return

        self.var_portfolio.set(self._format_tl(summary["total_value_tl"]))
        profit_tl = summary["profit_tl"]
        profit_pct = summary["profit_pct"]
        sign = "+" if profit_tl >= 0 else "−"
        change = f"{sign}%{abs(profit_pct):.1f} ({sign}{self._format_tl(abs(profit_tl))})"
        self.var_profit.set(change)
        color = self.color_success if profit_tl >= 0 else self.color_danger
        bg = self.color_success_bg if profit_tl >= 0 else self.color_danger_bg
        self.lbl_profit.config(fg=color, bg=bg)

    def _open_workspace_from_widget(self):
        if self.current_page == 1:
            return self.open_workspace("markets", self.chart_var.get(), period=self.chart_period.get())
        return self.open_workspace()

    def open_workspace(self, view="overview", asset_key=None, *, period=None):
        if self.workspace is None or not self.workspace.winfo_exists():
            self.workspace = DesktopWorkspace(self)
        self.workspace.present(view, asset_key, period=period)
        return self.workspace

    def _refresh_workspace(self, **changes):
        workspace = getattr(self, "workspace", None)
        if workspace is not None and workspace.winfo_exists():
            workspace.refresh(**changes)

    @staticmethod
    def workspace_chart_helpers():
        return {"parse_timestamp": parse_history_timestamp, "downsample": downsample_history_rows}

    def workspace_watch_row(self, key):
        if key not in self.price_vars:
            return None
        return (
            self.price_vars[key].get(), self.price_change_vars[key].get(),
            self.price_change_labels[key].cget("fg"),
            getattr(self.price_spark_canvases[key], "_sparkline_payload", None),
        )

    def workspace_quote_status(self, key, compact=False):
        return quote_status(
            getattr(self, "_last_prices", {}).get(key),
            getattr(self, "_last_quotes", {}).get(key),
            compact=compact,
        )

    def workspace_transaction_rows(self):
        instruments = {item["key"]: item for item in portfolio_instruments(self.workspace_instruments())}
        return [
            PortfolioManagerDialog.transaction_row(self.tm, instruments, index, raw)
            for index, raw in enumerate(self.tm.transactions)
        ]

    def _on_transactions_changed(self):
        self._update_portfolio_view(ledger_changed=True)

    def _on_trade_saved(self):
        self._on_transactions_changed()
        workspace = getattr(self, "workspace", None)
        if workspace is not None and workspace.winfo_exists():
            workspace.show_notice("İşlem kaydedildi")
        self.var_trade_feedback.set("Kaydedildi")
        previous = getattr(self, "_trade_feedback_job", None)
        if previous is not None:
            self.root.after_cancel(previous)
        self._trade_feedback_job = self.root.after(3000, self._clear_trade_feedback)

    def _clear_trade_feedback(self):
        self._trade_feedback_job = None
        self.var_trade_feedback.set("")

    def _cancel_trade_feedback(self, event):
        job = getattr(self, "_trade_feedback_job", None)
        if event.widget is self.root and job is not None:
            self.root.after_cancel(job)
            self._trade_feedback_job = None

    def open_add_transaction(self, *, parent=None, instrument_key=None, edit_index=None):
        parent = parent or self.root
        if self.tm.load_error:
            if messagebox.askyesno("İşlemler salt okunur", f"{self.tm.load_error}\nYedekten geri yüklemek ister misiniz?", parent=parent):
                self.restore_transactions(parent=parent)
            return None
        dialog = getattr(self, "portfolio_dialog", None)
        new_dialog = dialog is None or not dialog.winfo_exists()
        if new_dialog:
            dialog = PortfolioManagerDialog(
                parent, self.tm, self._on_trade_saved,
                getattr(self, "last_dolar_rate", None), self.workspace_instruments(), quick=True,
            )
            self.portfolio_dialog = dialog
        selected = dialog.selected_instrument() or {}
        changing_asset = instrument_key in dialog.instrument_by_key and selected.get("key") != instrument_key
        destination = int(edit_index) if edit_index is not None else None
        changing_mode = dialog.edit_index != destination
        if not new_dialog and (changing_mode or changing_asset):
            if not dialog.confirm_discard_draft():
                dialog.present()
                return dialog
            if destination is None:
                dialog.cancel_edit()
        if destination is not None and (new_dialog or changing_mode):
            dialog.start_edit(edit_index)
        elif destination is None and changing_asset:
            dialog.var_instrument.set(dialog.instrument_by_key[instrument_key]["label"])
            dialog.update_amount_label()
            dialog.mark_current_draft()
        dialog.present()
        return dialog

    def delete_saved_transaction(self, source_index, *, parent):
        index = int(source_index)
        dialog = getattr(self, "portfolio_dialog", None)
        dialog_open = dialog is not None and dialog.winfo_exists()
        if dialog_open and dialog.edit_index == index and not dialog.confirm_discard_draft():
            return
        instruments = {item["key"]: item for item in self.workspace_instruments()}
        if PortfolioManagerDialog.confirm_transaction_deletion(parent, self.tm, instruments, index):
            if dialog_open:
                dialog.refresh_after_deletion(index)
            self._on_transactions_changed()

    def import_transactions(self, *, parent=None):
        parent = parent or self.root
        filename = filedialog.askopenfilename(parent=parent, title="İçe Aktarılacak Dosyayı Seç", filetypes=[("JSON Files", "*.json")])
        if not filename:
            return
        try:
            transactions, duplicates = self.tm.preview_import(self.tm.read_import(filename))
            if not transactions:
                messagebox.showinfo("İçe aktar", f"Yeni kayıt yok. Atlanan tekrar: {duplicates}.", parent=parent)
                return
            preview = f"Eklenecek: {len(transactions)}\nAtlanacak olası tekrar: {duplicates}"
            if not messagebox.askyesno("İçe aktarma önizlemesi", preview, parent=parent):
                return
            self.tm.save_many(transactions)
        except (ValueError, OSError) as error:
            messagebox.showerror("İçe aktarma hatası", str(error), parent=parent)
            return
        dialog = getattr(self, "portfolio_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.load_list()
            dialog.update_amount_label()
        self._on_transactions_changed()
        messagebox.showinfo("Başarılı", f"{len(transactions)} işlem içe aktarıldı.", parent=parent)

    def export_transactions(self, *, parent=None):
        parent = parent or self.root
        target = filedialog.asksaveasfilename(parent=parent, title="İşlem yedeğini dışa aktar",
                                            initialfile=f"Piyasa-islemler-{datetime.now():%Y%m%d}.json",
                                            defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not target:
            return
        try:
            self.tm.export_backup(target)
        except (ValueError, OSError) as error:
            messagebox.showerror("Yedek kaydedilemedi", str(error), parent=parent)
            return
        messagebox.showinfo("Yedek kaydedildi", os.path.basename(target), parent=parent)

    def restore_transactions(self, *, parent=None):
        parent = parent or self.root
        source = filedialog.askopenfilename(parent=parent, title="İşlem yedeğini seç",
                                           filetypes=[("JSON", "*.json")])
        if not source:
            return
        dialog = getattr(self, "portfolio_dialog", None)
        dialog_open = dialog is not None and dialog.winfo_exists()
        if dialog_open and not dialog.confirm_discard_draft():
            return
        try:
            records = self.tm.read_import(source)
            if not messagebox.askyesno("Yedeği geri yükle", f"Seçilen yedek {len(records)} işlem içeriyor.\n"
                                       "Mevcut dosya ayrı bir kurtarma kopyasına alınacak.", parent=parent):
                return
            recovery = self.tm.restore_backup(source)
        except (ValueError, OSError) as error:
            messagebox.showerror("Yedek yüklenemedi", str(error), parent=parent)
            return
        if dialog_open:
            dialog.destroy()
        self._on_transactions_changed()
        text = f"{len(records)} işlem geri yüklendi."
        if recovery:
            text += f"\nÖnceki dosya: {os.path.basename(recovery)}"
        messagebox.showinfo("Yedek geri yüklendi", text, parent=parent)

    def open_watchlist_settings(self, *, parent=None):
        return WatchlistDialog(parent or self.root, self.watchlist_manager, self.on_watchlist_changed, self.validate_yahoo_symbol)

    def on_watchlist_changed(self):
        self.watchlist = self.watchlist_manager.instruments
        self.rebuild_price_rows()
        self._rebuild_chart_symbol_buttons()
        self._rebuild_stats_sections()

        last_data = self._compatible_cached_data(self.load_last_data())
        if last_data:
            self.guncelle_arayuz(last_data)
        self._update_portfolio_view(ledger_changed=True)
        self.request_data_refresh()
        self.request_history_backfill()

    def open_settings(self, event=None):
        anchor = getattr(self, "footer_buttons", {}).get("settings")
        if anchor is not None:
            self.settings_menu.toggle_for_widget(anchor)

    def make_toolwindow(self):
        # Windows API kullanarak pencereyi Taskbar'dan ve Alt-Tab'dan gizleme
        # GWL_EXSTYLE = -20
        # WS_EX_TOOLWINDOW = 0x00000080
        # WS_EX_APPWINDOW = 0x00040000

        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        style = style | 0x00000080 # WS_EX_TOOLWINDOW ekle
        style = style & ~0x00040000 # WS_EX_APPWINDOW çıkar (bazı durumlarda default olabilir)
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)

        # Değişikliğin hemen uygulanması için
        self.root.withdraw()
        self.root.after(10, self.root.deiconify)

    def veri_dongusu(self):
        next_history_sync = 0.0
        while not self._stop_event.is_set():
            self.veri_getir()
            now = time.monotonic()
            if now >= next_history_sync and not self._stop_event.is_set():
                self.request_history_backfill()
                next_history_sync = now + (30 * 60)
            self._stop_event.wait(self.refresh_rate)


    # --- Yeniden Boyutlandırma Mantığı ---
    def start_resize(self, event):
        self._is_resizing = True
        self.resize_start_x = event.x_root
        self.resize_start_y = event.y_root
        self.start_width = self.root.winfo_width()
        self.start_height = self.root.winfo_height()

    def do_resize(self, event):
        deltax = event.x_root - self.resize_start_x
        deltay = event.y_root - self.resize_start_y

        new_width = max(300, self.start_width + deltax)
        new_height = max(360, self.start_height + deltay)

        self.root.geometry(f"{new_width}x{new_height}")

    def _on_resize_end(self, event):
        """Resize grip bırakıldığında çağrılır."""
        self._is_resizing = False
        if self._resize_after_id is not None:
            self.root.after_cancel(self._resize_after_id)
        self._apply_font_scale()
        self.root.after_idle(self._save_widget_geometry)

    def _on_button_release(self, event):
        """Genel buton bırakma - resize flag'ini temizle."""
        self._is_resizing = False
        if self._window_move_origin is not None:
            self.root.after_idle(self._save_widget_geometry, self._window_move_origin[2:])
        self._window_move_origin = None

    def on_resize(self, event):
        if event.widget == self.root:
            w = event.width
            if abs(w - self.last_width) > 20:
                self.last_width = w
                # Debounce: Sürekli sürükleme sırasında font güncelleme yapma,
                # sadece kullanıcı durunca (150ms sonra) güncelle
                if self._resize_after_id is not None:
                    self.root.after_cancel(self._resize_after_id)
                self._resize_after_id = self.root.after(150, self._apply_font_scale)

    def _apply_font_scale(self):
        """Fontları mevcut pencere genişliğine göre ölçekle (debounced)."""
        self._resize_after_id = None
        w = self.root.winfo_width()
        scale = max(1.0, w / 320.0)
        scale = min(1.5, scale)  # Çok büyümesin

        self.font_header.config(size=int(self.base_fonts["header"] * scale))
        self.font_label.config(size=int(self.base_fonts["label"] * scale))
        self.font_value.config(size=int(self.base_fonts["value"] * scale))
        self.font_portfolio.config(size=int(self.base_fonts["portfolio"] * scale))
        self.font_profit.config(size=int(self.base_fonts["profit"] * scale))
        self.font_market_status.config(size=int(self.base_fonts["market_status"] * scale))
        self.font_icon.config(size=int(self.base_fonts["icon"] * scale))
        self.font_small.config(size=int(self.base_fonts["small"] * scale))
        self.font_badge.config(size=int(self.base_fonts["badge"] * scale))

    # --- Sürükleme Mantığı ---
    def start_move(self, event):
        self._window_move_origin = None
        widget = event.widget
        while widget is not None and widget != self.root:
            if widget == self.grip or isinstance(widget, (
                tk.Canvas, tk.Button, tk.Entry, tk.Radiobutton, tk.Checkbutton,
                ttk.Combobox, ttk.Scrollbar, ttk.Treeview,
            )):
                return
            widget = widget.master
        self._window_move_origin = (event.x_root, event.y_root,
                                    self.root.winfo_x(), self.root.winfo_y())

    def do_move(self, event):
        origin = self._window_move_origin
        if origin is None or self._is_resizing or self._row_press_key is not None:
            return
        pointer_x, pointer_y, window_x, window_y = origin
        dx, dy = event.x_root - pointer_x, event.y_root - pointer_y
        if abs(dx) <= 4 and abs(dy) <= 4:
            return
        # The leading plus selects literal offsets even on left/upper monitors.
        self.root.geometry(f"+{window_x + dx}+{window_y + dy}")

    def show_menu(self, event):
        self.menu.post(event.x_root, event.y_root)

    def kapat(self):
        self._stop_event.set()
        self.root.destroy()
        sys.exit()

    def run(self):
        self.root.mainloop()


def main():
    PiyasaWidget().run()
