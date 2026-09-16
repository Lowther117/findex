"""Light and dark palettes, and the ttk styling that goes with them."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# Two palettes. The rule for both: anything you can type in or click is a
# different shade from the surface behind it and carries a visible border, so
# you can tell an input from a label without hunting. Dark is the default.
LIGHT = dict(
    bg="#f1f3f6", panel="#ffffff", sidebar="#e7eaef", text="#14171c",
    dim="#596273", border="#ccd2dc", accent="#1f6feb", accent_text="#ffffff",
    field="#ffffff", field_text="#14171c", field_border="#a9b2c0",
    sel="#d6e4ff", good="#146c2e", warn="#8a5a00", bad="#b42318",
    code="#f5f7fa", hint="#eef4ff",
)
DARK = dict(
    bg="#14161b", panel="#1c1f27", sidebar="#101218", text="#e9ebf0",
    dim="#98a2b3", border="#333b49", accent="#5b9cff", accent_text="#0a0c10",
    field="#0a0c11", field_text="#f2f4f8", field_border="#49536485",
    sel="#24344d", good="#4ac26b", warn="#e0b44a", bad="#ff6f61",
    code="#0f1116", hint="#182339",
)
DARK["field_border"] = "#495364"


def mono_family(root):
    families = set(tkfont.families(root))
    for name in ("Cascadia Mono", "Consolas", "SF Mono", "Menlo",
                 "DejaVu Sans Mono", "Liberation Mono", "Courier New"):
        if name in families:
            return name
    return "TkFixedFont"


def ui_family(root):
    families = set(tkfont.families(root))
    for name in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter",
                 "DejaVu Sans", "Arial"):
        if name in families:
            return name
    return "TkDefaultFont"


def apply(root: tk.Misc, dark: bool) -> dict:
    c = DARK if dark else LIGHT
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    ui = ui_family(root)
    root.configure(bg=c["bg"])

    style.configure(".", background=c["bg"], foreground=c["text"],
                    fieldbackground=c["field"], bordercolor=c["border"],
                    font=(ui, 10))
    style.configure("TFrame", background=c["bg"])
    style.configure("Panel.TFrame", background=c["panel"])
    style.configure("Side.TFrame", background=c["sidebar"])
    style.configure("TLabel", background=c["bg"], foreground=c["text"])
    style.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
    style.configure("Dim.TLabel", background=c["bg"], foreground=c["dim"])
    style.configure("Field.TLabel", background=c["bg"], foreground=c["text"],
                    font=(ui, 9, "bold"))
    style.configure("Hint.TLabel", background=c["hint"], foreground=c["text"],
                    padding=(10, 6))
    style.configure("Hint.TFrame", background=c["hint"])
    style.configure("Hint.TButton", background=c["accent"], foreground=c["accent_text"],
                    padding=(9, 3), font=(ui, 9, "bold"))
    style.map("Hint.TButton", background=[("active", c["accent"])])
    style.configure("PanelDim.TLabel", background=c["panel"], foreground=c["dim"])
    style.configure("Title.TLabel", background=c["bg"], foreground=c["text"],
                    font=(ui, 15, "bold"))
    style.configure("Sub.TLabel", background=c["bg"], foreground=c["dim"], font=(ui, 10))
    style.configure("Good.TLabel", background=c["bg"], foreground=c["good"])
    style.configure("Warn.TLabel", background=c["bg"], foreground=c["warn"])
    style.configure("Bad.TLabel", background=c["bg"], foreground=c["bad"])

    style.configure("TButton", background=c["panel"], foreground=c["text"],
                    bordercolor=c["field_border"], lightcolor=c["field_border"],
                    darkcolor=c["field_border"], focuscolor=c["accent"],
                    padding=(11, 6), relief="solid")
    style.map("TButton",
              background=[("active", c["sel"]), ("pressed", c["sel"])],
              bordercolor=[("active", c["accent"]), ("focus", c["accent"])],
              lightcolor=[("active", c["accent"])],
              darkcolor=[("active", c["accent"])],
              foreground=[("disabled", c["dim"])])
    style.configure("Accent.TButton", background=c["accent"], foreground=c["accent_text"],
                    padding=(14, 6), font=(ui, 10, "bold"))
    style.map("Accent.TButton",
              background=[("disabled", c["border"]), ("active", c["accent"]),
                          ("pressed", c["accent"])],
              foreground=[("disabled", c["dim"])])

    # Entries: darker (or whiter) than the panel, a real border, and an accent
    # ring when focused so you always know where the caret is.
    style.configure("TEntry", fieldbackground=c["field"], foreground=c["field_text"],
                    insertcolor=c["accent"], bordercolor=c["field_border"],
                    lightcolor=c["field_border"], darkcolor=c["field_border"],
                    padding=5, relief="solid")
    style.map("TEntry",
              bordercolor=[("focus", c["accent"])],
              lightcolor=[("focus", c["accent"])],
              darkcolor=[("focus", c["accent"])],
              fieldbackground=[("readonly", c["panel"]), ("disabled", c["bg"])],
              foreground=[("disabled", c["dim"])])
    style.configure("TCombobox", fieldbackground=c["field"], foreground=c["field_text"],
                    background=c["panel"], arrowcolor=c["accent"],
                    bordercolor=c["field_border"], lightcolor=c["field_border"],
                    darkcolor=c["field_border"], padding=4, relief="solid")
    # A read-only combobox draws its text as a *selection*, so without these it
    # comes out grey-on-grey and unreadable in dark mode.
    style.map("TCombobox",
              fieldbackground=[("readonly", c["field"]), ("disabled", c["bg"])],
              foreground=[("readonly", c["field_text"]), ("disabled", c["dim"])],
              selectbackground=[("readonly", c["field"]), ("focus", c["field"])],
              selectforeground=[("readonly", c["field_text"]), ("focus", c["field_text"])],
              background=[("active", c["panel"])],
              bordercolor=[("focus", c["accent"]), ("hover", c["accent"])],
              lightcolor=[("focus", c["accent"])],
              darkcolor=[("focus", c["accent"])],
              arrowcolor=[("readonly", c["accent"])])
    root.option_add("*TCombobox*Listbox.background", c["field"])
    root.option_add("*TCombobox*Listbox.foreground", c["field_text"])
    root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", c["accent_text"])
    style.configure("TCheckbutton", background=c["bg"], foreground=c["text"])
    style.map("TCheckbutton", background=[("active", c["bg"])])
    style.configure("Panel.TCheckbutton", background=c["panel"], foreground=c["text"])

    style.configure("Treeview", background=c["panel"], fieldbackground=c["panel"],
                    foreground=c["text"], bordercolor=c["border"], rowheight=23,
                    font=(ui, 10))
    style.map("Treeview", background=[("selected", c["accent"])],
              foreground=[("selected", c["accent_text"])])
    style.configure("Treeview.Heading", background=c["sidebar"], foreground=c["dim"],
                    relief="flat", font=(ui, 9, "bold"))
    style.configure("Side.Treeview", background=c["sidebar"], fieldbackground=c["sidebar"])
    style.configure("TSeparator", background=c["border"])
    style.configure("TPanedwindow", background=c["border"])
    style.configure("Vertical.TScrollbar", background=c["panel"], troughcolor=c["bg"],
                    bordercolor=c["bg"], arrowcolor=c["dim"])
    style.configure("Horizontal.TScrollbar", background=c["panel"], troughcolor=c["bg"],
                    bordercolor=c["bg"], arrowcolor=c["dim"])
    style.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
    style.configure("TNotebook.Tab", background=c["sidebar"], foreground=c["dim"], padding=(12, 6))
    style.map("TNotebook.Tab", background=[("selected", c["panel"])],
              foreground=[("selected", c["text"])])
    return c
