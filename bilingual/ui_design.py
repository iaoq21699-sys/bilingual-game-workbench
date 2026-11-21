"""Shared desktop palette and small native Tk interaction treatments."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk


BG = "#101A1C"
PANEL = "#192629"
SURFACE = "#243538"
INK = "#F2F4EA"
MUTED = "#ACBCB9"
ACCENT = "#9BDBC5"
LINE = "#394C4D"
WARN = "#F1BE72"
FONT = "Microsoft YaHei UI"
ENGLISH_FONT = "Georgia"
PAPER = "#EBE8DC"
PAPER_INK = "#21312C"
PAPER_MUTED = "#536359"
PAPER_ACCENT = "#215C4C"
PAPER_MARK = "#CADBBE"
PAPER_LINE = "#BAC3B4"
PAPER_INPUT = "#F6F3E9"


def book_mark(parent):
    mark = tk.Canvas(parent, width=34, height=34, bg=BG, highlightthickness=0, bd=0)
    mark.create_rectangle(2, 3, 31, 31, fill=SURFACE, outline=LINE)
    mark.create_rectangle(8, 8, 25, 26, outline=ACCENT, width=2)
    mark.create_line(13, 8, 13, 26, fill=ACCENT, width=2)
    mark.create_line(17, 13, 22, 13, fill=ACCENT, width=2)
    return mark


def style_titlebar(window):
    """Match a Windows title bar without changing focus, position, or stacking."""
    try:
        import ctypes
        import os
        from ctypes import wintypes
        if os.name != "nt" or not window.winfo_exists():
            return
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.SetWindowPos.restype = wintypes.BOOL
        frame = int(window.wm_frame(), 0)
        hwnd = user.GetAncestor(frame, 2) or user.GetParent(frame) or frame
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if not hwnd or owner.value != os.getpid():
            return
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        dark = ctypes.c_int(1)
        if dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark)) != 0:
            dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(dark), ctypes.sizeof(dark))
        for attribute, color in ((35, BG), (36, INK), (34, LINE)):
            red, green, blue = (int(color[offset:offset+2], 16) for offset in (1, 3, 5))
            value = ctypes.c_uint(red | green << 8 | blue << 16)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        # SWP_NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED.
        user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
    except (OSError, AttributeError, ValueError, tk.TclError):
        pass


def _mix(first, second, amount):
    a = tuple(int(first[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(second[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def _rounded_image(root, fill, border, gradient=True):
    """Transparent corners and nine-slice-safe edges; no raster dependency."""
    width, height, radius = 20, 20, 6
    image = tk.PhotoImage(master=root, width=width, height=height)

    def inset(y, size, curve):
        distance = max(curve - y - 0.5, y + 0.5 - (size - curve), 0)
        return int(math.ceil(curve - math.sqrt(max(0, curve * curve - distance * distance))))

    for y in range(height):
        edge = inset(y, height, radius)
        image.put(border, to=(edge, y, width - edge, y + 1))
        if 0 < y < height - 1:
            inner = 1 + inset(y - 1, height - 2, radius - 1)
            tone = _mix(fill, "#FFFFFF", 0.035 * (1 - y / height)) if gradient else fill
            image.put(tone, to=(inner, y, width - inner, y + 1))
    return image


def apply_styles(root):
    """Install one coherent desktop style without changing window stacking."""
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=PANEL, foreground=INK, font=(FONT, 10),
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
    for name, color in (("TFrame", PANEL), ("Outer.TFrame", BG), ("Card.TFrame", PANEL),
                        ("Inset.TFrame", SURFACE), ("Health.TFrame", PANEL)):
        style.configure(name, background=color)
    style.configure("TLabel", background=PANEL, foreground=INK)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Title.TLabel", background=BG, foreground=INK, font=(FONT, 19, "bold"))
    style.configure("Section.TLabel", foreground=INK, font=(FONT, 12, "bold"))
    style.configure("Eyebrow.TLabel", foreground=ACCENT, font=(FONT, 9, "bold"))
    style.configure("Caption.TLabel", foreground=MUTED, font=(FONT, 9))
    style.configure("HealthTitle.TLabel", background=PANEL, foreground=INK, font=(FONT, 10, "bold"))
    style.configure("HealthDetail.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 9))
    for name, fill, ink in (("Success", "#1D3B36", "#A2EAD7"),
                            ("Warning", "#403426", "#F3CC92"),
                            ("Info", "#20354A", "#A5D5FB"),
                            ("Neutral", SURFACE, MUTED)):
        style.configure(name + ".Badge.TLabel", background=fill, foreground=ink,
                        padding=(9, 4), font=(FONT, 9, "bold"))

    # Keep PhotoImages alive for as long as this Tk interpreter exists.
    images = getattr(root, "_workbench_style_images", [])
    root._workbench_style_images = images
    treatments = {
        "Primary": (ACCENT, "#8CE7D3", "#50B8A6", ACCENT, BG),
        "Secondary": (SURFACE, "#344B4B", "#1A2829", LINE, INK),
        "Quiet": (PANEL, SURFACE, "#131E1F", PANEL, MUTED),
        "Header": (BG, PANEL, "#101819", BG, MUTED),
        "Paper": (PAPER_ACCENT, "#2E6A58", "#184737", PAPER_ACCENT, "#FFFFFF"),
        "Danger": ("#3B272E", "#50313A", "#2E1E24", "#64404B", "#F6B3B8"),
    }
    for name, (fill, hover, pressed, border, ink) in treatments.items():
        normal = _rounded_image(root, fill, border)
        active = _rounded_image(root, hover, _mix(border, INK, .12))
        down = _rounded_image(root, pressed, _mix(border, BG, .3), False)
        focus = _rounded_image(root, hover, PAPER_INK if name == "Paper" else "#DAFFF6" if name == "Primary" else ACCENT)
        disabled = (_rounded_image(root, "#DEE2D7", PAPER_LINE, False) if name == "Paper" else
                    _rounded_image(root, "#1C2A2C", "#2A393B", False))
        images.extend((normal, active, down, focus, disabled))
        element = "Workbench" + name + ".border"
        if element not in style.element_names():
            style.element_create(element, "image", normal, ("disabled", disabled), ("pressed", down),
                                 ("focus", focus), ("active", active), border=7, sticky="nsew")
        names = ("Accent.TButton", "Primary.TButton") if name == "Primary" else (
            ("TButton", "Secondary.TButton") if name == "Secondary" else (name + ".TButton",))
        for button_style in names:
            style.layout(button_style, [(element, {"sticky": "nsew", "children": [
                ("Button.padding", {"sticky": "nsew", "children": [
                    ("Button.label", {"sticky": "nsew"})]})]})])
            corner = PAPER if name == "Paper" else BG if name == "Header" else PANEL
            style.configure(button_style, foreground=ink, background=corner,
                            padding=(10, 3) if name in {"Primary", "Secondary"} else (6, 1),
                            anchor="center", font=(FONT, 10, "bold") if name == "Primary" else (FONT, 10),
                            width=0, borderwidth=0, relief="flat")
            style.map(button_style, foreground=[("disabled", PAPER_MUTED if name == "Paper" else "#81928F"), ("active", INK if name != "Primary" else BG)],
                      background=[("disabled", corner), ("pressed", corner), ("active", corner), ("focus", corner)])

    style.configure("TEntry", fieldbackground=SURFACE, foreground=INK, insertcolor=ACCENT,
                    padding=(12, 10), borderwidth=1, lightcolor=LINE, darkcolor=LINE, bordercolor=LINE)
    style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)], foreground=[("disabled", MUTED)])
    style.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE, foreground=INK,
                    arrowcolor=MUTED, lightcolor=LINE, darkcolor=LINE, bordercolor=LINE,
                    padding=(12, 9), borderwidth=1, arrowsize=14)
    style.map("TCombobox", fieldbackground=[("readonly", SURFACE)], foreground=[("disabled", "#66768C"), ("readonly", INK)],
              selectbackground=[("readonly", SURFACE)], selectforeground=[("readonly", INK)],
              bordercolor=[("focus", ACCENT), ("active", "#587188")], arrowcolor=[("active", ACCENT)])
    style.configure("Game.TCombobox", font=(FONT, 11), padding=(12, 10))
    style.configure("TMenubutton", background=BG, foreground=MUTED, arrowcolor=MUTED,
                    lightcolor=BG, darkcolor=BG, bordercolor=BG, padding=(10, 8), borderwidth=0)
    style.map("TMenubutton", background=[("active", PANEL)], foreground=[("active", INK), ("disabled", "#66768C")])
    style.configure("Vertical.TScrollbar", background=LINE, troughcolor=PANEL, borderwidth=0,
                    arrowcolor=MUTED, width=10, arrowsize=10)
    style.map("Vertical.TScrollbar", background=[("active", "#53657C")])
    style.configure("Horizontal.TScrollbar", background=LINE, troughcolor=PANEL, borderwidth=0, arrowcolor=MUTED)
    style.configure("TCheckbutton", background=PANEL, foreground=INK, indicatorbackground=SURFACE,
                    indicatorforeground=ACCENT, padding=4)
    style.map("TCheckbutton", background=[("active", PANEL)], indicatorbackground=[("selected", ACCENT)])
    style.configure("TNotebook", background=PANEL, borderwidth=0, lightcolor=PANEL, darkcolor=PANEL,
                    bordercolor=PANEL, tabmargins=(0, 4, 0, 0))
    style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(18, 9),
                    lightcolor=PANEL, darkcolor=PANEL, bordercolor=PANEL, font=(FONT, 10))
    style.map("TNotebook.Tab", background=[("selected", SURFACE), ("active", "#1D2938")],
              foreground=[("selected", ACCENT), ("active", INK)],
              bordercolor=[("selected", SURFACE)], lightcolor=[("selected", SURFACE)], darkcolor=[("selected", SURFACE)])
    style.configure("TPanedwindow", background=BG, sashwidth=12)
    style.configure("TSeparator", background=LINE)
    style.configure("Horizontal.TScale", background=ACCENT, troughcolor=SURFACE, bordercolor=LINE,
                    lightcolor=ACCENT, darkcolor=ACCENT, sliderlength=22)
    style.map("Horizontal.TScale", background=[("active", "#A0F0DF")])
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=SURFACE,
                    bordercolor=SURFACE, lightcolor=ACCENT, darkcolor=ACCENT)
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", "#294D4E")
    root.option_add("*TCombobox*Listbox.selectForeground", INK)
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))
    if not getattr(root, "_workbench_button_cursor_bound", False):
        def button_cursor(event):
            try:
                event.widget.configure(cursor="arrow" if event.widget.instate(["disabled"]) else "hand2")
            except tk.TclError:
                pass
        root.bind_class("TButton", "<Enter>", button_cursor, add=True)
        root._workbench_button_cursor_bound = True
    icon = tk.PhotoImage(master=root, width=32, height=32)
    icon.put(SURFACE, to=(2, 2, 30, 30))
    icon.put("#2B5F5C", to=(6, 7, 15, 25))
    icon.put(ACCENT, to=(7, 8, 14, 10))
    icon.put("#DCEAF5", to=(17, 7, 26, 25))
    for y, right in ((12, 24), (16, 24), (20, 22)):
        icon.put("#527185", to=(19, y, right, y + 1))
        icon.put(ACCENT, to=(8, y, 13, y + 1))
    images.append(icon)
    root.iconphoto(True, icon)
    return style


def tooltip(widget, text, delay=650):
    """A brief pointer hint: no focus, topmost flag, or lasting popup."""
    state = {"timer": None, "window": None}

    def hide(_=None):
        if state["timer"] is not None:
            try:
                widget.after_cancel(state["timer"])
            except tk.TclError:
                pass
            state["timer"] = None
        if state["window"] is not None:
            try:
                state["window"].destroy()
            except tk.TclError:
                pass
            state["window"] = None

    def show():
        state["timer"] = None
        if not widget.winfo_exists():
            return
        window = tk.Toplevel(widget)
        state["window"] = window
        window.withdraw()
        window.overrideredirect(True)
        window.transient(widget.winfo_toplevel())
        tk.Label(window, text=text, wraplength=300, justify="left", bg="#2A394B", fg=INK,
                 font=(FONT, 9), padx=12, pady=8, relief="flat").pack()
        window.update_idletasks()
        x = min(widget.winfo_pointerx() + 14, widget.winfo_screenwidth() - window.winfo_reqwidth() - 12)
        y = min(widget.winfo_rooty() + widget.winfo_height() + 7,
                widget.winfo_screenheight() - window.winfo_reqheight() - 12)
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
        window.deiconify()

    def schedule(_):
        hide()
        state["timer"] = widget.after(delay, show)

    widget.bind("<Enter>", schedule, add=True)
    widget.bind("<Leave>", hide, add=True)
    widget.bind("<ButtonPress>", hide, add=True)
    widget.bind("<FocusOut>", hide, add=True)
    widget.bind("<Destroy>", hide, add=True)
    return hide
