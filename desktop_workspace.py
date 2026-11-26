"""The optional native detail window for the Piyasa desktop widget."""

import ctypes
import sys
import tkinter as tk
from collections import Counter
from ctypes import wintypes
from datetime import datetime
from tkinter import ttk

from PIL import ImageTk

from rendering import render_icon
from theme import THEME, font, turkish_number
from ui_components import HoverButton, ModernEntry, SearchEntry, TransactionTable
from workspace_chart import DAY, PERIOD_LABELS, WorkspaceChart


def money(value, decimals=2):
    if value is None:
        return "—"
    number = turkish_number(abs(value), decimals)
    return f"{'−' if value < 0 else ''}₺{number}"


def signed_money(value):
    return f"+{money(value)}" if value is not None and value > 0 else money(value)


def quantity_text(value):
    return f"{value:,.6f}".rstrip("0").rstrip(".").translate(str.maketrans(",.", ".,"))


class DesktopWorkspace(tk.Toplevel):
    SCREENS = {
        "overview": "Piyasa özeti",
        "markets": "Grafikler",
        "portfolio": "Portföy",
        "trades": "İşlemler",
        "settings": "Ayarlar",
    }

    def __init__(self, app):
        super().__init__(app.root)
        self.withdraw()
        self.app = app
        self.title("Piyasa · Detaylar")
        self.configure(bg=THEME["background"])
        width = min(1130, self.winfo_screenwidth() - 64)
        height = min(800, self.winfo_screenheight() - 90)
        self.geometry(f"{width}x{height}")
        self.minsize(min(760, width), min(540, height))

        self.view = None
        self.selected_key = None
        self.chart_period = 30 * DAY
        self.pages = {}
        self.charts = {}
        self.holdings = {}
        self.metrics = {}
        self.watch_rows = {}
        self.watch_keys = None
        self.selector_options = {}
        self.summary = self.app.tm.get_portfolio_summary(self.app._last_prices, self.app.workspace_instruments())
        self._ledger_rows = None
        self._trade_sort = ("date", 2)
        self._notice = ""
        self._notice_job = None

        rail = tk.Frame(self, bg=THEME["surface"], width=171, highlightthickness=0)
        rail.grid(row=0, column=0, sticky="ns")
        rail.grid_propagate(False)
        rail.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        brand = tk.Frame(rail, bg=THEME["surface"])
        brand.grid(row=0, column=0, sticky="ew", padx=15, pady=(24, 24))
        self.brand_icon = ImageTk.PhotoImage(render_icon("chart", 25, THEME["primary_ink"]), master=brand)
        mark = tk.Label(brand, image=self.brand_icon, bg=THEME["primary"], bd=0, highlightthickness=0)
        mark.pack(side="left", ipadx=4, ipady=4)
        self._label(brand, "Piyasa", bg=THEME["surface"], role="section", weight="bold").pack(side="left", padx=(9, 0))
        self.navigation = {}
        for index, (view, label) in enumerate(self.SCREENS.items(), 1):
            button = self._button(rail, "Genel bakış" if view == "overview" else label, lambda key=view: self.show_view(key), anchor="w")
            button.grid(row=index, column=0, sticky="ew", padx=11, pady=2)
            self.navigation[view] = button
        rail.grid_rowconfigure(len(self.SCREENS) + 1, weight=1)
        self._button(rail, "Widget’a dön", self.destroy).grid(row=len(self.SCREENS) + 2, column=0, sticky="ew", padx=11, pady=14)

        workspace = tk.Frame(self, bg=THEME["background"], padx=22, pady=17)
        workspace.grid(row=0, column=1, sticky="nsew")
        workspace.grid_columnconfigure(0, weight=1)
        workspace.grid_rowconfigure(1, weight=1)
        header = tk.Frame(workspace, bg=THEME["background"])
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        heading = tk.Frame(header, bg=THEME["background"])
        heading.pack(side="left", fill="x", expand=True)
        self.title_label = self._label(heading, "", role=22, weight="bold")
        self.title_label.pack(anchor="w")
        self.status_label = self._label(heading, "", fg=THEME["text_secondary"], role="small")
        self.status_label.pack(anchor="w", pady=(3, 0))
        self.status_watches = [
            (variable, variable.trace_add("write", self._update_status))
            for variable in (self.app.var_market_label, self.app.var_time)
        ]
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._button(header, "+  İşlem ekle", self.open_trade, primary=True).pack(side="right")

        self.body = tk.Frame(workspace, bg=THEME["background"])
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(0, weight=1)
        self._configure_holdings_style()

    def _label(self, parent, text, *, bg=None, fg=None, role="body", weight="normal", anchor="w"):
        return tk.Label(
            parent, text=text, bg=bg or parent.cget("bg"), fg=fg or THEME["text_primary"],
            font=font(parent, role, weight), anchor=anchor, highlightthickness=0, bd=0,
        )

    def _button(self, parent, text, command, *, primary=False, anchor="center"):
        return HoverButton(
            parent, text=text, command=command, anchor=anchor,
            bg=THEME["primary"] if primary else THEME["surface_alt"],
            hover_bg=THEME["primary_hover"] if primary else THEME["surface_hover"],
            pressed_bg=THEME["primary_pressed"] if primary else THEME["surface_pressed"],
            fg=THEME["primary_ink"] if primary else THEME["text_primary"], padx=12, pady=8,
        )

    def _panel(self, parent):
        return tk.Frame(parent, bg=THEME["surface"], highlightthickness=1, highlightbackground=THEME["border"], bd=0)

    def present(self, view, asset_key=None, period=None):
        self.show_view(view, asset_key, period)
        self.deiconify()
        self.update_idletasks()
        self._style_titlebar()
        self.lift()
        self.focus_set()

    def _style_titlebar(self):
        if sys.platform != "win32":
            return
        try:
            get_ancestor = ctypes.windll.user32.GetAncestor
            get_ancestor.argtypes = (wintypes.HWND, wintypes.UINT)
            get_ancestor.restype = wintypes.HWND
            hwnd = get_ancestor(self.winfo_id(), 2)
            set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            set_attribute.argtypes = (wintypes.HWND, wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD)
            dark = wintypes.BOOL(True)
            set_attribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
            for attribute, color in ((35, THEME["surface"]), (36, THEME["text_primary"])):
                red, green, blue = bytes.fromhex(color.lstrip("#"))
                color_ref = wintypes.DWORD(red | (green << 8) | (blue << 16))
                set_attribute(hwnd, attribute, ctypes.byref(color_ref), ctypes.sizeof(color_ref))
        except (AttributeError, OSError):
            pass

    def _update_status(self, *_args):
        if self.app.tm.load_error:
            self.status_label.config(text="İşlem kayıtları okunamadı · Ayarlar’dan yedeği geri yükle", fg=THEME["negative"])
            return
        if self._notice:
            self.status_label.config(text=self._notice, fg=THEME["positive"])
            return
        status = self.app.var_market_label.get()
        time = self.app.var_time.get()
        self.status_label.config(
            text=f"{status}   ·   {time}" if time and time != "..." else status,
            fg=THEME["text_secondary"],
        )

    def show_notice(self, text):
        if self._notice_job is not None:
            self.after_cancel(self._notice_job)
        self._notice = text
        self._update_status()
        self._notice_job = self.after(4500, self._clear_notice)

    def _clear_notice(self):
        self._notice_job = None
        self._notice = ""
        self._update_status()

    def _on_destroy(self, event):
        if event.widget is self:
            if self._notice_job is not None:
                self.after_cancel(self._notice_job)
            for variable, trace in self.status_watches:
                variable.trace_remove("write", trace)

    def show_view(self, view, asset_key=None, period=None):
        if asset_key is not None:
            self.selected_key = asset_key
        if period in PERIOD_LABELS:
            self.chart_period = period
        if view != self.view:
            if self.view is not None:
                self.pages[self.view].grid_remove()
            self.view = view
            if view not in self.pages:
                page = tk.Frame(self.body, bg=THEME["background"])
                self.pages[view] = page
                getattr(self, f"_build_{view}")(page)
            self.pages[view].grid(row=0, column=0, sticky="nsew")
            self.title_label.config(text=self.SCREENS[view])
            for key, button in self.navigation.items():
                selected = key == view
                button.configure(
                    bg=THEME["primary_soft"] if selected else THEME["surface"],
                    fg=THEME["primary"] if selected else THEME["text_secondary"],
                    font=font(button, "body", "bold" if selected else "normal"),
                )
        self.refresh(history_changed=True)

    def _build_overview(self, page):
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1, minsize=218)
        page.grid_rowconfigure(2, weight=2, minsize=105)
        self._build_metrics(page, "overview").grid(row=0, column=0, sticky="ew", pady=(0, 13))
        middle = tk.Frame(page, bg=THEME["background"])
        middle.grid(row=1, column=0, sticky="nsew", pady=(0, 13))
        middle.grid_rowconfigure(0, weight=1)
        middle.grid_columnconfigure(0, weight=3)
        middle.grid_columnconfigure(1, weight=2, minsize=220)
        self._build_chart(middle, "overview").grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._build_watchlist(middle).grid(row=0, column=1, sticky="nsew")
        middle.grid_propagate(False)
        self._build_holdings(page, "overview").grid(row=2, column=0, sticky="nsew")

    def _build_markets(self, page):
        self._build_chart(page, "markets").pack(fill="both", expand=True)

    def _build_portfolio(self, page):
        self._build_metrics(page, "portfolio").pack(fill="x", pady=(0, 13))
        self._build_holdings(page, "portfolio").pack(fill="both", expand=True)

    def _build_metrics(self, page, view):
        metrics = tk.Frame(page, bg=THEME["background"])
        items = []
        titles = ["Portföy değeri", "Toplam maliyet", "Açık K/Z"]
        if view == "portfolio":
            titles.append("Gerçekleşen K/Z")
        for column, title in enumerate(titles):
            metrics.grid_columnconfigure(column, weight=1, uniform="metrics")
            card = self._panel(metrics)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 9) if column + 1 < len(titles) else 0)
            self._label(card, title, fg=THEME["text_secondary"]).pack(fill="x", padx=10, pady=(9, 0))
            value = self._label(card, "—", role=15 if view == "portfolio" else 19, weight="bold")
            value.pack(fill="x", padx=10, pady=(1, 0))
            caption = self._label(card, "", role="small", fg=THEME["text_secondary"])
            caption.pack(fill="x", padx=10, pady=(2, 9))
            items.append((value, caption))
        self.metrics[view] = items
        return metrics

    def _build_chart(self, parent, view):
        panel = self._panel(parent)
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)
        controls = tk.Frame(panel, bg=THEME["surface"])
        controls.grid(row=0, column=0, sticky="ew", padx=9, pady=(7 if view == "overview" else 10, 0))
        selector = ttk.Combobox(controls, style="Modern.TCombobox", state="readonly", width=22)
        selector.pack(side="left", fill="x", expand=True)
        selector.bind("<<ComboboxSelected>>", lambda _event: self._select_asset(self.selector_options[selector.get()]))
        if view == "overview":
            self._button(controls, "Detaylı grafik", lambda: self.show_view("markets")).pack(side="right", padx=(8, 0))
        chart = WorkspaceChart(
            panel, self.app._cached_history_series,
            compact=view == "overview", initial_period=self.chart_period,
            on_period_changed=self._remember_period, **self.app.workspace_chart_helpers(),
        )
        chart.grid(row=1, column=0, sticky="nsew")
        self.charts[view] = (selector, chart)
        return panel

    def _build_watchlist(self, parent):
        panel = self._panel(parent)
        top = tk.Frame(panel, bg=THEME["surface"])
        top.pack(fill="x", padx=11, pady=(9, 6))
        self._label(top, "İzleme listesi", weight="bold").pack(side="left")
        self.watch_caption = self._label(top, "", fg=THEME["text_secondary"], role="small")
        self.watch_caption.pack(side="right")
        scroll = ttk.Scrollbar(panel, orient="vertical", style="Modern.Vertical.TScrollbar")
        canvas = tk.Canvas(panel, bg=THEME["surface"], highlightthickness=0, yscrollcommand=scroll.set)
        scroll.config(command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.watch_container = tk.Frame(canvas, bg=THEME["surface"])
        inner = canvas.create_window(0, 0, window=self.watch_container, anchor="nw")
        self.watch_container.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(inner, width=event.width))
        self.watch_canvas = canvas
        return panel

    def _rebuild_watchlist(self):
        self.watch_rows.clear()
        for child in self.watch_container.winfo_children():
            child.destroy()
        for instrument in self.app.watchlist:
            key = instrument["key"]
            line = tk.Frame(self.watch_container, bg=THEME["surface"], padx=7, pady=4)
            line.pack(fill="x", padx=4)
            line.grid_columnconfigure(0, weight=1)
            button = self._button(line, instrument["label"], lambda item=key: self._select_asset(item), anchor="w")
            button.grid(row=0, column=0, rowspan=3, sticky="ew", padx=(1, 5))
            spark = tk.Canvas(line, width=49, height=23, bg=THEME["surface"], highlightthickness=0)
            spark.grid(row=0, column=1, rowspan=3, padx=(0, 7))
            value = self._label(line, "—", weight="bold", anchor="e")
            value.grid(row=0, column=2, sticky="e")
            change = self._label(line, "—", role="small", anchor="e")
            change.grid(row=1, column=2, sticky="e")
            quote_status = self._label(line, "", role="caption", anchor="e", fg=THEME["text_secondary"])
            quote_status.grid(row=2, column=2, sticky="e")
            self.watch_rows[key] = (button, spark, value, change, quote_status)
            for element in (line, spark, button, value, change, quote_status):
                element.bind("<MouseWheel>", self._scroll_watchlist, add="+")
        if not self.watch_rows:
            self._label(self.watch_container, "İzleme listen boş.", fg=THEME["text_muted"]).pack(fill="x", padx=9, pady=9)
        self.watch_canvas.yview_moveto(0)

    def _scroll_watchlist(self, event):
        self.watch_canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def _configure_holdings_style(self):
        style = ttk.Style(self)
        style.configure(
            "Workspace.Treeview", background=THEME["surface"], fieldbackground=THEME["surface"],
            foreground=THEME["text_primary"], borderwidth=0, relief="flat", rowheight=30, font=font(self),
            bordercolor=THEME["border"], lightcolor=THEME["border"], darkcolor=THEME["border"],
        )
        style.configure(
            "Workspace.Treeview.Heading", background=THEME["surface_alt"], foreground=THEME["text_secondary"],
            relief="flat", padding=(5, 6), font=font(self, "small", "bold"),
        )
        style.map("Workspace.Treeview", background=[("selected", THEME["primary_soft"])], foreground=[("selected", THEME["text_primary"])])
        style.map("Workspace.Treeview.Heading", background=[("active", THEME["surface_alt"])])
        style.configure(
            "Modern.Horizontal.TScrollbar", troughcolor=THEME["background"],
            background=THEME["surface_alt"], bordercolor=THEME["border"],
            lightcolor=THEME["surface_alt"], darkcolor=THEME["surface_alt"],
            arrowcolor=THEME["text_secondary"], arrowsize=12,
        )

    def _build_holdings(self, parent, view):
        panel = self._panel(parent)
        header = tk.Frame(panel, bg=THEME["surface"])
        header.pack(fill="x", padx=12, pady=7)
        self._label(header, "Açık pozisyonlar", weight="bold").pack(side="left")
        if view == "overview":
            self._button(header, "Portföyü aç", lambda: self.show_view("portfolio")).pack(side="right")
        else:
            self._label(header, "Maliyetler kayıtlı alış ve satışlara göre güncellenir.", fg=THEME["text_muted"], role="small").pack(side="right")
        table_area = tk.Frame(panel, bg=THEME["surface"])
        table_area.pack(fill="both", expand=True)
        columns = [
            ("asset", "Varlık", 128), ("quantity", "Miktar", 70), ("average", "Ort. maliyet", 115),
            ("price", "Son fiyat", 102), ("value", "Piyasa değeri", 97), ("profit", "Kâr / zarar", 93),
        ]
        if view == "portfolio":
            columns.extend((("return", "K/Z %", 69), ("weight", "Ağırlık", 68), ("status", "Fiyat durumu", 112)))
        table = ttk.Treeview(table_area, columns=tuple(key for key, _title, _width in columns), show="headings", height=1, selectmode="browse", style="Workspace.Treeview")
        for key, title, width in columns:
            anchor = "w" if key == "asset" else "e"
            table.heading(key, text=title, anchor=anchor)
            minimum = round(width * .78) if view == "portfolio" else 52
            table.column(key, width=width, minwidth=minimum, stretch=True, anchor="w" if key == "status" else anchor)
        scrollbar = ttk.Scrollbar(table_area, orient="vertical", command=table.yview, style="Modern.Vertical.TScrollbar")
        table.configure(yscrollcommand=scrollbar.set)
        if view == "portfolio":
            horizontal = ttk.Scrollbar(table_area, orient="horizontal", command=table.xview, style="Modern.Horizontal.TScrollbar")
            table.configure(xscrollcommand=horizontal.set)
            horizontal.pack(side="bottom", fill="x")
        scrollbar.pack(side="right", fill="y")
        table.pack(side="left", fill="both", expand=True)
        table.tag_configure("even", background=THEME["surface"])
        table.tag_configure("odd", background=THEME["surface_stripe"])
        table.bind("<Double-1>", lambda event: self._open_holdings_chart(table.identify_row(event.y)))
        table.bind("<Return>", lambda _event: self._open_holdings_chart(table.focus()))
        self.holdings[view] = table
        return panel

    def _open_holdings_chart(self, key):
        if key and any(asset["key"] == key for asset in self.app.workspace_instruments()):
            self.show_view("markets", key)

    def _build_trades(self, page):
        panel = self._panel(page)
        panel.pack(fill="both", expand=True)
        toolbar = tk.Frame(panel, bg=THEME["surface"])
        toolbar.pack(fill="x", padx=12, pady=10)
        self._label(toolbar, "İşlem geçmişi", weight="bold").pack(side="left")
        self.trade_search = tk.StringVar(self)
        self.trade_filter = tk.StringVar(self, value="Tüm işlemler")
        self.trade_count = self._label(toolbar, "", role="small", fg=THEME["text_muted"])
        self.trade_count.pack(side="right", padx=(7, 0))
        kinds = ttk.Combobox(toolbar, state="readonly", style="Modern.TCombobox", width=11, textvariable=self.trade_filter, values=("Tüm işlemler", "Alış", "Satış"))
        kinds.pack(side="right", padx=(6, 0))
        kinds.bind("<<ComboboxSelected>>", lambda _event: self._refresh_trades())
        SearchEntry(toolbar, self.trade_search).pack(side="right", fill="x", expand=True, padx=(20, 0))
        filters = tk.Frame(panel, bg=THEME["surface"])
        filters.pack(fill="x", padx=12, pady=(0, 8))
        filters.grid_columnconfigure(0, weight=1)
        self.trade_asset = ttk.Combobox(filters, style="Modern.TCombobox", state="readonly", width=14)
        self.trade_asset.set("Tüm varlıklar")
        self.trade_asset.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.trade_asset.bind("<<ComboboxSelected>>", lambda _event: self._refresh_trades())
        self.trade_start, self.trade_end = tk.StringVar(self), tk.StringVar(self)
        self.trade_date_range = (None, None)
        self.trade_asset_options = {}
        for column, (label, variable) in enumerate((("Başlangıç", self.trade_start), ("Bitiş", self.trade_end)), 1):
            group = tk.Frame(filters, bg=THEME["surface"])
            group.grid(row=0, column=column, sticky="w", padx=(0, 7))
            self._label(group, label, role="small", fg=THEME["text_secondary"]).pack(side="left", padx=(0, 4))
            field = ModernEntry(group, textvariable=variable, width=10)
            field.pack(side="left")
            field.entry.bind("<Return>", self._apply_trade_dates)
        self._button(filters, "Uygula", self._apply_trade_dates).grid(row=0, column=3, sticky="e")
        self.trade_date_message = self._label(filters, "GG-AA-YYYY", role="caption", fg=THEME["text_secondary"])
        self.trade_date_message.grid(row=1, column=1, columnspan=3, sticky="w", pady=(4, 0))
        self.trade_table = TransactionTable(panel, self._edit_trade, self._delete_trade, self._cycle_trade_sort)
        self.trade_table.pack(fill="both", expand=True)
        self.trade_search.trace_add("write", lambda *_args: self._refresh_trades())

    def _cycle_trade_sort(self, column):
        key, order = self._trade_sort
        self._trade_sort = (column, (order + 1) % 3 if key == column else 1)
        self._refresh_trades()

    def _apply_trade_dates(self, _event=None):
        try:
            values = (self.trade_start.get().strip(), self.trade_end.get().strip())
            start, end = (datetime.strptime(value, "%d-%m-%Y").date() if value else None for value in values)
        except ValueError:
            self.trade_date_message.config(text="Tarihleri GG-AA-YYYY biçiminde gir.", fg=THEME["negative"])
            return "break"
        if start and end and start > end:
            self.trade_date_message.config(text="Başlangıç, bitiş tarihinden sonra olamaz.", fg=THEME["negative"])
            return "break"
        self.trade_date_range = (start, end)
        self.trade_date_message.config(text="GG-AA-YYYY", fg=THEME["text_secondary"])
        self._refresh_trades()
        return "break"

    def _refresh_trades(self):
        if self._ledger_rows is None:
            self._ledger_rows = self.app.workspace_transaction_rows()
            assets = {row.get("instrument_key", row["asset"]): row["asset"] for row in self._ledger_rows}
            counts = Counter(assets.values())
            self.trade_asset_options = {
                f"{label} · {key}" if counts[label] > 1 or label == "Tüm varlıklar" else label: key
                for key, label in assets.items()
            }
            choices = ("Tüm varlıklar", *sorted(self.trade_asset_options, key=str.casefold))
            self.trade_asset.config(values=choices)
            if self.trade_asset.get() not in choices:
                self.trade_asset.set(choices[0])
        query = self.trade_search.get().strip().casefold().replace(",", ".")
        side = {"Alış": "buy", "Satış": "sell"}.get(self.trade_filter.get())
        asset = self.trade_asset_options.get(self.trade_asset.get())
        start, end = self.trade_date_range
        rows = []
        for row in self._ledger_rows:
            if asset is not None and row.get("instrument_key", row["asset"]) != asset:
                continue
            if side is not None and row["action_key"] != side:
                continue
            if query not in row["searchable"].replace(",", "."):
                continue
            date = row["sort_values"]["date"]
            if (start or end) and (date == datetime.min or (start and date.date() < start) or (end and date.date() > end)):
                continue
            rows.append(row)
        field, order = self._trade_sort
        if order:
            rows.sort(key=lambda row: (row["sort_values"][field], row["source_index"]), reverse=order == 2)
        self.trade_table.set_sort_indicator(field, order)
        self.trade_table.set_rows(rows)
        self.trade_count.config(text=f"{len(rows)} / {len(self._ledger_rows)}")

    def _build_settings(self, page):
        canvas = tk.Canvas(page, bg=THEME["background"], highlightthickness=0)
        scroll = ttk.Scrollbar(page, orient="vertical", command=canvas.yview, style="Modern.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = tk.Frame(canvas, bg=THEME["background"])
        content_id = canvas.create_window(0, 0, window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(content_id, width=event.width))

        def group(title):
            box = self._panel(content)
            box.pack(fill="x", pady=(0, 9))
            self._label(box, title, role="section", weight="bold").pack(anchor="w", padx=14, pady=(11, 7))
            return box

        box = group("Görünüm")
        for label, variable, command in (
            ("Widget’ı üstte tut", self.app.var_topmost, self.app.toggle_topmost),
            ("Windows’la başlat", self.app.var_autostart, self.app.toggle_autostart),
        ):
            row = self._settings_row(box)
            self._label(row, label).pack(side="left")
            tk.Checkbutton(
                row, variable=variable, command=command, bg=THEME["surface"], activebackground=THEME["surface"],
                fg=THEME["primary"], activeforeground=THEME["primary"], selectcolor=THEME["input"],
                highlightthickness=0, bd=0,
            ).pack(side="right")
        row = self._settings_row(box)
        self._label(row, "Mini grafik zaman aralığı").pack(side="left")
        periods = dict(self.app.CHART_PERIOD_OPTIONS)
        period = ttk.Combobox(row, style="Modern.TCombobox", state="readonly", values=tuple(periods), width=14)
        period.pack(side="right")

        def change_period(_event):
            self.app.sparkline_period.set(periods[period.get()])
            self.app._on_sparkline_period_changed()

        period.bind("<<ComboboxSelected>>", change_period)
        self.settings_period = period
        watch = self._settings_row(box)
        self._label(watch, "İzleme listesi").pack(side="left")
        self._button(watch, "İzlenenleri düzenle", lambda: self.app.open_watchlist_settings(parent=self)).pack(side="right")

        data = group("Veri ve yedek")
        for title, label, command in (
            ("İşlem dosyasından kayıt ekle", "İçe aktar", self.app.import_transactions),
            ("İşlem kayıtlarının kopyasını al", "Dışa aktar", self.app.export_transactions),
            ("İşlem defterini yedekten kurtar", "Yedeği geri yükle", self.app.restore_transactions),
        ):
            row = self._settings_row(data)
            self._label(row, title).pack(side="left")
            self._button(row, label, lambda step=command: step(parent=self)).pack(side="right")

        update = self._settings_row(group("Güncelleme"))
        self._label(update, "Windows uygulaması").pack(side="left")
        self._button(update, "Güncellemeleri kontrol et", lambda: self.app.check_updates(parent=self)).pack(side="right")

        def wheel(event):
            canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"

        children = [content, canvas]
        while children:
            widget = children.pop()
            widget.bind("<MouseWheel>", wheel, add="+")
            if widget is not canvas:
                children.extend(widget.winfo_children())

    def _settings_row(self, box):
        tk.Frame(box, bg=THEME["border"], height=1).pack(fill="x")
        row = tk.Frame(box, bg=THEME["surface"], padx=14, pady=9)
        row.pack(fill="x")
        return row

    def open_trade(self):
        self.app.open_add_transaction(parent=self, instrument_key=self.selected_key)

    def _edit_trade(self, source_index):
        self.app.open_add_transaction(parent=self, edit_index=source_index)

    def _delete_trade(self, source_index):
        self.app.delete_saved_transaction(source_index, parent=self)

    def _select_asset(self, key):
        self.selected_key = key
        self._update_chart()
        if self.view == "overview":
            self._update_watchlist()

    def _remember_period(self, period):
        self.chart_period = period

    def _update_chart(self, history_changed=False):
        if self.view not in self.charts:
            return
        selector, chart = self.charts[self.view]
        instruments = {item["key"]: item for item in self.app.workspace_instruments()}
        if self.selected_key not in instruments:
            self.selected_key = next(iter(instruments), None)
        options = {f"{asset['label']} · {asset['symbol']}": key for key, asset in instruments.items()}
        if options != self.selector_options or tuple(selector.cget("values")) != tuple(options):
            self.selector_options = options
            selector.configure(values=tuple(options))
        selector.set(next((label for label, key in options.items() if key == self.selected_key), ""))
        chart.set_period(self.chart_period)
        chart.set_instrument(
            instruments.get(self.selected_key), price=self.app._last_prices.get(self.selected_key),
            quote_status=self.app.workspace_quote_status(self.selected_key, compact=self.view == "overview") if self.selected_key else "",
        )
        if history_changed:
            chart.refresh()

    def _update_watchlist(self):
        keys = tuple((asset["key"], asset["label"]) for asset in self.app.watchlist)
        if keys != self.watch_keys:
            self.watch_keys = keys
            self._rebuild_watchlist()
        period = PERIOD_LABELS.get(self.app.sparkline_period.get(), "")
        self.watch_caption.config(text=f"Değişim · {period}" if period else "")
        for key, (button, spark, value, change, quote_status) in self.watch_rows.items():
            data = self.app.workspace_watch_row(key)
            if data is None:
                continue
            price, delta, color, sparkline = data
            button.configure(bg=THEME["primary_soft"] if key == self.selected_key else THEME["surface"], fg=THEME["primary"] if key == self.selected_key else THEME["text_primary"])
            value.config(text=price)
            change.config(text=delta, fg=color)
            quote_status.config(text=self.app.workspace_quote_status(key, compact=True))
            if sparkline is not None:
                self.app._draw_sparkline(spark, *sparkline)

    def _update_holdings(self):
        rows = self.summary["rows"]
        labels = self.metrics[self.view]
        priced = sum(row["has_price"] for row in rows)
        ledger_unavailable = bool(self.app.tm.load_error)
        cost = self.summary["total_cost_tl"] if not ledger_unavailable else None
        value = self.summary["total_value_tl"] if not ledger_unavailable and (priced or not rows) else None
        profit = self.summary["profit_tl"] if not ledger_unavailable and (priced or not rows) else None
        profit_color = THEME["text_primary"] if not profit else THEME["positive"] if profit > 0 else THEME["negative"]
        labels[0][0].config(text=money(value))
        labels[0][1].config(text=f"{priced}/{len(rows)} pozisyon fiyatlandı" if priced < len(rows) else f"{len(rows)} açık pozisyon")
        labels[1][0].config(text=money(cost))
        labels[1][1].config(text="Açık pozisyonların maliyeti")
        labels[2][0].config(text=signed_money(profit), fg=profit_color)
        labels[2][1].config(text="Fiyatlanan pozisyonlar" if priced < len(rows) else f"%{turkish_number(self.summary['profit_pct'])}")
        if self.view == "portfolio":
            realized = self.summary.get("realized_profit_tl", 0) if not ledger_unavailable else None
            realized_color = THEME["text_primary"] if not realized else THEME["positive"] if realized > 0 else THEME["negative"]
            labels[3][0].config(text=signed_money(realized), fg=realized_color)
            labels[3][1].config(text="Tüm satışlar")
        if ledger_unavailable:
            labels[0][1].config(text="Kayıt okunamadı")
            labels[2][1].config(text="")
        table = self.holdings[self.view]
        current = set(table.get_children())
        if not rows:
            stale = current - {"no-positions"}
            if stale:
                table.delete(*stale)
            empty = ("İşlem kayıtları okunamadı" if ledger_unavailable else "Açık pozisyon yok",)
            if "no-positions" in current:
                table.item("no-positions", values=empty)
            else:
                table.insert("", "end", iid="no-positions", values=empty)
            return
        for index, row in enumerate(rows):
            key = row["key"]
            unit = row["unit"]
            values = (
                row["label"], f"{quantity_text(row['quantity'])} {unit}",
                f"{money(row['average_cost_tl'])} / {unit}", money(row["current_price_tl"]),
                money(row["value_tl"] if row["has_price"] else None), signed_money(row.get("profit_tl")),
            )
            if self.view == "portfolio":
                returns = row.get("profit_pct")
                weight = 100 * row["value_tl"] / value if value and row["has_price"] and priced == len(rows) else None
                values += (
                    f"%{turkish_number(returns)}" if returns is not None else "—",
                    f"%{turkish_number(weight, 1)}" if weight is not None else "—",
                    self.app.workspace_quote_status(key, compact=True),
                )
            tag = "odd" if index % 2 else "even"
            if key in current:
                current.remove(key)
                if tuple(table.item(key, "values")) != values or tuple(table.item(key, "tags")) != (tag,):
                    table.item(key, values=values, tags=(tag,))
                if table.index(key) != index:
                    table.move(key, "", index)
            else:
                table.insert("", index, iid=key, values=values, tags=(tag,))
        if current:
            table.delete(*current)

    def refresh(self, summary=None, *, history_changed=False, ledger_changed=False):
        if summary is not None:
            self.summary = summary
        if ledger_changed:
            self._ledger_rows = None
        if self.view is None:
            return
        self._update_status()
        self._update_chart(history_changed)
        if self.view in self.holdings:
            self._update_holdings()
        if self.view == "overview":
            self._update_watchlist()
        elif self.view == "trades" and (history_changed or ledger_changed or self._ledger_rows is None):
            self._refresh_trades()
        elif self.view == "settings":
            labels = {seconds: label for label, seconds in self.app.CHART_PERIOD_OPTIONS}
            self.settings_period.set(labels.get(self.app.sparkline_period.get(), ""))
