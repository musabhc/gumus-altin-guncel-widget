"""Watchlist editing dialog."""

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from piyasa.ui.components import HoverButton, ModernEntry
from piyasa.ui.theme import SPACING, THEME, configure_ttk_styles, font


class WatchlistDialog(tk.Toplevel):
    def __init__(self, parent, manager, on_change_callback, price_validator):
        super().__init__(parent)
        self.manager = manager
        self.on_change_callback = on_change_callback
        self.price_validator = price_validator
        self.title("İzlenenler")
        self.geometry("780x560")
        self.minsize(700, 520)
        self.configure(bg=THEME["background"])
        configure_ttk_styles(self)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self, bg=THEME["background"])
        header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=SPACING["xl"],
            pady=(SPACING["lg"], SPACING["md"]),
        )
        tk.Label(
            header,
            text="İzlenen Varlıklar",
            bg=THEME["background"],
            fg=THEME["text_primary"],
            font=font(self, "title", "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Widget'ta gösterilecek piyasa sembollerini yönetin.",
            bg=THEME["background"],
            fg=THEME["text_secondary"],
            font=font(self, "body"),
            anchor="w",
        ).pack(anchor="w", pady=(SPACING["xxs"], 0))

        content = tk.Frame(self, bg=THEME["background"])
        content.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=SPACING["xl"],
            pady=(0, SPACING["xl"]),
        )
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1, minsize=410)
        content.grid_columnconfigure(1, weight=0, minsize=260)

        left_frame = tk.Frame(
            content,
            bg=THEME["surface"],
            padx=SPACING["md"],
            pady=SPACING["md"],
            highlightthickness=1,
            highlightbackground=THEME["border"],
        )
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, SPACING["sm"]))
        left_frame.grid_rowconfigure(1, weight=1)
        left_frame.grid_columnconfigure(0, weight=1)

        tk.Label(
            left_frame,
            text="İzleme Listesi",
            bg=THEME["surface"],
            fg=THEME["text_primary"],
            font=font(self, "section", "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, SPACING["sm"]))

        table_frame = tk.Frame(left_frame, bg=THEME["border"], padx=1, pady=1)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        columns = ("label", "symbol", "currency", "delete")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=13,
            style="Modern.Treeview",
        )
        self.tree.heading("label", text="Ad")
        self.tree.heading("symbol", text="Yahoo Sembolü")
        self.tree.heading("currency", text="Birim")
        self.tree.heading("delete", text="")
        self.tree.column("label", width=150, minwidth=100, anchor="w")
        self.tree.column("symbol", width=120, minwidth=90, anchor="center")
        self.tree.column("currency", width=60, minwidth=50, anchor="center", stretch=False)
        self.tree.column("delete", width=52, minwidth=48, anchor="center", stretch=False)
        self.tree.tag_configure("hidden", foreground=THEME["text_muted"])
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<ButtonRelease-1>", self.on_click)

        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview,
            style="Modern.Vertical.TScrollbar",
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        right_frame = tk.Frame(
            content,
            bg=THEME["surface"],
            padx=SPACING["lg"],
            pady=SPACING["lg"],
            highlightthickness=1,
            highlightbackground=THEME["border"],
        )
        right_frame.grid(row=0, column=1, sticky="nsew")

        tk.Label(
            right_frame,
            text="Yeni Varlık",
            bg=THEME["surface"],
            fg=THEME["text_primary"],
            font=font(self, "section", "bold"),
        ).pack(anchor="w")
        tk.Label(
            right_frame,
            text="Geçerli bir Yahoo Finance sembolü ekleyin.",
            bg=THEME["surface"],
            fg=THEME["text_secondary"],
            font=font(self, "small"),
            wraplength=220,
            justify="left",
        ).pack(anchor="w", pady=(SPACING["xxs"], SPACING["md"]))

        field_label = {
            "bg": THEME["surface"],
            "fg": THEME["text_secondary"],
            "font": font(self, "small"),
        }

        tk.Label(right_frame, text="Görünen Ad", **field_label).pack(anchor="w")
        self.entry_label = ModernEntry(right_frame)
        self.entry_label.pack(fill="x", pady=(SPACING["xxs"], SPACING["sm"]))

        tk.Label(right_frame, text="Yahoo Sembolü", **field_label).pack(anchor="w")
        self.entry_symbol = ModernEntry(right_frame)
        self.entry_symbol.pack(fill="x", pady=(SPACING["xxs"], SPACING["xxs"]))
        tk.Label(
            right_frame,
            text="Örnek: THYAO.IS, AAPL, BTC-USD",
            bg=THEME["surface"],
            fg=THEME["text_muted"],
            font=font(self, "caption"),
        ).pack(anchor="w", pady=(0, SPACING["sm"]))

        tk.Label(right_frame, text="Para Birimi", **field_label).pack(anchor="w")
        self.var_currency = tk.StringVar(value="₺")
        self.combo_currency = ttk.Combobox(
            right_frame,
            textvariable=self.var_currency,
            values=["₺", "$", "€", ""],
            state="readonly",
            style="Modern.TCombobox",
        )
        self.combo_currency.pack(fill="x", pady=(SPACING["xxs"], SPACING["sm"]))

        self.var_status = tk.StringVar(value="")
        tk.Label(
            right_frame,
            textvariable=self.var_status,
            bg=THEME["surface"],
            fg=THEME["text_secondary"],
            font=font(self, "small"),
            wraplength=220,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(0, SPACING["xs"]))

        self.btn_add = HoverButton(
            right_frame,
            text="+  Varlık Ekle",
            command=self.add_symbol,
            pady=10,
        )
        self.btn_add.pack(fill="x")

        self.load_list()

    def load_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        visible = {item["key"] for item in self.manager.instruments}
        for instrument in (*self.manager.instruments, *self.manager.archived.values()):
            active = instrument["key"] in visible
            if self.tree.exists(instrument["key"]):
                continue
            self.tree.insert("", "end", iid=instrument["key"], values=(
                instrument["label"],
                instrument["symbol"],
                instrument.get("currency", ""),
                "Gizle" if active else "Göster",
            ), tags=() if active else ("hidden",))

    def on_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        column = self.tree.identify_column(event.x)
        if column != "#4":
            return
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        try:
            if item_id in self.manager.archived:
                self.manager.restore(item_id)
            else:
                self.manager.remove(item_id)
            self.load_list()
            self.on_change_callback()
        except (OSError, ValueError) as error:
            messagebox.showerror("İzleme listesi", str(error), parent=self)

    def add_symbol(self):
        label = self.entry_label.get().strip()
        symbol = self.entry_symbol.get().strip().upper()
        currency = self.var_currency.get()

        if not label or not symbol:
            messagebox.showwarning("Eksik Bilgi", "Görünen ad ve Yahoo sembolü zorunlu.", parent=self)
            return
        if self.manager.has_symbol(symbol):
            messagebox.showwarning("Tekrar", "Bu sembol zaten izleme listesinde.", parent=self)
            return

        self.btn_add.config(state="disabled")
        self.var_status.set("Sembol kontrol ediliyor...")
        threading.Thread(target=self._validate_and_add, args=(label, symbol, currency), daemon=True).start()

    def _validate_and_add(self, label, symbol, currency):
        error = None
        try:
            price = self.price_validator(symbol)
            if not price or price <= 0:
                error = "Bu sembol için geçerli fiyat bulunamadı."
        except Exception as e:
            error = f"Sembol doğrulanamadı: {e}"
        self.after(0, lambda: self._finish_add(label, symbol, currency, error))

    def _finish_add(self, label, symbol, currency, error):
        self.btn_add.config(state="normal")
        if error:
            self.var_status.set("")
            messagebox.showerror("Sembol Eklenemedi", error, parent=self)
            return
        try:
            self.manager.add(label, symbol, currency)
            self.load_list()
            self.on_change_callback()
            self.entry_label.delete(0, 'end')
            self.entry_symbol.delete(0, 'end')
            self.var_status.set("Eklendi.")
        except ValueError as e:
            self.var_status.set("")
            messagebox.showerror("Hata", str(e), parent=self)
