"""The house theme: three palettes and the ttk styling that goes with them.

This is the shared theme module, identical in every one of Dan's Python apps.
The palettes and their key names match Winnow (Theme/Palette.cs), so the whole
family looks like one set:

    phosphor - violet phosphor on near-black, like an old vector display.
               Winnow's look, and the default everywhere.
    dark     - the original graphite-and-blue dark.
    light    - light.

Ctrl+D (Cmd+D on a Mac where an app uses it) cycles phosphor -> dark -> light,
the same order as Winnow, and each app remembers the choice as "theme" in its
settings file. Older settings files only held "dark": true/false; true now
opens in phosphor (the new dark default) and false stays light.

The house rule for all three palettes: anything you can type in or click is a
different shade from the surface behind it, carries a visible border, and
shows an accent ring when focused, so an input never looks like a label.

`apply()` styles a Tk root. An app that draws its window as a web page (Deal
Hunter) takes the same palettes through `css_vars()` instead. tkinter is only
imported inside the Tk helpers, so a build that leaves tkinter out never loads
it.
"""
from __future__ import annotations

# --------------------------------------------------------------- palettes
# Winnow's default. Inputs still get their own darker shade, a visible border
# and an accent ring.
PHOSPHOR = dict(
    bg="#0b0812", panel="#130e1e", sidebar="#08060d", text="#eadcff",
    dim="#9b88c6", border="#2b2040", accent="#b77dff", accent_text="#0b0812",
    field="#06040a", field_text="#f3ebff", field_border="#4d3a73",
    sel="#2c1b48", good="#7de3a6", warn="#ffc56b", bad="#ff6b8e",
    code="#07050c", hint="#1b1230",
)
DARK = dict(
    bg="#14161b", panel="#1c1f27", sidebar="#101218", text="#e9ebf0",
    dim="#98a2b3", border="#333b49", accent="#5b9cff", accent_text="#0a0c10",
    field="#0a0c11", field_text="#f2f4f8", field_border="#495364",
    sel="#24344d", good="#4ac26b", warn="#e0b44a", bad="#ff6f61",
    code="#0f1116", hint="#182339",
)
LIGHT = dict(
    bg="#f1f3f6", panel="#ffffff", sidebar="#e7eaef", text="#14171c",
    dim="#596273", border="#ccd2dc", accent="#1f6feb", accent_text="#ffffff",
    field="#ffffff", field_text="#14171c", field_border="#a9b2c0",
    sel="#d6e4ff", good="#146c2e", warn="#8a5a00", bad="#b42318",
    code="#f5f7fa", hint="#eef4ff",
)

THEMES = {"phosphor": PHOSPHOR, "dark": DARK, "light": LIGHT}
ORDER = ("phosphor", "dark", "light")          # the Ctrl+D cycle, as in Winnow
DEFAULT = "phosphor"
LABELS = {"phosphor": "Phosphor purple", "dark": "Dark", "light": "Light"}
SHORT = {"phosphor": "Phosphor", "dark": "Dark", "light": "Light"}


# ---------------------------------------------------------- name helpers
def resolve(mode=None) -> str:
    """A theme name from whatever an app holds: a name, None, or the old
    dark flag (True = the dark default, now phosphor; False = light)."""
    if isinstance(mode, str):
        m = mode.strip().lower()
        if m in THEMES:
            return m
        if m.startswith("phosphor") or m == "purple":
            return "phosphor"
        return DEFAULT
    if mode is False:
        return "light"
    return DEFAULT


def from_settings(settings) -> str:
    """The theme name stored in a settings dict. "theme" wins; failing that
    the old "dark" flag decides (false = light, anything else = phosphor)."""
    if not isinstance(settings, dict):
        return DEFAULT
    t = settings.get("theme")
    if isinstance(t, str) and t.strip().lower() in THEMES:
        return t.strip().lower()
    if settings.get("dark") is False:
        return "light"
    return DEFAULT


def next_theme(mode) -> str:
    """The theme after this one in the Ctrl+D cycle."""
    name = resolve(mode)
    return ORDER[(ORDER.index(name) + 1) % len(ORDER)]


def is_dark(mode) -> bool:
    return resolve(mode) != "light"


def label(mode) -> str:
    return LABELS[resolve(mode)]


def short_label(mode) -> str:
    return SHORT[resolve(mode)]


# ---------------------------------------------------------------- colours
def _rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def blend(a: str, b: str, t: float) -> str:
    """Mix colour a toward colour b by t (0..1)."""
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ra, rb))


def palette(mode=None) -> dict:
    """A copy of the palette for this theme, plus the derived colours:
    hover (Winnow's button/row hover) and accent_hover (pressed-in accent).
    Also carries "name" and "is_dark" for apps that branch on them."""
    name = resolve(mode)
    c = dict(THEMES[name])
    dark = name != "light"
    c["hover"] = blend(c["panel"], c["accent"], 0.10 if dark else 0.07)
    c["accent_hover"] = blend(c["accent"], c["bg"], 0.12)
    c["name"] = name
    c["is_dark"] = dark
    return c


COLOUR_KEYS = tuple(PHOSPHOR) + ("hover", "accent_hover")


def css_vars(mode=None) -> str:
    """The palette as CSS custom properties: --bg, --panel, --field-border..."""
    c = palette(mode)
    return " ".join(f"--{k.replace('_', '-')}: {c[k]};" for k in COLOUR_KEYS)


# ------------------------------------------------------------------ fonts
def mono_family(root):
    from tkinter import font as tkfont
    families = set(tkfont.families(root))
    for name in ("Cascadia Mono", "Consolas", "SF Mono", "Menlo",
                 "DejaVu Sans Mono", "Liberation Mono", "Courier New"):
        if name in families:
            return name
    return "TkFixedFont"


def ui_family(root):
    from tkinter import font as tkfont
    families = set(tkfont.families(root))
    for name in ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text",
                 "Helvetica Neue", "Inter", "DejaVu Sans", "Arial"):
        if name in families:
            return name
    return "TkDefaultFont"


# ----------------------------------------------------------------- styling
def _recolour_menus(root, c):
    """Recolour the plain Tk menus that already exist (a theme switch at
    runtime); menus made later pick the colours up from the option database.
    The Mac menu bar is drawn by macOS and ignores this, which is fine."""
    import tkinter as tk
    stack = [root]
    while stack:
        w = stack.pop()
        try:
            stack.extend(w.winfo_children())
        except Exception:
            continue
        if isinstance(w, tk.Menu):
            try:
                w.configure(background=c["panel"], foreground=c["text"],
                            activebackground=c["sel"], activeforeground=c["text"],
                            selectcolor=c["accent"], disabledforeground=c["dim"])
            except Exception:
                pass


def apply(root, mode=None) -> dict:
    """Style a Tk root with a theme and return its palette.

    `mode` is a theme name ("phosphor", "dark", "light") or, for older
    callers, the dark flag (True = phosphor, False = light)."""
    import tkinter as tk
    from tkinter import ttk

    c = palette(mode)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    ui = ui_family(root)
    root.configure(bg=c["bg"])

    style.configure(".", background=c["bg"], foreground=c["text"],
                    fieldbackground=c["field"], bordercolor=c["border"],
                    selectbackground=c["sel"], selectforeground=c["text"],
                    insertcolor=c["accent"], font=(ui, 10))

    # ---- surfaces and text
    style.configure("TFrame", background=c["bg"])
    style.configure("Panel.TFrame", background=c["panel"])
    style.configure("Side.TFrame", background=c["sidebar"])
    style.configure("Card.TFrame", background=c["panel"], bordercolor=c["border"],
                    lightcolor=c["border"], darkcolor=c["border"],
                    relief="solid", borderwidth=1)
    style.configure("TLabel", background=c["bg"], foreground=c["text"])
    style.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
    style.configure("Dim.TLabel", background=c["bg"], foreground=c["dim"])
    style.configure("Field.TLabel", background=c["bg"], foreground=c["text"],
                    font=(ui, 9, "bold"))
    style.configure("Hint.TLabel", background=c["hint"], foreground=c["text"],
                    padding=(10, 6))
    style.configure("Hint.TFrame", background=c["hint"])
    style.configure("Hint.TButton", background=c["accent"], foreground=c["accent_text"],
                    bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"],
                    padding=(9, 3), font=(ui, 9, "bold"))
    style.map("Hint.TButton", background=[("pressed", c["accent_hover"]),
                                          ("active", c["accent_hover"])])
    style.configure("PanelDim.TLabel", background=c["panel"], foreground=c["dim"])
    style.configure("Title.TLabel", background=c["bg"], foreground=c["text"],
                    font=(ui, 15, "bold"))
    style.configure("Sub.TLabel", background=c["bg"], foreground=c["dim"], font=(ui, 10))
    style.configure("Good.TLabel", background=c["bg"], foreground=c["good"])
    style.configure("Warn.TLabel", background=c["bg"], foreground=c["warn"])
    style.configure("Bad.TLabel", background=c["bg"], foreground=c["bad"])
    style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"],
                    lightcolor=c["border"], darkcolor=c["border"])
    style.configure("TLabelframe.Label", background=c["bg"], foreground=c["dim"],
                    font=(ui, 9, "bold"))

    # ---- buttons: panel-coloured with a field border; hover tints toward
    # the accent and lights the border, as in Winnow
    style.configure("TButton", background=c["panel"], foreground=c["text"],
                    bordercolor=c["field_border"], lightcolor=c["field_border"],
                    darkcolor=c["field_border"], focuscolor=c["accent"],
                    padding=(11, 6), relief="solid")
    style.map("TButton",
              background=[("pressed", c["sel"]), ("active", c["hover"])],
              bordercolor=[("active", c["accent"]), ("focus", c["accent"])],
              lightcolor=[("active", c["accent"]), ("focus", c["accent"])],
              darkcolor=[("active", c["accent"]), ("focus", c["accent"])],
              foreground=[("disabled", c["dim"])])
    style.configure("Accent.TButton", background=c["accent"], foreground=c["accent_text"],
                    bordercolor=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"],
                    padding=(14, 6), font=(ui, 10, "bold"))
    style.map("Accent.TButton",
              background=[("disabled", c["border"]), ("pressed", c["accent_hover"]),
                          ("active", c["accent_hover"])],
              bordercolor=[("disabled", c["border"])],
              lightcolor=[("disabled", c["border"])],
              darkcolor=[("disabled", c["border"])],
              foreground=[("disabled", c["dim"])])
    style.configure("Danger.TButton", background=c["panel"], foreground=c["bad"],
                    bordercolor=c["bad"], lightcolor=c["bad"], darkcolor=c["bad"])
    style.map("Danger.TButton", background=[("pressed", c["sel"]), ("active", c["hover"])],
              foreground=[("disabled", c["dim"])])
    style.configure("TMenubutton", background=c["panel"], foreground=c["text"],
                    bordercolor=c["field_border"], lightcolor=c["field_border"],
                    darkcolor=c["field_border"], arrowcolor=c["accent"], padding=(9, 5))
    style.map("TMenubutton", background=[("active", c["hover"])],
              bordercolor=[("active", c["accent"])])

    # ---- entries: darker (or whiter) than the panel, a real border, and an
    # accent ring when focused so you always know where the caret is.
    style.configure("TEntry", fieldbackground=c["field"], foreground=c["field_text"],
                    insertcolor=c["accent"], bordercolor=c["field_border"],
                    lightcolor=c["field_border"], darkcolor=c["field_border"],
                    selectbackground=c["accent"], selectforeground=c["accent_text"],
                    padding=5, relief="solid")
    style.map("TEntry",
              bordercolor=[("focus", c["accent"]), ("hover", c["accent"])],
              lightcolor=[("focus", c["accent"])],
              darkcolor=[("focus", c["accent"])],
              fieldbackground=[("readonly", c["panel"]), ("disabled", c["bg"])],
              foreground=[("disabled", c["dim"])])
    style.configure("TSpinbox", fieldbackground=c["field"], foreground=c["field_text"],
                    background=c["panel"], arrowcolor=c["accent"],
                    insertcolor=c["accent"], bordercolor=c["field_border"],
                    lightcolor=c["field_border"], darkcolor=c["field_border"],
                    selectbackground=c["accent"], selectforeground=c["accent_text"],
                    padding=4, relief="solid")
    style.map("TSpinbox",
              bordercolor=[("focus", c["accent"]), ("hover", c["accent"])],
              lightcolor=[("focus", c["accent"])],
              darkcolor=[("focus", c["accent"])],
              fieldbackground=[("readonly", c["panel"]), ("disabled", c["bg"])],
              foreground=[("disabled", c["dim"])])
    style.configure("TCombobox", fieldbackground=c["field"], foreground=c["field_text"],
                    background=c["panel"], arrowcolor=c["accent"],
                    insertcolor=c["accent"], bordercolor=c["field_border"],
                    lightcolor=c["field_border"], darkcolor=c["field_border"],
                    padding=4, relief="solid")
    # A read-only combobox draws its text as a *selection*, so without these it
    # comes out grey-on-grey and unreadable in the dark themes.
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

    # ---- tick boxes and radio buttons: a field-coloured box that fills with
    # the accent when on, as in Winnow
    for name, bg in (("TCheckbutton", c["bg"]), ("Panel.TCheckbutton", c["panel"]),
                     ("TRadiobutton", c["bg"]), ("Panel.TRadiobutton", c["panel"])):
        style.configure(name, background=bg, foreground=c["text"],
                        indicatorbackground=c["field"], indicatorforeground=c["accent_text"],
                        upperbordercolor=c["field_border"], lowerbordercolor=c["field_border"],
                        focuscolor=c["accent"])
        style.map(name,
                  background=[("active", bg)],
                  foreground=[("disabled", c["dim"])],
                  indicatorbackground=[("disabled", c["bg"]), ("selected", c["accent"]),
                                       ("pressed", c["sel"])],
                  upperbordercolor=[("selected", c["accent"]), ("active", c["accent"])],
                  lowerbordercolor=[("selected", c["accent"]), ("active", c["accent"])])

    # ---- lists and trees: selection is the soft selection shade with normal
    # text (Winnow's rows), headings sit on the sidebar shade
    style.configure("Treeview", background=c["panel"], fieldbackground=c["panel"],
                    foreground=c["text"], bordercolor=c["border"],
                    lightcolor=c["border"], darkcolor=c["border"], rowheight=23,
                    font=(ui, 10))
    style.map("Treeview", background=[("selected", c["sel"])],
              foreground=[("selected", c["text"])])
    style.configure("Treeview.Heading", background=c["sidebar"], foreground=c["dim"],
                    bordercolor=c["border"], lightcolor=c["sidebar"], darkcolor=c["border"],
                    relief="flat", font=(ui, 9, "bold"))
    style.map("Treeview.Heading", background=[("active", c["hover"])],
              foreground=[("active", c["text"])])
    style.configure("Side.Treeview", background=c["sidebar"], fieldbackground=c["sidebar"])

    # ---- the rest
    style.configure("TSeparator", background=c["border"])
    style.configure("TPanedwindow", background=c["border"])
    style.configure("Sash", sashthickness=5, gripcount=0, background=c["border"],
                    lightcolor=c["border"], bordercolor=c["border"])
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=c["field_border"],
                        troughcolor=c["bg"], bordercolor=c["bg"],
                        lightcolor=c["field_border"], darkcolor=c["field_border"],
                        arrowcolor=c["dim"], gripcount=0)
        style.map(f"{orient}.TScrollbar",
                  background=[("pressed", c["accent"]), ("active", c["accent"])],
                  lightcolor=[("pressed", c["accent"]), ("active", c["accent"])],
                  darkcolor=[("pressed", c["accent"]), ("active", c["accent"])],
                  arrowcolor=[("active", c["accent_text"])])
        style.configure(f"{orient}.TProgressbar", background=c["accent"],
                        troughcolor=c["field"], bordercolor=c["field_border"],
                        lightcolor=c["accent"], darkcolor=c["accent"])
        style.configure(f"{orient}.TScale", background=c["accent"], troughcolor=c["field"],
                        bordercolor=c["field_border"], lightcolor=c["accent"],
                        darkcolor=c["accent"])
        style.map(f"{orient}.TScale", background=[("active", c["accent_hover"])])
    style.configure("TNotebook", background=c["bg"], bordercolor=c["border"],
                    lightcolor=c["border"], darkcolor=c["border"])
    style.configure("TNotebook.Tab", background=c["sidebar"], foreground=c["dim"],
                    bordercolor=c["border"], lightcolor=c["sidebar"], padding=(12, 6))
    style.map("TNotebook.Tab",
              background=[("selected", c["panel"]), ("active", c["hover"])],
              foreground=[("selected", c["text"]), ("active", c["text"])],
              lightcolor=[("selected", c["accent"])])

    # ---- plain Tk menus (context menus, menu-bar drop-downs on Windows/Linux)
    root.option_add("*Menu.background", c["panel"])
    root.option_add("*Menu.foreground", c["text"])
    root.option_add("*Menu.activeBackground", c["sel"])
    root.option_add("*Menu.activeForeground", c["text"])
    root.option_add("*Menu.selectColor", c["accent"])
    root.option_add("*Menu.disabledForeground", c["dim"])
    _recolour_menus(root, c)
    return c
