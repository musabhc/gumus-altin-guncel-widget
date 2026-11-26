"""A compact, themed date selector for trade entry."""

from calendar import Calendar
from datetime import date
import tkinter as tk

from theme import SPACING, THEME, font


MONTH_NAMES = (
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
)


class TradeCalendar(tk.Toplevel):
    def __init__(self, parent, selected, choose):
        super().__init__(parent)
        self.title("İşlem tarihi")
        self.transient(parent)
        self.resizable(False, False)
        self.configure(bg=THEME["background"], padx=SPACING["sm"], pady=SPACING["sm"])
        self.selected = selected
        self.visible_month = selected.replace(day=1)
        self.choose = choose
        self.bind("<Escape>", lambda _event: self.destroy())
        self._populate()
        self.focus_set()

    def _button(self, text, command, **options):
        return tk.Button(
            self, text=text, command=command, font=font(self, "small"), takefocus=True,
            bg=options.pop("bg", THEME["surface"]), fg=options.pop("fg", THEME["text_primary"]),
            activebackground=THEME["surface_hover"], activeforeground=THEME["text_primary"],
            relief="flat", bd=0, padx=SPACING["xs"], pady=SPACING["xxs"], **options,
        )

    def _populate(self):
        for child in self.winfo_children():
            child.destroy()
        month = self.visible_month
        earlier = tk.NORMAL if month.year > 1 or month.month > 1 else tk.DISABLED
        later = tk.NORMAL if month < date.today().replace(day=1) else tk.DISABLED
        self._button("‹", lambda: self._step(-1), state=earlier).grid(row=0, column=0, sticky="ew")
        tk.Label(
            self, text=f"{MONTH_NAMES[month.month - 1]} {month.year}", bg=THEME["background"],
            fg=THEME["text_primary"], font=font(self, "body", "bold"),
        ).grid(row=0, column=1, columnspan=5)
        self._button("›", lambda: self._step(1), state=later).grid(row=0, column=6, sticky="ew")
        for column, label in enumerate(("Pt", "Sa", "Ça", "Pe", "Cu", "Ct", "Pa")):
            tk.Label(self, text=label, bg=THEME["background"], fg=THEME["text_muted"],
                     font=font(self, "small")).grid(row=1, column=column, pady=(6, 3))
        for row, week in enumerate(Calendar().monthdayscalendar(month.year, month.month), start=2):
            for column, day in enumerate(week):
                if not day:
                    continue
                current = month.replace(day=day)
                selected = current == self.selected
                self._button(
                    str(day), lambda value=current: self._pick(value), width=3,
                    bg=THEME["primary"] if selected else THEME["surface"],
                    fg=THEME["primary_ink"] if selected else THEME["text_primary"],
                    state=tk.NORMAL if current <= date.today() else tk.DISABLED,
                ).grid(row=row, column=column, padx=1, pady=1, sticky="ew")

    def _step(self, direction):
        month_index = (self.visible_month.year * 12 + self.visible_month.month - 1) + direction
        year, month = divmod(month_index, 12)
        self.visible_month = date(year, month + 1, 1)
        self._populate()

    def _pick(self, selected):
        self.choose(selected)
        self.destroy()
