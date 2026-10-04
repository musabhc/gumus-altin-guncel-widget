"""Small reusable Tkinter components used by the presentation layer."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from theme import SPACING, THEME, font


class HoverButton(tk.Button):
    """Flat button with inexpensive hover/pressed/disabled states."""

    def __init__(
        self,
        master,
        *,
        bg=THEME["primary"],
        hover_bg=THEME["primary_hover"],
        pressed_bg=THEME["primary_pressed"],
        disabled_bg=THEME["border"],
        fg=THEME["white"],
        disabled_fg=THEME["text_muted"],
        **kwargs,
    ):
        self._normal_bg = bg
        self._hover_bg = hover_bg
        self._pressed_bg = pressed_bg
        self._disabled_bg = disabled_bg
        self._normal_fg = fg
        self._disabled_fg = disabled_fg
        super().__init__(
            master,
            bg=bg,
            fg=fg,
            activebackground=pressed_bg,
            activeforeground=fg,
            disabledforeground=disabled_fg,
            relief="flat",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            font=font(master, "body", "bold"),
            **kwargs,
        )
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")

    def _enabled(self):
        return str(self.cget("state")) != "disabled"

    def _on_enter(self, _event=None):
        if self._enabled():
            super().configure(bg=self._hover_bg)

    def _on_leave(self, _event=None):
        super().configure(
            bg=self._normal_bg if self._enabled() else self._disabled_bg,
            fg=self._normal_fg if self._enabled() else self._disabled_fg,
        )

    def _on_press(self, _event=None):
        if self._enabled():
            super().configure(bg=self._pressed_bg)

    def _on_release(self, event=None):
        if not self._enabled():
            return
        inside = event is None or (
            0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        )
        super().configure(bg=self._hover_bg if inside else self._normal_bg)

    def configure(self, cnf=None, **kwargs):
        result = super().configure(cnf, **kwargs)
        if hasattr(self, "_normal_bg") and "state" in kwargs:
            self._on_leave()
            super().configure(cursor="arrow" if kwargs["state"] == "disabled" else "hand2")
        return result

    config = configure


class ModernEntry(tk.Frame):
    """Entry wrapped in a one-pixel focus border.

    Common Entry methods are proxied so existing form logic can keep using the
    original ``get/delete/insert`` contract.
    """

    def __init__(self, master, *, textvariable=None, justify="left", **entry_kwargs):
        super().__init__(
            master,
            bg=THEME["input"],
            highlightthickness=1,
            highlightbackground=THEME["border"],
            highlightcolor=THEME["primary"],
            bd=0,
        )
        self.entry = tk.Entry(
            self,
            textvariable=textvariable,
            justify=justify,
            bg=THEME["input"],
            fg=THEME["text_primary"],
            insertbackground=THEME["text_primary"],
            disabledbackground=THEME["surface_alt"],
            disabledforeground=THEME["text_muted"],
            selectbackground=THEME["primary"],
            selectforeground=THEME["white"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=font(master, "body"),
            **entry_kwargs,
        )
        self.entry.pack(fill="both", expand=True, padx=10, pady=8)
        self.entry.bind("<FocusIn>", lambda _e: self.configure(highlightbackground=THEME["primary"]), add="+")
        self.entry.bind("<FocusOut>", lambda _e: self.configure(highlightbackground=THEME["border"]), add="+")

    def get(self):
        return self.entry.get()

    def delete(self, first, last=None):
        return self.entry.delete(first, last)

    def insert(self, index, string):
        return self.entry.insert(index, string)

    def focus_set(self):
        return self.entry.focus_set()

    def selection_range(self, start, end):
        return self.entry.selection_range(start, end)

    def icursor(self, index):
        return self.entry.icursor(index)

    def bind_entry(self, sequence=None, func=None, add=None):
        return self.entry.bind(sequence, func, add)

    def set_state(self, state):
        self.entry.configure(state=state)


class SearchEntry(tk.Frame):
    """Search field with a non-destructive placeholder overlay."""

    def __init__(self, master, variable, placeholder="İşlem ara..."):
        super().__init__(
            master,
            bg=THEME["input"],
            highlightthickness=1,
            highlightbackground=THEME["border"],
            bd=0,
        )
        self.variable = variable
        self.icon = tk.Canvas(
            self,
            width=20,
            height=20,
            bg=THEME["input"],
            highlightthickness=0,
            bd=0,
        )
        self.icon.pack(side="left", padx=(9, 2))
        self.icon.create_oval(5, 4, 13, 12, outline=THEME["text_muted"], width=1.5)
        self.icon.create_line(12, 11, 16, 15, fill=THEME["text_muted"], width=1.5)
        self.entry = tk.Entry(
            self,
            textvariable=variable,
            bg=THEME["input"],
            fg=THEME["text_primary"],
            insertbackground=THEME["text_primary"],
            selectbackground=THEME["primary"],
            selectforeground=THEME["white"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=font(master, "body"),
        )
        self.entry.pack(side="left", fill="both", expand=True, padx=(1, 9), pady=8)
        self.placeholder = tk.Label(
            self,
            text=placeholder,
            bg=THEME["input"],
            fg=THEME["text_muted"],
            font=font(master, "body"),
            cursor="xterm",
        )
        self.placeholder.place(x=34, rely=0.5, anchor="w")
        self.placeholder.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", self._focus_in, add="+")
        self.entry.bind("<FocusOut>", self._focus_out, add="+")
        self._trace_id = variable.trace_add("write", self._sync_placeholder)
        self._sync_placeholder()

    def _focus_in(self, _event=None):
        self.configure(highlightbackground=THEME["primary"])
        self.placeholder.place_forget()

    def _focus_out(self, _event=None):
        self.configure(highlightbackground=THEME["border"])
        self._sync_placeholder()

    def _sync_placeholder(self, *_args):
        if self.variable.get() or self.entry.focus_get() == self.entry:
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=34, rely=0.5, anchor="w")


class SegmentedControl(tk.Frame):
    """Button-based segmented control backed by an existing Tk variable."""

    def __init__(self, master, variable, options, command=None, selection_colors=None):
        super().__init__(
            master,
            bg=THEME["border"],
            highlightthickness=1,
            highlightbackground=THEME["border"],
            bd=0,
        )
        self.variable = variable
        self.command = command
        self.options = list(options)
        self.selection_colors = selection_colors or {}
        self.buttons = {}
        for column, (label, value) in enumerate(self.options):
            self.grid_columnconfigure(column, weight=1, uniform="segment")
            button = tk.Button(
                self,
                text=label,
                command=lambda selected=value: self.select(selected),
                bg=THEME["input"],
                fg=THEME["text_secondary"],
                activebackground=THEME["surface_pressed"],
                activeforeground=THEME["text_primary"],
                relief="flat",
                bd=0,
                highlightthickness=0,
                cursor="hand2",
                font=font(master, "body", "bold"),
                padx=8,
                pady=8,
            )
            button.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 1, 0))
            button.bind("<Enter>", lambda _e, b=button, v=value: self._hover(b, v, True), add="+")
            button.bind("<Leave>", lambda _e, b=button, v=value: self._hover(b, v, False), add="+")
            self.buttons[value] = button
        self._trace_id = variable.trace_add("write", self._render)
        self._render()

    def select(self, value):
        self.variable.set(value)
        if self.command:
            self.command()

    def _selected_colors(self, value):
        return self.selection_colors.get(
            value,
            (THEME["primary_soft"], THEME["primary"]),
        )

    def _hover(self, button, value, entered):
        if value == self.variable.get():
            return
        button.configure(bg=THEME["surface_hover"] if entered else THEME["input"])

    def _render(self, *_args):
        selected = self.variable.get()
        for value, button in self.buttons.items():
            if value == selected:
                bg, fg = self._selected_colors(value)
                button.configure(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
            else:
                button.configure(
                    bg=THEME["input"],
                    fg=THEME["text_secondary"],
                    activebackground=THEME["surface_hover"],
                    activeforeground=THEME["text_primary"],
                )


class IconButton(tk.Canvas):
    """Small dependency-free line-icon button drawn on a Canvas."""

    def __init__(
        self,
        master,
        icon,
        command,
        *,
        size=30,
        bg=THEME["surface_alt"],
        hover_bg=THEME["surface_hover"],
        fg=THEME["text_secondary"],
        hover_fg=THEME["text_primary"],
        danger=False,
        show_border=True,
    ):
        super().__init__(
            master,
            width=size,
            height=size,
            bg=bg,
            highlightthickness=1 if show_border else 0,
            highlightbackground=THEME["border"],
            bd=0,
            cursor="hand2",
            takefocus=1,
        )
        self.icon = icon
        self.command = command
        self.size = size
        self.normal_bg = bg
        self.hover_bg = THEME["negative_soft"] if danger else hover_bg
        self.normal_fg = THEME["negative"] if danger else fg
        self.hover_fg = THEME["negative"] if danger else hover_fg
        self._hovered = False
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())
        self.bind("<Configure>", lambda _e: self._draw(), add="+")
        self._draw()

    def invoke(self):
        if self.command:
            self.command()

    def _click(self, _event=None):
        self.invoke()
        return "break"

    def _enter(self, _event=None):
        self._hovered = True
        self.configure(bg=self.hover_bg)
        self._draw()

    def _leave(self, _event=None):
        self._hovered = False
        self.configure(bg=self.normal_bg)
        self._draw()

    def _draw(self):
        self.delete("icon")
        color = self.hover_fg if self._hovered else self.normal_fg
        s = max(16, min(self.winfo_width(), self.winfo_height()))
        c = s / 2
        w = 1.6
        tag = "icon"
        if self.icon == "edit":
            self.create_line(c - 5, c + 5, c + 5, c - 5, fill=color, width=w, tags=tag)
            self.create_line(c - 6, c + 2, c - 6, c + 6, c - 2, c + 6, fill=color, width=w, tags=tag)
            self.create_line(c + 3, c - 6, c + 6, c - 3, fill=color, width=w, tags=tag)
        elif self.icon == "delete":
            self.create_rectangle(c - 5, c - 3, c + 5, c + 7, outline=color, width=w, tags=tag)
            self.create_line(c - 7, c - 5, c + 7, c - 5, fill=color, width=w, tags=tag)
            self.create_line(c - 2, c - 7, c + 2, c - 7, fill=color, width=w, tags=tag)
            self.create_line(c - 2, c, c - 2, c + 4, fill=color, width=w, tags=tag)
            self.create_line(c + 2, c, c + 2, c + 4, fill=color, width=w, tags=tag)
        elif self.icon == "plus":
            self.create_line(c - 6, c, c + 6, c, fill=color, width=2, tags=tag)
            self.create_line(c, c - 6, c, c + 6, fill=color, width=2, tags=tag)
        elif self.icon == "refresh":
            self.create_arc(c - 7, c - 7, c + 7, c + 7, start=35, extent=275, style="arc", outline=color, width=w, tags=tag)
            self.create_line(c + 5, c - 7, c + 8, c - 7, c + 8, c - 3, fill=color, width=w, tags=tag)
        elif self.icon == "import":
            self.create_rectangle(c - 6, c + 1, c + 6, c + 7, outline=color, width=w, tags=tag)
            self.create_line(c, c - 7, c, c + 3, fill=color, width=w, tags=tag)
            self.create_line(c - 4, c - 1, c, c + 3, c + 4, c - 1, fill=color, width=w, tags=tag)
        elif self.icon == "settings":
            self.create_oval(c - 6, c - 6, c + 6, c + 6, outline=color, width=w, tags=tag)
            self.create_oval(c - 2, c - 2, c + 2, c + 2, outline=color, width=w, tags=tag)
            for dx, dy in ((0, -9), (0, 9), (-9, 0), (9, 0)):
                self.create_line(c + dx * .65, c + dy * .65, c + dx, c + dy, fill=color, width=w, tags=tag)
        elif self.icon == "chart":
            self.create_line(c - 7, c + 7, c - 7, c - 6, fill=color, width=w, tags=tag)
            self.create_line(c - 7, c + 7, c + 7, c + 7, fill=color, width=w, tags=tag)
            self.create_line(c - 4, c + 3, c - 1, c - 1, c + 2, c + 1, c + 6, c - 5, fill=color, width=w, tags=tag)
        else:
            self.create_oval(c - 5, c - 5, c + 5, c + 5, outline=color, width=w, tags=tag)


class TransactionTable(tk.Frame):
    """Responsive, scrollable transaction table with real action badges."""

    COLUMN_CONFIG = (
        ("date", "Tarih", 92, 1),
        ("action", "İşlem", 78, 0),
        ("asset", "Varlık", 112, 2),
        ("amount", "Miktar", 106, 1),
        ("total", "Toplam", 108, 1),
        ("actions", "", 72, 0),
    )

    def __init__(self, master, on_edit, on_delete):
        super().__init__(master, bg=THEME["surface"], bd=0)
        self.on_edit = on_edit
        self.on_delete = on_delete
        self.rows = []
        self._row_widgets = {}
        self._selected_index = None

        self.header = tk.Frame(self, bg=THEME["surface_alt"], height=38)
        self.header.pack(fill="x")
        self.header.grid_propagate(False)
        self._configure_columns(self.header)
        for column, (_key, title, _minsize, _weight) in enumerate(self.COLUMN_CONFIG):
            tk.Label(
                self.header,
                text=title,
                bg=THEME["surface_alt"],
                fg=THEME["text_secondary"],
                font=font(master, "small", "bold"),
                anchor="w" if column in (0, 2) else "center",
            ).grid(row=0, column=column, sticky="nsew", padx=(12 if column == 0 else 6))

        body = tk.Frame(self, bg=THEME["surface"])
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg=THEME["surface"], highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(
            body,
            orient="vertical",
            command=self.canvas.yview,
            style="Modern.Vertical.TScrollbar",
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=THEME["surface"])
        self._window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._sync_scrollregion)
        self.canvas.bind("<Configure>", self._sync_width)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(1, "units"))
        self.canvas.bind("<Up>", lambda _e: self._move_selection(-1))
        self.canvas.bind("<Down>", lambda _e: self._move_selection(1))
        self.canvas.bind("<Return>", lambda _e: self._invoke_selected(self.on_edit))
        self.canvas.bind("<Delete>", lambda _e: self._invoke_selected(self.on_delete))

    def _configure_columns(self, frame):
        for column, (_key, _title, minsize, weight) in enumerate(self.COLUMN_CONFIG):
            frame.grid_columnconfigure(column, minsize=minsize, weight=weight)
        frame.grid_rowconfigure(0, weight=1)

    def _sync_scrollregion(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_width(self, event):
        self.canvas.itemconfigure(self._window_id, width=event.width)

    def _on_mousewheel(self, event):
        delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta * 3, "units")
        return "break"

    def _bind_wheel(self, widget):
        widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        widget.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-1, "units"), add="+")
        widget.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(1, "units"), add="+")

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.rows = []
        self._row_widgets = {}
        self._selected_index = None

    def set_rows(self, rows):
        previous_selection = self._selected_index
        self.clear()
        self.rows = list(rows)
        if not self.rows:
            empty = tk.Label(
                self.inner,
                text="Bu filtreyle eşleşen işlem bulunamadı.",
                bg=THEME["surface"],
                fg=THEME["text_muted"],
                font=font(self, "body"),
                pady=28,
            )
            empty.pack(fill="x")
            self._bind_wheel(empty)
            return

        for display_index, row_data in enumerate(self.rows):
            source_index = row_data["source_index"]
            base_bg = THEME["surface"] if display_index % 2 == 0 else "#0F1B2C"
            row = tk.Frame(
                self.inner,
                bg=base_bg,
                height=40,
                highlightthickness=0,
                bd=0,
                cursor="hand2",
                takefocus=1,
            )
            row.pack(fill="x")
            row.grid_propagate(False)
            self._configure_columns(row)
            normal_widgets = []

            def add_label(column, text, anchor="center", color=THEME["text_primary"]):
                label = tk.Label(
                    row,
                    text=text,
                    bg=base_bg,
                    fg=color,
                    font=font(self, "body"),
                    anchor=anchor,
                )
                label.grid(row=0, column=column, sticky="nsew", padx=(12 if column == 0 else 6))
                normal_widgets.append(label)
                return label

            add_label(0, row_data["date"], "w", THEME["text_secondary"])
            badge_cell = tk.Frame(row, bg=base_bg)
            badge_cell.grid(row=0, column=1, sticky="nsew")
            normal_widgets.append(badge_cell)
            is_buy = row_data["action_key"] == "buy"
            badge = tk.Label(
                badge_cell,
                text=row_data["action"],
                bg=THEME["positive_soft"] if is_buy else THEME["negative_soft"],
                fg=THEME["positive"] if is_buy else THEME["negative"],
                font=font(self, "small", "bold"),
                padx=8,
                pady=3,
            )
            badge.place(relx=.5, rely=.5, anchor="center")
            add_label(2, row_data["asset"], "w")
            add_label(3, row_data["amount"], "e", THEME["text_secondary"])
            add_label(4, row_data["total"], "e")

            action_cell = tk.Frame(row, bg=base_bg)
            action_cell.grid(row=0, column=5, sticky="nsew")
            normal_widgets.append(action_cell)
            delete_button = IconButton(
                action_cell,
                "delete",
                lambda idx=source_index: self.on_delete(str(idx)),
                size=24,
                bg=base_bg,
                show_border=False,
                danger=True,
            )
            delete_button.pack(side="right", padx=(1, 7), pady=7)
            edit_button = IconButton(
                action_cell,
                "edit",
                lambda idx=source_index: self.on_edit(str(idx)),
                size=24,
                bg=base_bg,
                show_border=False,
            )
            edit_button.pack(side="right", padx=1, pady=7)

            row_info = {
                "frame": row,
                "normal_widgets": normal_widgets,
                "badge": badge,
                "edit": edit_button,
                "delete": delete_button,
                "base_bg": base_bg,
            }
            self._row_widgets[source_index] = row_info
            for widget in [row, *normal_widgets]:
                widget.bind("<Button-1>", lambda _e, idx=source_index: self.select(idx), add="+")
                widget.bind("<Enter>", lambda _e, idx=source_index: self._set_hover(idx, True), add="+")
                widget.bind("<Leave>", lambda _e, idx=source_index: self._set_hover(idx, False), add="+")
                self._bind_wheel(widget)
            self._bind_wheel(badge)
            self._bind_wheel(edit_button)
            self._bind_wheel(delete_button)

        visible_indices = {row["source_index"] for row in self.rows}
        if previous_selection in visible_indices:
            self.select(previous_selection)
        self.canvas.yview_moveto(0)

    def _set_row_bg(self, source_index, color):
        info = self._row_widgets.get(source_index)
        if not info:
            return
        info["frame"].configure(bg=color)
        for widget in info["normal_widgets"]:
            widget.configure(bg=color)
        info["edit"].normal_bg = color
        info["delete"].normal_bg = color
        if not info["edit"]._hovered:
            info["edit"].configure(bg=color)
        if not info["delete"]._hovered:
            info["delete"].configure(bg=color)

    def _set_hover(self, source_index, entered):
        if source_index == self._selected_index:
            return
        info = self._row_widgets.get(source_index)
        if info:
            self._set_row_bg(source_index, THEME["surface_hover"] if entered else info["base_bg"])

    def select(self, source_index):
        if self._selected_index in self._row_widgets:
            old = self._row_widgets[self._selected_index]
            self._set_row_bg(self._selected_index, old["base_bg"])
        self._selected_index = source_index
        self._set_row_bg(source_index, THEME["primary_soft"])
        self.canvas.focus_set()

    def _move_selection(self, direction):
        if not self.rows:
            return "break"
        indices = [row["source_index"] for row in self.rows]
        if self._selected_index not in indices:
            target = 0 if direction > 0 else len(indices) - 1
        else:
            target = max(0, min(len(indices) - 1, indices.index(self._selected_index) + direction))
        self.select(indices[target])
        return "break"

    def _invoke_selected(self, callback):
        if self._selected_index is not None:
            callback(str(self._selected_index))
        return "break"
