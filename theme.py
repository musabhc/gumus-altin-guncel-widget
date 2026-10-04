"""Shared visual tokens and ttk styles for the Tkinter user interface.

The module intentionally contains presentation concerns only.  Business logic
and persistence stay in :mod:`main`.
"""

from __future__ import annotations

import tkinter.font as tkfont
from tkinter import ttk


THEME = {
    "background": "#08111F",
    "surface": "#0D1828",
    "surface_alt": "#121F31",
    "surface_hover": "#17263A",
    "surface_pressed": "#1B2D45",
    "input": "#101D2F",
    "border": "#223249",
    "border_strong": "#30445F",
    "text_primary": "#F4F7FB",
    "text_secondary": "#A7B4C8",
    "text_muted": "#6F8098",
    "primary": "#3B82F6",
    "primary_hover": "#5592F7",
    "primary_pressed": "#2F6FD6",
    "primary_soft": "#102A52",
    "positive": "#35D07F",
    "positive_soft": "#0E3428",
    "negative": "#F25A68",
    "negative_soft": "#3A1720",
    "gold": "#F5B82E",
    "silver": "#C8D4E3",
    "white": "#FFFFFF",
}


SPACING = {
    "xxs": 4,
    "xs": 8,
    "sm": 12,
    "md": 16,
    "lg": 20,
    "xl": 24,
    "xxl": 32,
}


TYPOGRAPHY = {
    "caption": 8,
    "small": 9,
    "body": 10,
    "body_large": 11,
    "section": 14,
    "title": 17,
    "value": 25,
}


def resolve_font_family(widget) -> str:
    """Return the best Segoe UI family available on the current system."""

    try:
        available = set(tkfont.families(widget))
    except Exception:
        available = set()
    for family in ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"):
        if family in available:
            return family
    return "TkDefaultFont"


def font(widget, role="body", weight="normal"):
    """Build a font tuple from the central typography scale."""

    return (resolve_font_family(widget), TYPOGRAPHY.get(role, role), weight)


def configure_ttk_styles(widget):
    """Configure the small set of ttk controls still used by the application."""

    family = resolve_font_family(widget)
    style = ttk.Style(widget)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(
        "Modern.TCombobox",
        background=THEME["input"],
        fieldbackground=THEME["input"],
        foreground=THEME["text_primary"],
        arrowcolor=THEME["text_secondary"],
        bordercolor=THEME["border"],
        lightcolor=THEME["border"],
        darkcolor=THEME["border"],
        insertcolor=THEME["text_primary"],
        padding=(10, 7),
        relief="flat",
        font=(family, TYPOGRAPHY["body"]),
    )
    style.map(
        "Modern.TCombobox",
        fieldbackground=[("readonly", THEME["input"]), ("focus", THEME["input"])],
        foreground=[("readonly", THEME["text_primary"]), ("disabled", THEME["text_muted"])],
        bordercolor=[("focus", THEME["primary"]), ("!focus", THEME["border"])],
        lightcolor=[("focus", THEME["primary"]), ("!focus", THEME["border"])],
        darkcolor=[("focus", THEME["primary"]), ("!focus", THEME["border"])],
        arrowcolor=[("disabled", THEME["text_muted"]), ("!disabled", THEME["text_secondary"])],
    )

    style.configure(
        "Modern.Vertical.TScrollbar",
        troughcolor=THEME["surface"],
        background=THEME["border_strong"],
        bordercolor=THEME["surface"],
        lightcolor=THEME["border_strong"],
        darkcolor=THEME["border_strong"],
        arrowcolor=THEME["text_muted"],
        relief="flat",
        width=10,
    )
    style.map(
        "Modern.Vertical.TScrollbar",
        background=[("active", THEME["text_muted"]), ("!active", THEME["border_strong"])],
    )

    # Treeview remains useful in the watchlist dialog.  The transaction table
    # uses a custom frame-based component so it can display per-cell badges.
    style.configure(
        "Modern.Treeview",
        background=THEME["surface"],
        foreground=THEME["text_primary"],
        fieldbackground=THEME["surface"],
        borderwidth=0,
        bordercolor=THEME["border"],
        lightcolor=THEME["border"],
        darkcolor=THEME["border"],
        rowheight=34,
        relief="flat",
        font=(family, TYPOGRAPHY["body"]),
    )
    style.configure(
        "Modern.Treeview.Heading",
        background=THEME["surface_alt"],
        foreground=THEME["text_secondary"],
        borderwidth=0,
        relief="flat",
        padding=(8, 9),
        font=(family, TYPOGRAPHY["small"], "bold"),
    )
    style.map(
        "Modern.Treeview",
        background=[("selected", THEME["primary_soft"])],
        foreground=[("selected", THEME["text_primary"])],
    )

    widget.option_add("*TCombobox*Listbox.background", THEME["surface_alt"])
    widget.option_add("*TCombobox*Listbox.foreground", THEME["text_primary"])
    widget.option_add("*TCombobox*Listbox.selectBackground", THEME["primary"])
    widget.option_add("*TCombobox*Listbox.selectForeground", THEME["white"])
    return style
