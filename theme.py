"""Shared visual tokens and ttk styles for the Tkinter user interface.

The module intentionally contains presentation concerns only.  Business logic
and persistence stay in :mod:`main`.
"""

from __future__ import annotations

import tkinter.font as tkfont
import ctypes
import sys
from tkinter import TclError, ttk


def enable_high_dpi():
    """Opt into native Windows display resolution before creating any HWNDs."""
    if sys.platform != "win32":
        return
    try:
        # System awareness avoids bitmap virtualization and works with Tk 8.6's
        # process-wide font metrics when moving between different displays.
        if ctypes.windll.shcore.SetProcessDpiAwareness(1) == 0:
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


THEME = {
    "background": "#101415",
    "surface": "#171d1e",
    "surface_alt": "#1d2526",
    "surface_stripe": "#1a2122",
    "surface_hover": "#25302d",
    "surface_pressed": "#2b3734",
    "input": "#101415",
    "border": "#2a3435",
    "border_strong": "#40534d",
    "text_primary": "#eff4f2",
    "text_secondary": "#a0aeac",
    "text_muted": "#82928f",
    "primary": "#7fe5b4",
    "primary_hover": "#a0efc7",
    "primary_pressed": "#65c89a",
    "primary_soft": "#22302b",
    "primary_ink": "#11281d",
    "positive": "#7fe5b4",
    "positive_soft": "#243b30",
    "negative": "#f19a9b",
    "negative_soft": "#402d30",
    "gold": "#e6c587",
    "silver": "#bacfe5",
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

_TURKISH_SEPARATORS = str.maketrans(",.", ".,")


def turkish_number(value, decimal_places=2):
    return f"{value:,.{decimal_places}f}".translate(_TURKISH_SEPARATORS)


def resolve_font_family(widget) -> str:
    """Return the best Segoe UI family available on the current system."""

    root = widget._root()
    cached = getattr(root, "_finans_font_family", None)
    if cached is not None:
        return cached
    try:
        available = set(tkfont.families(root))
        family = next(
            (name for name in ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI") if name in available),
            None,
        )
        if family is None:
            family = tkfont.nametofont("TkDefaultFont", root=root).actual("family")
    except (RuntimeError, TclError):
        return "TkDefaultFont"
    root._finans_font_family = family
    return family


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
    widget.option_add("*TCombobox*Listbox.selectForeground", THEME["primary_ink"])
    return style
