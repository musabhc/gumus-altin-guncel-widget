"""Portfolio management and quick-trade forms."""

import math
import re
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from piyasa.market.instruments import portfolio_instruments, portfolio_unit, safe_float
from piyasa.portfolio.transactions import TransactionManager
from piyasa.ui.components import (
    HoverButton,
    IconButton,
    ModernEntry,
    SearchEntry,
    SegmentedControl,
    TransactionTable,
)
from piyasa.ui.theme import SPACING, THEME, configure_ttk_styles, font, turkish_number
from piyasa.ui.trade_calendar import TradeCalendar


class PortfolioManagerDialog(tk.Toplevel):
    ALL_ASSETS_LABEL = "Tüm Varlıklar"

    def __init__(self, parent, manager, on_save_callback, current_dollar_rate, instruments, *, quick=False):
        super().__init__(parent)
        self.quick = quick
        self.manager = manager
        self.on_save_callback = on_save_callback
        self.current_dollar_rate = current_dollar_rate
        self.instruments = portfolio_instruments(instruments)
        self.instrument_by_key = {item["key"]: item for item in self.instruments}
        self.instrument_label_to_key = {item["label"]: item["key"] for item in self.instruments}
        self.edit_index = None
        self.sort_column = None
        self.sort_direction = 0
        self._form_scroll_after_id = None
        self._preview_after_id = None
        self._present_after_id = None
        self._quick_focused = False
        self._calendar_window = None
        self._price_mode = "Birim fiyat" if quick else "Toplam tutar"
        self.var_price_mode = tk.StringVar(self, value=self._price_mode)
        self.var_form_title = tk.StringVar(self, value="İşlem ekle" if quick else "Yeni İşlem")
        self.bg_color = THEME["background"]
        self.color_card = THEME["surface"]
        self.color_card_alt = THEME["surface_alt"]
        self.color_border = THEME["border"]
        self.color_text_main = THEME["text_primary"]
        self.color_text_dim = THEME["text_secondary"]
        self.color_text_muted = THEME["text_muted"]
        self.color_accent = THEME["primary"]
        self.color_danger = THEME["negative"]
        self.style_entry = {
            "bg": THEME["input"],
            "fg": THEME["text_primary"],
            "insertbackground": THEME["text_primary"],
            "relief": "flat",
            "font": font(self, "body"),
        }
        self.style_label = {
            "bg": THEME["surface"],
            "fg": THEME["text_secondary"],
            "font": font(self, "small"),
        }

        self.title("İşlem ekle" if quick else "Portföy Yönetimi")
        self.geometry("456x624" if quick else "1120x760")
        self.minsize(*((360, 440) if quick else (980, 700)))
        self.configure(bg=self.bg_color)
        self.overrideredirect(True)
        self.transient(parent)
        self.bind("<Escape>", lambda _event: self._close_requested())
        self.protocol("WM_DELETE_WINDOW", self._close_requested)
        configure_ttk_styles(self)

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self, bg=self.bg_color)
        header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=SPACING["xl"],
            pady=(SPACING["lg"], SPACING["md"]),
        )
        self.close_button = IconButton(header, "close", self._close_requested, size=30,
                                       bg=self.bg_color, danger=True, show_border=False)
        self.close_button.pack(side="right", anchor="n")
        title_label = tk.Label(
            header,
            text="Portföy Yönetimi",
            bg=self.bg_color,
            fg=THEME["text_primary"],
            font=font(self, "title", "bold"),
            anchor="w",
        )
        title_label.pack(anchor="w")
        if quick:
            title_label.configure(textvariable=self.var_form_title)
        for handle in (header, title_label):
            handle.bind("<ButtonPress-1>", self._start_window_move)
            handle.bind("<B1-Motion>", self._move_window)
        self.resize_grip = IconButton(self, "grip", None, size=14, bg=self.bg_color,
                                      fg=self.color_text_muted, show_border=False)
        self.resize_grip.configure(cursor="sizing")
        self.resize_grip.place(relx=1, rely=1, anchor="se")
        self.resize_grip.bind("<ButtonPress-1>", self._start_window_resize)
        self.resize_grip.bind("<B1-Motion>", self._resize_window)

        content = tk.Frame(self, bg=self.bg_color)
        content.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=SPACING["xl"],
            pady=(0, SPACING["xl"]),
        )
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1, minsize=0 if quick else 590)
        if not quick:
            content.grid_columnconfigure(1, minsize=330)

        if not self.quick:
            self._build_history(content)

        # --- Sağ kart: Yeni işlem / düzenleme formu ---
        right_frame = tk.Frame(
            content,
            bg=self.color_card,
            padx=SPACING["lg"],
            pady=SPACING["lg"],
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        right_frame.grid(row=0, column=0 if quick else 1, sticky="nsew")
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_rowconfigure(2, weight=1)

        if not quick:
            tk.Label(
                right_frame, textvariable=self.var_form_title, bg=self.color_card,
                fg=self.color_text_main, font=font(self, "section", "bold"), anchor="w",
            ).grid(row=0, column=0, sticky="ew", pady=(0, SPACING["md"]))

        form_host = tk.Frame(right_frame, bg=self.color_card)
        form_host.grid(row=2, column=0, sticky="nsew")
        form_host.grid_rowconfigure(0, weight=1)
        form_host.grid_columnconfigure(0, weight=1)
        self.form_canvas = tk.Canvas(
            form_host,
            width=290,
            height=1,
            bg=self.color_card,
            highlightthickness=0,
            bd=0,
        )
        self.form_scrollbar = ttk.Scrollbar(
            form_host,
            orient="vertical",
            command=self.form_canvas.yview,
            style="Modern.Vertical.TScrollbar",
        )
        self.form_canvas.configure(yscrollcommand=self.form_scrollbar.set)
        self.form_canvas.grid(row=0, column=0, sticky="nsew")
        self.form_scrollbar.grid(row=0, column=1, sticky="ns")
        self.form_body = tk.Frame(self.form_canvas, bg=self.color_card)
        form_body = self.form_body
        self._form_window_id = self.form_canvas.create_window(
            (0, 0), window=form_body, anchor="nw"
        )
        form_body.bind("<Configure>", self._sync_form_scrollregion)
        self.form_canvas.bind("<Configure>", self._resize_form_canvas)
        self.form_canvas.bind("<MouseWheel>", self._on_form_mousewheel)

        def field_frame(label_text, parent=form_body):
            frame = tk.Frame(parent, bg=self.color_card)
            tk.Label(frame, text=label_text, **self.style_label).pack(anchor="w", pady=(0, SPACING["xxs"]))
            return frame

        trade_details = tk.Frame(form_body, bg=self.color_card) if quick else form_body
        date_field = field_frame("Tarih", trade_details)
        if quick:
            date_field.pack(side="right", fill="both", expand=True, padx=(SPACING["xs"], 0))
        else:
            date_field.pack(fill="x", pady=(0, SPACING["sm"]))
        date_input = tk.Frame(date_field, bg=self.color_card)
        date_input.pack(fill="x")
        self.var_date = tk.StringVar(self, value=datetime.now().strftime("%d-%m-%Y"))
        self.entry_date = ModernEntry(date_input, textvariable=self.var_date)
        self.entry_date.pack(side="left", fill="x", expand=True)
        HoverButton(date_input, text="Takvim", command=self._open_calendar,
                    bg=THEME["surface_alt"], fg=THEME["text_primary"], padx=4).pack(side="right", padx=(4, 0), fill="y")

        # 2. Varlık
        instrument_field = field_frame("Varlık")
        instrument_field.pack(fill="x", pady=(0, SPACING["sm"]))
        default_label = self.instruments[0]["label"] if self.instruments else ""
        self.var_instrument = tk.StringVar(value=default_label)
        self.combo_instrument = ttk.Combobox(
            instrument_field,
            textvariable=self.var_instrument,
            values=[item["label"] for item in self.instruments],
            state="readonly",
            style="Modern.TCombobox",
        )
        self.combo_instrument.pack(fill="x")
        self.combo_instrument.bind("<<ComboboxSelected>>", lambda e: self.update_amount_label())
        self.lbl_average_cost = tk.Label(
            instrument_field, anchor="w", **self.style_label
        )
        self.lbl_average_cost.pack(fill="x", pady=(SPACING["xxs"], 0))

        # 3. İşlem türü
        action_field = field_frame("İşlem", trade_details)
        if quick:
            action_field.pack(side="left", fill="both", expand=True)
            trade_details.pack(fill="x", pady=(0, SPACING["sm"]))
        else:
            action_field.pack(fill="x", pady=(0, SPACING["sm"]))
        self.var_action = tk.StringVar(value="buy")
        self.segment_action = SegmentedControl(
            action_field,
            self.var_action,
            (("↑  Alış", "buy"), ("↓  Satış", "sell")),
            selection_colors={
                "buy": (THEME["positive_soft"], THEME["positive"]),
                "sell": (THEME["negative_soft"], THEME["negative"]),
            },
        )
        self.segment_action.pack(fill="x")

        # 4. Miktar
        amount_field = tk.Frame(form_body, bg=self.color_card)
        amount_field.pack(fill="x", pady=(0, SPACING["sm"]))
        self.lbl_amount = tk.Label(amount_field, text="Miktar", **self.style_label)
        self.lbl_amount.pack(anchor="w", pady=(0, SPACING["xxs"]))
        self.var_amount = tk.StringVar(self)
        self.entry_amount = ModernEntry(amount_field, textvariable=self.var_amount)
        self.entry_amount.pack(fill="x")
        self.sell_available = tk.StringVar(self)
        self.sell_available_label = tk.Label(amount_field, textvariable=self.sell_available,
                                             anchor="w", **self.style_label)

        # 5. Para Birimi
        currency_field = field_frame("Para Birimi")
        currency_field.pack(fill="x", pady=(0, SPACING["sm"]))
        self.var_currency = tk.StringVar(value="TL")
        self.segment_currency = SegmentedControl(
            currency_field,
            self.var_currency,
            (("₺  TL", "TL"), ("$  USD", "USD")),
            command=self.toggle_rate_entry,
        )
        self.segment_currency.pack(fill="x")

        # 6. Kur (Sadece USD seçiliyse görünür)
        self.frame_rate = field_frame("İşlem Kuru (USD/TL)")
        self.frame_rate.pack(fill="x", pady=(0, SPACING["sm"]))
        self.var_rate = tk.StringVar(self)
        self.entry_rate = ModernEntry(self.frame_rate, textvariable=self.var_rate)
        self.entry_rate.pack(fill="x")
        self._set_rate_input(self.current_dollar_rate)

        # 7. Toplam Tutar
        self.total_field_frame = field_frame("Fiyat" if quick else "Toplam Tutar")
        self.total_field_frame.pack(fill="x", pady=(0, SPACING["sm"]))
        self.var_entered_total = tk.StringVar(self)
        if quick:
            price_inputs = tk.Frame(self.total_field_frame, bg=self.color_card)
            price_inputs.pack(fill="x")
            mode = ttk.Combobox(
                price_inputs, textvariable=self.var_price_mode, values=("Birim fiyat", "Toplam tutar"),
                state="readonly", style="Modern.TCombobox", width=12,
            )
            mode.pack(side="right", padx=(SPACING["xs"], 0))
            mode.bind("<<ComboboxSelected>>", self._on_price_mode)
        else:
            price_inputs = self.total_field_frame
        self.entry_total = ModernEntry(price_inputs, textvariable=self.var_entered_total)
        self.entry_total.pack(fill="x")
        commission = field_frame("Komisyon (₺, isteğe bağlı)")
        commission.pack(fill="x", pady=(0, SPACING["sm"]))
        self.var_fee = tk.StringVar(self)
        self.entry_fee = ModernEntry(commission, textvariable=self.var_fee)
        self.entry_fee.pack(fill="x")
        if quick:
            self._build_trade_preview(form_body)

        # Ekle/Güncelle Butonları
        button_frame = tk.Frame(right_frame, bg=self.color_card)
        button_frame.grid(row=3, column=0, sticky="ew", pady=(SPACING["xs"], 0))
        self.btn_save = HoverButton(
            button_frame,
            text="İşlemi kaydet" if quick else "+  İşlemi Ekle",
            command=self.save,
            pady=10,
        )
        if quick:
            self.btn_save.pack(side="right", padx=(SPACING["xs"], 0))
            HoverButton(
                button_frame, text="Vazgeç", command=self._close_requested,
                bg=THEME["surface_alt"], hover_bg=THEME["surface_hover"],
                pressed_bg=THEME["surface_pressed"], fg=THEME["text_primary"], pady=10,
            ).pack(side="right")
        else:
            self.btn_save.pack(fill="x", pady=(0, SPACING["xs"]))

        self.btn_cancel_edit = HoverButton(
            button_frame,
            text="Düzenlemeyi İptal Et",
            command=self._cancel_edit_requested,
            bg=THEME["surface_alt"],
            hover_bg=THEME["surface_hover"],
            pressed_bg=THEME["surface_pressed"],
            fg=THEME["text_secondary"],
            pady=8,
        )

        self.update_amount_label()
        self.toggle_rate_entry()
        self._bind_form_mousewheel(self.form_body)
        self._schedule_form_scroll_sync()
        self.bind("<Destroy>", self._on_dialog_destroy, add="+")
        for variable in self._draft_variables():
            variable.trace_add("write", self._queue_trade_preview)
        if not quick:
            self._search_trace_id = self.var_search.trace_add("write", self._on_search_changed)
            self.load_list()
        self.mark_current_draft()
        self._present_after_id = self.after_idle(self._present_on_idle)

    def _draft_variables(self):
        return (self.var_date, self.var_amount, self.var_entered_total, self.var_rate, self.var_fee,
                self.var_instrument, self.var_action, self.var_currency, self.var_price_mode)

    def mark_current_draft(self):
        self._original_draft = tuple(variable.get() for variable in self._draft_variables())

    def confirm_discard_draft(self):
        current = tuple(variable.get() for variable in self._draft_variables())
        return current == self._original_draft or messagebox.askyesno(
            "Kaydedilmemiş işlem", "Formdaki değişikliklerden vazgeçmek istiyor musunuz?", parent=self,
        )

    def _close_requested(self):
        if self.confirm_discard_draft():
            self.destroy()

    def _cancel_edit_requested(self):
        if self.confirm_discard_draft():
            self.cancel_edit()

    def _open_calendar(self):
        if self._calendar_window is not None and self._calendar_window.winfo_exists():
            self._calendar_window.lift()
            return
        selected = self.manager.parse_record_date(self.var_date.get()) or datetime.now()
        self._calendar_window = TradeCalendar(
            self, min(selected.date(), datetime.now().date()),
            lambda day: self.var_date.set(day.strftime("%d-%m-%Y")),
        )

    def _build_history(self, content):
        left_frame = tk.Frame(
            content,
            bg=self.color_card,
            highlightthickness=1,
            highlightbackground=self.color_border,
        )
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, SPACING["sm"]))
        left_frame.grid_rowconfigure(3, weight=1)
        left_frame.grid_columnconfigure(0, weight=1)

        history_header = tk.Frame(left_frame, bg=self.color_card)
        history_header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=SPACING["lg"],
            pady=(SPACING["lg"], SPACING["md"]),
        )
        tk.Label(
            history_header,
            text="İşlem Geçmişi",
            bg=self.color_card,
            fg=self.color_text_main,
            font=font(self, "section", "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            history_header,
            text="Tüm alım-satım kayıtlarınızı arayın, düzenleyin veya silin.",
            bg=self.color_card,
            fg=self.color_text_dim,
            font=font(self, "small"),
            anchor="w",
        ).pack(anchor="w", pady=(SPACING["xxs"], 0))

        toolbar = tk.Frame(left_frame, bg=self.color_card)
        toolbar.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=SPACING["lg"],
            pady=(0, SPACING["md"]),
        )
        toolbar.grid_columnconfigure(0, weight=1)
        toolbar.grid_columnconfigure(1, minsize=180)

        self.var_search = tk.StringVar(value="")
        self.search_entry = SearchEntry(toolbar, self.var_search, placeholder="İşlem ara...")
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, SPACING["sm"]))

        self.var_asset_filter = tk.StringVar(value=self.ALL_ASSETS_LABEL)
        self.combo_asset_filter = ttk.Combobox(
            toolbar,
            textvariable=self.var_asset_filter,
            values=(self.ALL_ASSETS_LABEL,),
            state="readonly",
            style="Modern.TCombobox",
            width=20,
        )
        self.combo_asset_filter.grid(row=0, column=1, sticky="ew")
        self.combo_asset_filter.bind("<<ComboboxSelected>>", lambda _event: self.load_list())

        table_border = tk.Frame(
            left_frame,
            bg=self.color_border,
            padx=1,
            pady=1,
        )
        table_border.grid(
            row=3,
            column=0,
            sticky="nsew",
            padx=SPACING["lg"],
        )
        table_border.grid_rowconfigure(0, weight=1)
        table_border.grid_columnconfigure(0, weight=1)
        self.tree = TransactionTable(table_border, self.start_edit, self.delete_transaction,
                                     on_sort=self.cycle_sort)
        self.tree.grid(row=0, column=0, sticky="nsew")

        history_footer = tk.Frame(left_frame, bg=self.color_card)
        history_footer.grid(
            row=4,
            column=0,
            sticky="ew",
            padx=SPACING["lg"],
            pady=(SPACING["sm"], SPACING["md"]),
        )
        self.var_result_count = tk.StringVar(value="Toplam 0 işlem")
        tk.Label(
            history_footer,
            textvariable=self.var_result_count,
            bg=self.color_card,
            fg=self.color_text_muted,
            font=font(self, "small"),
            anchor="w",
        ).pack(side="left")

    def _present_on_idle(self):
        self._present_after_id = None
        self.present()

    def present(self):
        """Map above the owner, including when the widget is always on top."""
        if not self.winfo_exists():
            return
        if self._present_after_id is not None:
            self.after_cancel(self._present_after_id)
            self._present_after_id = None
        parent = self.master.winfo_toplevel()
        self.attributes("-topmost", bool(parent.attributes("-topmost")))
        self.deiconify()
        self.lift(parent)
        self.focus_force()
        if self.quick and not self._quick_focused:
            self.entry_amount.focus_set()
            self._quick_focused = True

    def _start_window_move(self, event):
        self._window_move_origin = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _move_window(self, event):
        dx, dy = self._window_move_origin
        self.geometry(f"{event.x_root - dx:+d}{event.y_root - dy:+d}")

    def _start_window_resize(self, event):
        self._window_resize_origin = (event.x_root, event.y_root, self.winfo_width(), self.winfo_height())

    def _resize_window(self, event):
        x, y, width, height = self._window_resize_origin
        min_width, min_height = self.minsize()
        self.geometry(f"{max(min_width, width + event.x_root - x)}x{max(min_height, height + event.y_root - y)}")

    def selected_instrument(self):
        key = self.instrument_label_to_key.get(self.var_instrument.get())
        return self.instrument_by_key.get(key)

    def update_amount_label(self):
        instrument = self.selected_instrument()
        unit = portfolio_unit(instrument or {})
        self.lbl_amount.config(text=f"Miktar ({unit})")
        average = self.manager.get_average_cost(instrument["key"]) if instrument else None
        if average is None:
            cost_text = "Ortalama maliyet —"
        else:
            cost_text = f"Ortalama maliyet ₺{average:,.2f} / {unit}"
        self.lbl_average_cost.config(text=cost_text)

    def _sync_form_scrollregion(self, _event=None):
        try:
            if not self.form_canvas.winfo_exists():
                return
        except tk.TclError:
            return
        self.form_canvas.configure(scrollregion=self.form_canvas.bbox("all"))
        needs_scroll = self.form_body.winfo_reqheight() > self.form_canvas.winfo_height()
        if needs_scroll:
            self.form_scrollbar.grid()
        else:
            self.form_scrollbar.grid_remove()
            self.form_canvas.yview_moveto(0)

    def _run_form_scroll_sync(self):
        self._form_scroll_after_id = None
        self._sync_form_scrollregion()

    def _schedule_form_scroll_sync(self):
        if not self.winfo_exists():
            return
        if self._form_scroll_after_id is not None:
            try:
                self.after_cancel(self._form_scroll_after_id)
            except (tk.TclError, ValueError):
                pass
        self._form_scroll_after_id = self.after_idle(self._run_form_scroll_sync)

    def _on_dialog_destroy(self, event):
        if event.widget is not self:
            return
        for name in ("_form_scroll_after_id", "_preview_after_id", "_present_after_id"):
            callback = getattr(self, name)
            if callback is not None:
                self.after_cancel(callback)
                setattr(self, name, None)

    def _resize_form_canvas(self, event):
        self.form_canvas.itemconfigure(self._form_window_id, width=event.width)
        self._schedule_form_scroll_sync()

    def _on_form_mousewheel(self, event):
        if self.form_scrollbar.winfo_ismapped():
            self.form_canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
            return "break"
        return None

    def _bind_form_mousewheel(self, widget):
        widget.bind("<MouseWheel>", self._on_form_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_form_mousewheel(child)

    def toggle_rate_entry(self):
        if self.var_currency.get() == "USD":
            self.frame_rate.pack(
                fill="x",
                before=self.total_field_frame,
                pady=(0, SPACING["sm"]),
            )
        else:
            self.frame_rate.pack_forget()

    def _set_rate_input(self, rate):
        self.entry_rate.delete(0, "end")
        rate = safe_float(rate)
        if math.isfinite(rate) and rate > 0:
            self.entry_rate.insert(0, self._trade_input_number(rate))
        self._schedule_form_scroll_sync()

    @staticmethod
    def _parse_trade_number(value):
        value = value.strip()
        if "," in value:
            if re.fullmatch(r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+),\d+", value) is None:
                raise ValueError("Geçerli bir sayı girin (ör. 1.000,25).")
            value = value.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"[+-]?[1-9]\d{0,2}(?:\.\d{3})+", value):
            value = value.replace(".", "")
        try:
            number = float(value)
        except ValueError:
            raise ValueError("Geçerli bir sayı girin.") from None
        if not math.isfinite(number):
            raise ValueError("Geçerli bir sayı girin.")
        return number

    @staticmethod
    def _trade_input_number(value):
        text = str(value)
        if re.fullmatch(r"[+-]?[1-9]\d{0,2}\.\d{3}", text):
            return text.replace(".", ",")
        return text

    @staticmethod
    def _format_trade_money(value, currency="₺"):
        return f"{currency}{value:,.2f}".translate(str.maketrans(",.", ".,"))

    def _on_price_mode(self, _event=None):
        mode = self.var_price_mode.get()
        if mode == self._price_mode:
            return
        self._price_mode = mode
        try:
            quantity = self._parse_trade_number(self.var_amount.get())
            value = self._parse_trade_number(self.var_entered_total.get())
        except ValueError:
            return
        if quantity > 0:
            converted = value / quantity if mode == "Birim fiyat" else value * quantity
            if math.isfinite(converted):
                self.var_entered_total.set(self._trade_input_number(converted))

    def _build_trade_preview(self, parent):
        frame = tk.Frame(parent, bg=self.color_card_alt, padx=SPACING["sm"], pady=SPACING["xs"])
        frame.pack(fill="x", pady=(0, SPACING["xs"]))
        frame.grid_columnconfigure(1, weight=1)
        self.preview_total = tk.StringVar(self, value="—")
        self.preview_net = tk.StringVar(self, value="—")
        self.preview_cost = tk.StringVar(self, value="—")
        self.preview_error = tk.StringVar(self)
        for index, (label, variable) in enumerate((
            ("Brüt işlem tutarı", self.preview_total),
            ("Komisyon dâhil net tutar", self.preview_net),
            ("İşlem sonrası ort. maliyet", self.preview_cost),
        )):
            tk.Label(frame, text=label, bg=self.color_card_alt, fg=self.color_text_dim,
                     font=font(self, "small"), anchor="w").grid(row=index, column=0, sticky="w")
            tk.Label(frame, textvariable=variable, bg=self.color_card_alt, fg=self.color_text_main,
                     font=font(self, "small", "bold"), anchor="e").grid(row=index, column=1, sticky="e")
        tk.Label(parent, textvariable=self.preview_error, bg=self.color_card, fg=self.color_danger,
                 font=font(self, "small"), justify="left", anchor="w", wraplength=255).pack(fill="x")
        self.preview_history = tk.StringVar(self)
        tk.Label(parent, textvariable=self.preview_history, bg=self.color_card, fg=self.color_text_dim,
                 font=font(self, "small"), justify="left", anchor="w", wraplength=320).pack(fill="x")

    def _queue_trade_preview(self, *_args):
        if self._preview_after_id is not None:
            self.after_cancel(self._preview_after_id)
        self._preview_after_id = self.after(85, self._update_trade_preview)

    def _update_trade_preview(self):
        self._preview_after_id = None
        self._update_sell_available()
        if not self.quick:
            return
        self.preview_total.set("—")
        self.preview_net.set("—")
        self.preview_cost.set("—")
        self.preview_error.set("")
        self.preview_history.set("")
        if not self.var_amount.get().strip() or not self.var_entered_total.get().strip():
            return
        try:
            data = self._read_trade_form()
            if data["currency"] == "USD":
                total = self._format_trade_money(data["total_usd"], "$")
            else:
                total = self._format_trade_money(data["total_tl"])
            self.preview_total.set(total)
            net = data["total_tl"] + (data["fee_tl"] if data["action"] == "buy" else -data["fee_tl"])
            self.preview_net.set(self._format_trade_money(net))
            impact = self.manager.preview_trade(data, self.edit_index)
            average = impact["average_cost_tl"]
        except (OSError, ValueError) as error:
            self.preview_error.set(str(error))
            return
        if average is not None:
            unit = portfolio_unit(self.selected_instrument())
            self.preview_cost.set(f"{self._format_trade_money(average)} / {unit}")
        else:
            self.preview_cost.set("Pozisyon kapanır")
        if impact["historical"]:
            previous_cost = impact["previous_average_cost_tl"]
            previous = "—" if previous_cost is None else self._format_trade_money(previous_cost)
            realized = self._format_trade_money(impact["realized_change_tl"])
            self.preview_history.set(f"Geçmişe etkisi · Önceki ort. maliyet {previous} · Gerçekleşen K/Z değişimi {realized}")

    def _update_sell_available(self):
        instrument = self.selected_instrument()
        if self.var_action.get() != "sell" or instrument is None:
            self.sell_available_label.pack_forget()
            return
        try:
            quantity = self.manager.available_to_sell(instrument["key"], self.var_date.get(), self.edit_index)
        except ValueError:
            return
        amount = turkish_number(quantity, 8).rstrip("0").rstrip(",")
        self.sell_available.set(f"Satılabilir · {amount} {portfolio_unit(instrument)}")
        self.sell_available_label.pack(fill="x", pady=(SPACING["xxs"], 0))

    def _on_search_changed(self, *_args):
        self.load_list()

    def _transaction_row(self, source_index, raw_transaction):
        return self.transaction_row(self.manager, self.instrument_by_key, source_index, raw_transaction)

    @staticmethod
    def transaction_row(manager, instrument_by_key, source_index, raw_transaction):
        transaction = manager.normalize_transaction(raw_transaction)
        instrument = instrument_by_key.get(transaction["instrument_key"])
        label = (
            instrument["label"]
            if instrument
            else transaction.get("instrument_label", transaction["instrument_key"])
        )
        unit = portfolio_unit(instrument or {})
        action_text = "Alış" if transaction["action"] == "buy" else "Satış"
        quantity_text = f"{transaction['quantity']:.2f} {unit}"
        if transaction["currency"] == "USD":
            total_text = f"${transaction['total_usd']:.2f}"
            raw_total = transaction["total_usd"]
        else:
            total_text = f"₺{transaction['total_tl']:.2f}"
            raw_total = transaction["total_tl"]

        raw_quantity_text = f"{transaction['quantity']:.8f}".rstrip("0").rstrip(".")
        raw_total_text = f"{raw_total:.8f}".rstrip("0").rstrip(".")
        searchable = " ".join((
            str(transaction["date"]),
            action_text,
            transaction["action"],
            str(label),
            quantity_text,
            raw_quantity_text,
            total_text,
            raw_total_text,
        )).casefold()

        return {
            "source_index": source_index,
            "date": str(transaction["date"]),
            "action": action_text,
            "action_key": transaction["action"],
            "instrument_key": transaction["instrument_key"],
            "asset": str(label),
            "amount": quantity_text,
            "total": total_text,
            "searchable": searchable,
            "sort_values": {
                "date": PortfolioManagerDialog._sortable_date(transaction["date"]),
                "action": action_text.casefold(),
                "asset": str(label).casefold(),
                "amount": transaction["quantity"],
                "total": raw_total,
            },
        }

    @staticmethod
    def _sortable_date(value):
        return TransactionManager.parse_record_date(value) or datetime.min

    def cycle_sort(self, column):
        if column not in ("date", "action", "asset", "amount", "total"):
            return
        self.sort_direction = (self.sort_direction + 1) % 3 if self.sort_column == column else 1
        self.sort_column = column if self.sort_direction else None
        self.load_list()

    def _refresh_asset_filter_values(self):
        labels = []
        seen = set()

        for instrument in self.instruments:
            label = str(instrument.get("label", instrument.get("key", ""))).strip()
            if label and label not in seen:
                seen.add(label)
                labels.append(label)

        for index, raw_transaction in enumerate(self.manager.transactions):
            label = self._transaction_row(index, raw_transaction)["asset"]
            if label and label not in seen:
                seen.add(label)
                labels.append(label)

        values = (self.ALL_ASSETS_LABEL, *labels)
        self.combo_asset_filter.configure(values=values)
        if self.var_asset_filter.get() not in values:
            self.var_asset_filter.set(self.ALL_ASSETS_LABEL)

    def _filtered_transaction_rows(self):
        query = self.var_search.get().strip().casefold()
        normalized_query = query.replace(",", ".")
        selected_asset = self.var_asset_filter.get()
        rows = []

        for source_index, raw_transaction in enumerate(self.manager.transactions):
            row = self._transaction_row(source_index, raw_transaction)
            if selected_asset != self.ALL_ASSETS_LABEL and row["asset"] != selected_asset:
                continue
            if query:
                searchable = row["searchable"]
                normalized_searchable = searchable.replace(",", ".")
                if query not in searchable and normalized_query not in normalized_searchable:
                    continue
            rows.append(row)
        return rows

    def load_list(self):
        if self.quick:
            return
        self._refresh_asset_filter_values()
        rows = self._filtered_transaction_rows()
        if self.sort_column and self.sort_direction:
            rows.sort(key=lambda row: row["sort_values"][self.sort_column],
                      reverse=self.sort_direction == 2)
        self.tree.set_sort_indicator(self.sort_column, self.sort_direction)
        self.tree.set_rows(rows)

        total = len(self.manager.transactions)
        filters_active = bool(self.var_search.get().strip()) or (
            self.var_asset_filter.get() != self.ALL_ASSETS_LABEL
        )
        if filters_active:
            self.var_result_count.set(f"{len(rows)} / {total} işlem")
        else:
            self.var_result_count.set(f"Toplam {total} işlem")

    def on_click(self, event):
        """Compatibility dispatcher for callers that provide row metadata.

        ``TransactionTable`` binds its edit/delete buttons directly, but this
        method remains available for the dialog's historical callback API.
        """
        item_id = getattr(event, "source_index", None)
        action = getattr(event, "action", None)
        if item_id is None:
            return
        if action == "edit":
            self.start_edit(str(item_id))
        elif action == "delete":
            self.delete_transaction(str(item_id))

    def start_edit(self, item_id):
        idx = int(item_id)
        if idx < 0 or idx >= len(self.manager.transactions):
            return
        transaction = self.manager.normalize_transaction(self.manager.transactions[idx])
        key = transaction["instrument_key"]
        instrument = self.instrument_by_key.get(key)
        display = next((label for label, asset_key in self.instrument_label_to_key.items() if asset_key == key), None)
        if display is None:
            label = transaction["instrument_label"]
            instrument = instrument or {"key": key, "label": label}
            display = f"{label} · {key}" if label in self.instrument_label_to_key else label
            self.instrument_by_key[key] = instrument
            self.instrument_label_to_key[display] = key
            self.combo_instrument.configure(values=tuple(self.instrument_label_to_key))
        self.var_instrument.set(display)
        self.update_amount_label()

        self.entry_date.delete(0, 'end')
        self.entry_date.insert(0, transaction["date"])
        self.var_action.set(transaction["action"])
        self.entry_amount.delete(0, 'end')
        self.entry_amount.insert(0, self._trade_input_number(transaction["quantity"]))
        self.var_currency.set(transaction["currency"])
        self.toggle_rate_entry()
        self._price_mode = "Toplam tutar"
        self.var_price_mode.set(self._price_mode)
        self.entry_total.delete(0, 'end')
        if transaction["currency"] == "USD":
            self.entry_total.insert(0, self._trade_input_number(transaction["total_usd"]))
            self._set_rate_input(transaction["fx_rate"])
        else:
            self.entry_total.insert(0, self._trade_input_number(transaction["total_tl"]))
        fee = transaction["fee_tl"]
        self.var_fee.set(self._trade_input_number(fee) if math.isfinite(fee) and fee else "")

        self.edit_index = idx
        self.var_form_title.set("İşlemi düzenle" if self.quick else "İşlemi Düzenle")
        self.btn_save.config(text="Değişiklikleri kaydet" if self.quick else "Değişiklikleri Kaydet")
        self.mark_current_draft()
        self._queue_trade_preview()
        if not self.quick and not self.btn_cancel_edit.winfo_ismapped():
            self.btn_cancel_edit.pack(fill="x")

    def cancel_edit(self):
        self.edit_index = None
        self.var_form_title.set("İşlem ekle" if self.quick else "Yeni İşlem")
        self.btn_save.config(text="İşlemi kaydet" if self.quick else "+  İşlemi Ekle")
        self.btn_cancel_edit.pack_forget()
        self.clear_form()

    def clear_form(self):
        self.entry_amount.delete(0, 'end')
        self.entry_total.delete(0, 'end')
        self.var_fee.set("")
        self.entry_date.delete(0, 'end')
        self.entry_date.insert(0, datetime.now().strftime("%d-%m-%Y"))
        self.var_action.set("buy")
        self.var_currency.set("TL")
        self._price_mode = "Birim fiyat" if self.quick else "Toplam tutar"
        self.var_price_mode.set(self._price_mode)
        self._set_rate_input(self.current_dollar_rate)
        self.toggle_rate_entry()
        self.mark_current_draft()

    def delete_transaction(self, item_id):
        idx = int(item_id)
        if self.confirm_transaction_deletion(self, self.manager, self.instrument_by_key, idx):
            self.refresh_after_deletion(idx)
            self.on_save_callback()

    @staticmethod
    def confirm_transaction_deletion(parent, manager, instruments, index):
        if not 0 <= index < len(manager.transactions):
            return False
        transaction = manager.normalize_transaction(manager.transactions[index])
        instrument = instruments.get(transaction["instrument_key"], {})
        label = instrument.get("label", transaction["instrument_label"])
        action = "alış" if transaction["action"] == "buy" else "satış"
        prompt = f"{transaction['date']} tarihindeki {label} {action} kaydı silinecek. Onaylıyor musunuz?"
        if not messagebox.askyesno("İşlemi sil", prompt, parent=parent):
            return False
        try:
            manager.delete(index)
        except (OSError, ValueError) as error:
            messagebox.showerror("İşlem silinemedi", str(error), parent=parent)
            return False
        return True

    def refresh_after_deletion(self, index):
        if self.edit_index == index:
            self.cancel_edit()
        elif self.edit_index is not None and self.edit_index > index:
            self.edit_index -= 1
        self.load_list()
        self.update_amount_label()
        self._queue_trade_preview()

    def _read_trade_form(self):
        instrument = self.selected_instrument()
        if not instrument:
            raise ValueError("Lütfen bir varlık seçin.")
        date = self.manager.parse_record_date(self.var_date.get())
        if date is None:
            raise ValueError("İşlem tarihini gün-ay-yıl biçiminde girin veya takvimden seçin.")
        if date.date() > datetime.now().date():
            raise ValueError("İşlem tarihi gelecekte olamaz.")
        amount = self._parse_trade_number(self.var_amount.get())
        entered_price = self._parse_trade_number(self.var_entered_total.get())
        if amount <= 0 or entered_price < 0:
            raise ValueError("Miktar pozitif, tutar sıfır veya pozitif olmalı.")
        total = entered_price * amount if self.var_price_mode.get() == "Birim fiyat" else entered_price
        currency = self.var_currency.get()
        original = self.manager.transactions[self.edit_index] if self.edit_index is not None else {}
        original = original if isinstance(original, dict) else {}
        data = {
            **original,
            "date": date.strftime("%d-%m-%Y"),
            "instrument_key": instrument["key"],
            "instrument_label": instrument["label"],
            "action": self.var_action.get(),
            "quantity": amount,
            "currency": currency,
            "fee_tl": self._parse_trade_number(self.var_fee.get()) if self.var_fee.get().strip() else 0.0,
        }
        for field in ("symbol", "currency", "source", "decimals"):
            if instrument.get(field) is not None:
                data[f"instrument_{field}"] = instrument[field]
        if currency == "USD":
            try:
                rate = self._parse_trade_number(self.var_rate.get())
            except ValueError:
                rate = 0
            if rate <= 0:
                raise ValueError("USD işlemi için pozitif bir işlem kuru girin.")
            original_values = self.manager.normalize_transaction(original)
            if original_values["currency"] == "USD" and (original_values["total_usd"], original_values["fx_rate"]) == (total, rate):
                total_tl = original_values["total_tl"]
            else:
                total_tl = total * rate
            data.update(total_usd=total, fx_rate=rate, total_tl=total_tl)
        else:
            data.update(total_tl=total, total_usd=0, fx_rate=0)
        if not math.isfinite(data["total_tl"]):
            raise ValueError("İşlem tutarı çok büyük.")
        return data

    def save(self):
        try:
            data = self._read_trade_form()
            if self.edit_index is None:
                self.manager.save(data)
            else:
                self.manager.replace(self.edit_index, data)
        except ValueError as error:
            messagebox.showerror("İşlem kaydedilemedi", str(error), parent=self)
            return
        except OSError as error:
            messagebox.showerror("Kayıt hatası", str(error), parent=self)
            return

        if self.quick:
            self.destroy()
        else:
            self.cancel_edit()
            self.load_list()
            self.update_amount_label()
        self.on_save_callback()
