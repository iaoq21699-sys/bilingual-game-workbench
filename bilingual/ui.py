"""Offline desktop library and in-game companion panel (Python stdlib only)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import copy
import json
import queue
import re
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

from .hotkey import GlobalHotkey
from .game_session import GameSession
from .core import display_text
from .notes import Notebook, note_key
from .ui_design import (BG, PANEL, SURFACE, INK, MUTED, ACCENT, LINE, WARN, FONT,
                        ENGLISH_FONT, PAPER, PAPER_INK, PAPER_MUTED, PAPER_ACCENT, PAPER_MARK,
                        apply_styles, book_mark, tooltip)


def brief(value, length=95):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= length else text[:length] + "…"


class ResultCards(ttk.Frame):
    """Readable, keyboard-accessible search cards instead of a compressed grid."""

    def __init__(self, parent, on_select, on_empty=None, *, compact=False):
        super().__init__(parent)
        self.on_select = on_select
        self.on_empty = on_empty
        self.compact = compact
        self.show_secondary = True
        self.items = []
        self.selected_id = None
        self.hovered = None
        self.focused = False
        self.empty_bounds = None
        self.english_font = tkfont.Font(family="Segoe UI", size=12, weight="bold" if compact else "normal")
        self.chinese_font = tkfont.Font(family=FONT, size=10)
        self.meta_font = tkfont.Font(family=FONT, size=9)
        self.card_height = (self.meta_font.metrics("linespace") + self.english_font.metrics("linespace") * (1 if compact else 2)
                            + self.chinese_font.metrics("linespace") + 32)
        self.canvas = tk.Canvas(self, width=1, height=1, bg=PANEL, highlightthickness=0, bd=0, takefocus=True)
        scroll = ttk.Scrollbar(self, orient="vertical", command=self._scroll)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.bind("<Configure>", lambda event: self._draw())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda event: self._hover(None))
        self.canvas.bind("<FocusIn>", lambda event: self._focus(True))
        self.canvas.bind("<FocusOut>", lambda event: self._focus(False))
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda event: self._scroll("scroll", -2, "units"))
        self.canvas.bind("<Button-5>", lambda event: self._scroll("scroll", 2, "units"))
        self.canvas.bind("<Up>", lambda event: self._step(-1))
        self.canvas.bind("<Down>", lambda event: self._step(1))
        self.canvas.bind("<Prior>", lambda event: self._step(-3))
        self.canvas.bind("<Next>", lambda event: self._step(3))
        self.canvas.bind("<Home>", lambda event: self._step(-len(self.items)))
        self.canvas.bind("<End>", lambda event: self._step(len(self.items)))
        self.canvas.bind("<Return>", self._activate)

    @staticmethod
    def _fit(value, font, width):
        value = str(value).strip()
        if font.measure(value) <= width:
            return value
        lo, hi = 0, len(value)
        while lo < hi:
            middle = (lo + hi + 1) // 2
            if font.measure(value[:middle] + "…") <= width:
                lo = middle
            else:
                hi = middle - 1
        return value[:lo].rstrip() + "…"

    def _english(self, value, width):
        value = re.sub(r"\s+", " ", str(value)).strip()
        if self.english_font.measure(value) <= width:
            return value
        first = self._fit(value, self.english_font, width)
        cut = max(1, len(first.rstrip("…")))
        space = value.rfind(" ", 0, cut + 1)
        if space > cut // 2:
            cut = space
        return value[:cut].strip() + "\n" + self._fit(value[cut:].strip(), self.english_font, width)

    def _round_rect(self, x1, y1, x2, y2, fill, outline="", line_width=1):
        radius = 12
        self.canvas.create_polygon(x1+radius, y1, x2-radius, y1, x2, y1, x2, y1+radius,
                                   x2, y2-radius, x2, y2, x2-radius, y2, x1+radius, y2,
                                   x1, y2, x1, y2-radius, x1, y1+radius, x1, y1,
                                   smooth=True, splinesteps=18, fill=fill, outline=outline, width=line_width)

    def _draw(self):
        width = max(100, self.canvas.winfo_width() - 5)
        self.canvas.delete("all")
        stride = self.card_height + 10
        self.canvas.configure(scrollregion=(0, 0, width, len(self.items) * stride if self.items else 210))
        self.empty_bounds = None
        if not self.items:
            if self.compact:
                self.canvas.create_text(12, 20, anchor="nw", text="暂无收藏", fill=MUTED, font=self.meta_font)
                return
            center = width / 2
            top = 22
            self.canvas.create_oval(center-23, top, center+23, top+46, fill=SURFACE, outline=LINE)
            self.canvas.create_oval(center-9, top+11, center+5, top+25, outline=ACCENT, width=2)
            self.canvas.create_line(center+4, top+25, center+12, top+33, fill=ACCENT, width=2)
            self.canvas.create_text(center, top+69, text="没有找到匹配原句", fill=INK, font=(FONT, 12, "bold"))
            self.canvas.create_text(center, top+99, width=width-34, text="试试更短的关键词，\n或清空筛选，浏览全部原句。", justify="center", fill=MUTED, font=(FONT, 10))
            if self.on_empty:
                self.empty_bounds = (max(8, center-84), top+140, min(width-8, center+84), top+176)
                self._round_rect(*self.empty_bounds, fill="#243F48", outline="#375C62")
                self.canvas.create_text(center, top+158, text="清空筛选  →", fill=ACCENT, font=(FONT, 10, "bold"))
            return
        first = max(0, int(self.canvas.canvasy(0) // stride) - 1)
        last = min(len(self.items), int((self.canvas.canvasy(0)+self.canvas.winfo_height()) // stride) + 2)
        for index in range(first, last):
            item = self.items[index]
            y = index * stride
            selected = str(item["id"]) == self.selected_id
            hovered = index == self.hovered
            background = "#2B4945" if selected else "#2B3E40" if hovered else PANEL
            self._round_rect(2, y+2, width-2, y+self.card_height-2, background,
                             ACCENT if selected else "#647F79" if hovered else LINE,
                             2 if selected and self.focused else 1)
            if selected:
                self.canvas.create_rectangle(3, y+19, 6, y+self.card_height-19, fill=ACCENT, outline="")
            usable = width - 36
            self.canvas.create_text(16, y+9, anchor="nw", text=self._fit(item.get("category") or "游戏文本", self.meta_font, usable), fill=ACCENT if selected else MUTED, font=self.meta_font)
            en_y = y + 13 + self.meta_font.metrics("linespace")
            english = display_text(item.get("en")) or "（缺少英文）"
            title = self._fit(english, self.english_font, usable) if self.compact else self._english(english, usable)
            self.canvas.create_text(16, en_y, anchor="nw", text=title, fill=INK, font=self.english_font)
            zh_y = y + self.card_height - self.chinese_font.metrics("linespace") - 10
            if self.show_secondary:
                secondary = brief(display_text(item.get("zh")) or "（缺少中文）", 160)
                self.canvas.create_text(16, zh_y, anchor="nw", text=self._fit(secondary, self.chinese_font, usable), fill=MUTED, font=self.chinese_font)

    def set_items(self, items):
        self.items = list(items)
        self.selected_id = None
        self.hovered = None
        self.canvas.yview_moveto(0)
        self._draw()

    def set_secondary_visible(self, visible):
        self.show_secondary = visible
        self._draw()

    def select(self, identifier, *, notify=True):
        if identifier is None or not any(str(item["id"]) == str(identifier) for item in self.items):
            return
        self.selected_id = str(identifier)
        index = next(i for i, item in enumerate(self.items) if str(item["id"]) == self.selected_id)
        y = index * (self.card_height + 10)
        top = self.canvas.canvasy(0)
        if y < top or y + self.card_height > top + self.canvas.winfo_height():
            self.canvas.yview_moveto(y / max(1, len(self.items) * (self.card_height + 10)))
        self._draw()
        if notify:
            self.on_select(self.selected_id)

    def _click(self, event):
        self.canvas.focus_set()
        if not self.items and self.empty_bounds:
            x1, y1, x2, y2 = self.empty_bounds
            if x1 <= event.x <= x2 and y1 <= self.canvas.canvasy(event.y) <= y2:
                self.on_empty()
            return
        index = int(self.canvas.canvasy(event.y) // (self.card_height + 10))
        if 0 <= index < len(self.items):
            self.select(self.items[index]["id"])

    def _wheel(self, event):
        if event.delta:
            self._scroll("scroll", -max(1, abs(event.delta) // 120) * (1 if event.delta > 0 else -1) * 2, "units")
        return "break"

    def _scroll(self, *args):
        self.canvas.yview(*args)
        self.hovered = None
        self._draw()

    def _hover(self, index):
        if index != self.hovered:
            self.hovered = index
            self._draw()

    def _motion(self, event):
        index = int(self.canvas.canvasy(event.y) // (self.card_height + 10))
        valid = 0 <= index < len(self.items)
        self.canvas.configure(cursor="hand2" if valid or self.empty_bounds else "arrow")
        self._hover(index if valid else None)

    def _focus(self, focused):
        self.focused = focused
        self._draw()

    def _activate(self, event=None):
        if self.selected_id is not None:
            self.select(self.selected_id)
        elif self.on_empty and not self.items:
            self.on_empty()
        return "break"

    def _step(self, step):
        if self.items:
            index = next((i for i, item in enumerate(self.items) if str(item["id"]) == self.selected_id), -1 if step > 0 else 0)
            self.select(self.items[min(len(self.items)-1, max(0, index+step))]["id"])
        return "break"


class Desktop:
    PAGE_SIZE = 100

    def __init__(self, service, hidden=False):
        self.service = service
        self.root = tk.Tk()
        if hidden:
            self.root.withdraw()
        self.root.title("双语游戏工作台")
        width = min(1180, self.root.winfo_screenwidth() - 80)
        height = min(820, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{max(900, width)}x{max(720, height)}")
        self.root.minsize(900, 720)
        self.root.configure(bg=BG)
        self.root.option_add("*Font", (FONT, 11))
        self.root.option_add("*TCombobox*Listbox.background", SURFACE)
        self.root.option_add("*TCombobox*Listbox.foreground", INK)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#294D4E")
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="bilingual-ui")
        self.events = queue.Queue()
        self.closed = False
        self.game_id = None
        self.game_rows = {}
        self.results = {}
        self.game_ids = [None]
        self.show_provenance = False
        self.current_entry = None
        self.offset = 0
        self.total = 0
        self.search_generation = 0
        self.detail_generation = 0
        self.operation_busy = False
        self.notes_window = None
        self.note_busy = False
        self.lookup_note = None
        self.initial_load = True
        self.games_generation = 0
        self.sessions = {}
        self.return_session = None
        self.pending_launch = None
        self.launching_game = None
        self.search_after = None
        self.query = tk.StringVar()
        self.category = tk.StringVar(value="全部类别")
        self.status = tk.StringVar(value="正在载入本地文本库…")
        self.stat_text = tk.StringVar(value="本地离线 · 中英对照 · 词句注释")
        self._style()
        self._layout()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda event: self.hide())
        self.root.bind("<Control-f>", lambda event: self.search_box.focus_set())
        self.hotkey = GlobalHotkey(lambda: self.events.put((self.toggle, (), None)))
        self.hotkey.start()
        self.hotkey_label.configure(text="Ctrl+Shift+B 呼出  ·  Esc 返回" if self.hotkey.active else self.hotkey.error)
        self.root.after(200, self._window_chrome)
        self.root.after(950, self._window_chrome)
        self.root.after(70, self._drain)
        self._load_games()

    def _style(self):
        style = apply_styles(self.root)
        style.configure("Identity.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
        style.configure("ReaderTitle.TLabel", foreground=INK, font=(FONT, 14, "bold"))
        style.configure("ReaderContext.TLabel", foreground=MUTED, font=(FONT, 9))
        style.configure("Search.TEntry", padding=(11, 9))
        style.configure("Filter.TCombobox", padding=(10, 6))
        style.configure("Compact.TButton", padding=(9, 6), font=(FONT, 9))

    def _menu(self, owner):
        return tk.Menu(owner, tearoff=False, bg=SURFACE, fg=INK, activebackground="#294D4E", activeforeground=INK, font=(FONT, 11), relief="flat", borderwidth=1)

    def _layout(self):
        header = ttk.Frame(self.root, style="Outer.TFrame", padding=(22, 12, 20, 10))
        header.pack(fill="x")
        controls = ttk.Frame(header, style="Outer.TFrame")
        controls.pack(side="right")
        mark = book_mark(header)
        mark.pack(side="left", padx=(0, 11))
        ttk.Label(header, text="双语游戏工作台", style="Title.TLabel", font=(FONT, 17, "bold")).pack(side="left")
        game_button = ttk.Menubutton(controls, text="游戏")
        game_button.pack(side="left", padx=1)
        self.game_menu = self._menu(game_button)
        game_button.configure(menu=self.game_menu)
        self.game_menu.add_command(label="＋ 接入新游戏…", command=self._inspect_new_game)
        self.game_menu.add_separator()
        for label, action in (("启动游戏", "launch"), ("启用游戏双语", "enable"), ("恢复原版显示", "disable"), ("检查接入状态", "status")):
            self.game_menu.add_command(label=label, command=lambda a=action: self._action(a))
        self.game_menu.add_command(label="查看完整支持范围", command=self._coverage)
        self.game_menu.add_separator()
        self.game_menu.add_command(label="游戏内面板与主题…", command=lambda: self._native_panel("install"))
        self.game_menu.add_command(label="查看面板运行检查…", command=self._show_native_health)
        self.game_menu.add_command(label="移除游戏内检索面板", command=lambda: self._native_panel("remove"))
        library_button = ttk.Menubutton(controls, text="资料")
        library_button.pack(side="left", padx=1)
        self.library_menu = self._menu(library_button)
        library_button.configure(menu=self.library_menu)
        ttk.Button(controls, text="收藏与复习", style="Secondary.TButton", command=self._open_notes).pack(side="left", padx=(10, 2))
        for label, callback in (("接入已有双语项目", self._add_root), ("导入文本…", self._import_menu), ("选择离线词典…", self._import_dictionary), ("导出当前文本库", self._export), ("更新本地文本库", self._refresh)):
            self.library_menu.add_command(label=label, command=callback)
        self.library_menu.add_separator()
        for label, callback in (("从接入配置文件导入…", self._import_profile), ("导出当前游戏接入配置…", self._export_profile), ("创建双语游戏启动入口", self._create_launcher)):
            self.library_menu.add_command(label=label, command=callback)
        return_button = ttk.Button(controls, text="返回游戏  Esc", style="Header.TButton", command=self.hide)
        return_button.pack(side="left", padx=(8, 0))
        tooltip(return_button, "收起工作台并返回正在运行的游戏。游戏内检索面板使用 F8 打开。")

        identity = ttk.Frame(self.root, style="Outer.TFrame", padding=(22, 0, 20, 8))
        identity.pack(fill="x")
        identity.columnconfigure(0, weight=1)
        selector = ttk.Frame(identity, style="Outer.TFrame")
        selector.grid(row=0, column=0, sticky="ew", padx=(0, 18))
        ttk.Label(selector, text="当前游戏", style="Identity.TLabel").pack(side="left", padx=(0, 10))
        self.game_choice = tk.StringVar(value="全部游戏")
        self.game_picker = ttk.Combobox(selector, textvariable=self.game_choice, state="readonly", values=["全部游戏"], width=18, style="Game.TCombobox")
        self.game_picker.pack(side="left", fill="x", expand=True)
        self.game_picker.bind("<<ComboboxSelected>>", self._game_selected)
        actions = ttk.Frame(identity, style="Outer.TFrame")
        actions.grid(row=0, column=1, sticky="e")
        add_button = ttk.Button(actions, text="＋ 接入游戏", style="Header.TButton", command=self._inspect_new_game)
        add_button.pack(side="left", padx=(0, 8))
        self.native_panel_button = ttk.Button(actions, text="面板设置", style="Secondary.TButton",
                                             command=lambda: self._native_panel("install"))
        self.native_panel_button.pack(side="left", padx=(0, 8))
        self.launch_button = ttk.Button(actions, text="▶  启动游戏", style="Primary.TButton", command=lambda: self._action("launch"), state="disabled")
        self.launch_button.pack(side="left")
        tooltip(self.native_panel_button, "安装游戏内半透明检索面板，设置这款游戏的阅读主题。")
        tooltip(self.launch_button, "启动当前游戏，工作台会自动检查内置检索面板是否加载。")
        self.mutation_buttons = [game_button, library_button, add_button, self.native_panel_button]
        context = ttk.Frame(identity, style="Outer.TFrame")
        context.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.game_summary = tk.Label(context, text="", bg=BG, fg=MUTED, anchor="w", width=1, font=(FONT, 9))
        self.game_summary.pack(side="left", fill="x", expand=True)
        self.hotkey_label = tk.Label(context, text="", bg=BG, fg=ACCENT, font=(FONT, 9))
        self.hotkey_label.pack(side="right")

        from .native_health_ui import NativeHealthCard
        health_host = ttk.Frame(self.root, style="Outer.TFrame")
        health_host.pack(fill="x")
        self.native_health = NativeHealthCard(self, health_host)

        footer = ttk.Frame(self.root, style="Outer.TFrame", padding=(23, 7, 23, 8))
        footer.pack(side="bottom", fill="x")
        tk.Label(footer, textvariable=self.stat_text, bg=BG, fg=MUTED, anchor="e", font=(FONT, 9)).pack(side="right", padx=(14, 0))
        tk.Label(footer, textvariable=self.status, bg=BG, fg=MUTED, anchor="w", width=1, font=(FONT, 9)).pack(side="left", fill="x", expand=True)
        split = ttk.Panedwindow(self.root, orient="horizontal")
        split.pack(fill="both", expand=True, padx=20)
        left = ttk.Frame(split, style="Card.TFrame", padding=(16, 14, 14, 12), width=350)
        right = ttk.Frame(split, style="Card.TFrame", padding=(22, 13, 18, 12), width=730)
        split.add(left, weight=1)
        split.add(right, weight=3)
        self.root.after(220, lambda: split.sashpos(0, min(360, max(280, int(split.winfo_width() * .32)))))

        search_heading = ttk.Frame(left)
        search_heading.pack(fill="x", pady=(0, 10))
        ttk.Label(search_heading, text="查找原句", font=(FONT, 15, "bold")).pack(side="left")
        ttk.Label(search_heading, text="Ctrl + F", style="Caption.TLabel").pack(side="right")
        ttk.Label(left, text="按台词或术语检索，支持中英文。", style="Caption.TLabel", wraplength=255).pack(anchor="w", pady=(0, 6))
        search_row = ttk.Frame(left)
        search_row.pack(fill="x")
        self.search_box = ttk.Entry(search_row, textvariable=self.query, style="Search.TEntry", font=(FONT, 11), width=10)
        self.search_box.pack(side="left", fill="x", expand=True)
        self.search_box.bind("<Return>", lambda event: self._search(reset=True))
        self.search_box.bind("<Down>", self._focus_results)
        tooltip(self.search_box, "输入中文或英文关键词，会自动查找当前游戏的原句。按 ↓ 进入结果。")
        ttk.Button(search_row, text="搜索", style="Accent.TButton", command=lambda: self._search(reset=True)).pack(side="left", padx=(7, 0))
        self.query.trace_add("write", self._debounce)
        filters = ttk.Frame(left)
        filters.pack(fill="x", pady=(8, 8))
        self.category_box = ttk.Combobox(filters, textvariable=self.category, state="readonly", values=["全部类别"], style="Filter.TCombobox", width=12, font=(FONT, 10))
        self.category_box.pack(side="left", fill="x", expand=True)
        self.category_box.bind("<<ComboboxSelected>>", lambda event: self._search(reset=True))
        ttk.Button(filters, text="重置", style="Quiet.TButton", command=self._clear_filters).pack(side="right", padx=(4, 0))
        self.result_label = ttk.Label(left, text="输入关键词，或浏览全部文本", font=(FONT, 10))
        self.result_label.pack(anchor="w", pady=(0, 9))
        pagination = ttk.Frame(left)
        pagination.pack(side="bottom", fill="x", pady=(8, 0))
        self.prev_button = ttk.Button(pagination, text="←", width=3, style="Quiet.TButton", command=lambda: self._page(-1))
        self.prev_button.pack(side="left")
        self.next_button = ttk.Button(pagination, text="→", width=3, style="Quiet.TButton", command=lambda: self._page(1))
        self.next_button.pack(side="right")
        self.page_label = ttk.Label(pagination, text="", style="Muted.TLabel", font=("Segoe UI", 10))
        self.page_label.pack(expand=True)
        self.result_cards = ResultCards(left, self._entry_selected, self._clear_filters)
        self.result_cards.pack(fill="both", expand=True)

        detail_header = ttk.Frame(right)
        detail_header.pack(fill="x", pady=(0, 5))
        ttk.Label(detail_header, text="双语阅读", style="ReaderTitle.TLabel").pack(side="left", padx=(0, 14))
        self.detail_context = ttk.Label(detail_header, text="", style="ReaderContext.TLabel", width=1)
        self.detail_context.pack(side="left", fill="x", expand=True)
        self.copy_button = ttk.Button(detail_header, text="复制原句", style="Quiet.TButton", command=self._copy)
        self.copy_button.pack(side="right")
        self.save_sentence_button = ttk.Button(detail_header, text="收藏整句", style="Secondary.TButton",
                                               command=self._save_sentence)
        self.save_sentence_button.pack(side="right", padx=(0, 6))
        lookup_block = ttk.Frame(right)
        lookup_block.pack(side="bottom", fill="x", pady=(7, 0))
        ttk.Separator(lookup_block).pack(fill="x", pady=(0, 9))
        lookup_row = ttk.Frame(lookup_block)
        lookup_row.pack(fill="x", pady=(0, 7))
        ttk.Label(lookup_row, text="词语释义", font=(FONT, 11, "bold")).pack(side="left", padx=(0, 10))
        self.lookup_query = tk.StringVar()
        self.lookup_query.trace_add("write", self._clear_lookup_note)
        lookup_entry = ttk.Entry(lookup_row, textvariable=self.lookup_query, width=8, style="Search.TEntry", font=("Segoe UI", 11))
        lookup_entry.pack(side="left", fill="x", expand=True)
        lookup_entry.bind("<Return>", lambda event: self._lookup())
        ttk.Button(lookup_row, text="查词", style="Compact.TButton", command=self._lookup).pack(side="left", padx=(5, 0))
        selection_button = ttk.Button(lookup_row, text="查选中", style="Quiet.TButton", command=self._lookup_selection)
        selection_button.pack(side="left", padx=(3, 0))
        tooltip(selection_button, "在原句或句内单词页拖动选中一个词组，再点击这里查看释义。")
        self.save_term_button = ttk.Button(lookup_row, text="收藏词语", style="Secondary.TButton",
                                           state="disabled", command=self._save_term)
        self.save_term_button.pack(side="right", padx=(2, 0))
        lookup_body = ttk.Frame(lookup_block)
        lookup_body.pack(fill="x")
        self.lookup_text = tk.Text(lookup_body, height=3, bg=SURFACE, fg=INK, insertbackground=ACCENT, selectbackground="#31566A", selectforeground=INK, font=(FONT, 11), wrap="word", relief="flat", padx=14, pady=9, spacing3=4, highlightthickness=0, exportselection=False)
        self.lookup_text.pack(side="left", fill="x", expand=True)
        lookup_scroll = ttk.Scrollbar(lookup_body, orient="vertical", command=self.lookup_text.yview)
        lookup_scroll.pack(side="right", fill="y")
        self.lookup_text.configure(yscrollcommand=lookup_scroll.set)
        self.lookup_text.tag_configure("headword", foreground=ACCENT, font=("Segoe UI", 15, "bold"))
        self.lookup_text.tag_configure("source", foreground=MUTED, font=(FONT, 9))
        self.lookup_text.tag_configure("hint", foreground=MUTED, font=(FONT, 10))
        self.lookup_text.insert("1.0", "点击原句中的英文单词，释义将在这里展开。\n也可以选中一个词组后，点击「查选中」。", "hint")
        self.lookup_text.configure(state="disabled")
        self.provenance_button = ttk.Button(right, text="来源与核对信息  ▸", style="Quiet.TButton", command=self._toggle_provenance)
        self.provenance_button.pack(side="bottom", anchor="w", pady=(2, 0))
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)
        reading, vocabulary = ttk.Frame(self.notebook), ttk.Frame(self.notebook)
        self.notebook.add(reading, text="原句与表达")
        self.notebook.add(vocabulary, text="句内单词")
        self.reading = self._text(reading)
        self.vocabulary = self._text(vocabulary)
        self.reading.bind("<ButtonRelease-1>", self._word_clicked)
        self._empty_detail("在左侧查找一句台词、一个术语，\n或浏览这款游戏的全部文本。")

    def _text(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical")
        scroll.pack(side="right", fill="y")
        text = tk.Text(frame, bg=PAPER, fg=PAPER_INK, insertbackground=PAPER_ACCENT, selectbackground=PAPER_ACCENT, selectforeground="#FFFFFF", font=(FONT, 12), wrap="word", relief="flat", padx=22, pady=15, spacing1=2, spacing3=7, highlightthickness=0, exportselection=False)
        text.pack(side="left", fill="both", expand=True)
        scroll.configure(command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.tag_configure("eyebrow", foreground=PAPER_MUTED, font=(FONT, 10), spacing1=2, spacing3=4)
        text.tag_configure("section", foreground=PAPER_ACCENT, font=(FONT, 11, "bold"), spacing1=9, spacing3=4)
        text.tag_configure("english", foreground=PAPER_INK, font=(ENGLISH_FONT, 22), spacing1=1, spacing2=4, spacing3=9)
        text.tag_configure("chinese", foreground=PAPER_MUTED, font=(FONT, 13), spacing2=3, spacing3=10)
        text.tag_configure("muted", foreground=PAPER_MUTED, font=(FONT, 10), spacing3=4)
        text.tag_configure("warning", foreground="#7B4311", font=(FONT, 10))
        text.tag_configure("term", foreground=PAPER_ACCENT, font=("Segoe UI", 13, "bold"), spacing1=6, spacing3=3)
        text.tag_configure("welcome", foreground=PAPER_INK, font=(FONT, 17, "bold"), spacing1=12, spacing3=10)
        text.tag_configure("guidance", foreground=PAPER_MUTED, font=(FONT, 11), spacing2=5, spacing3=10)
        text.tag_configure("clickword", underline=False)
        text.tag_configure("pickedword", foreground=PAPER_ACCENT, background=PAPER_MARK, underline=True)
        text.tag_configure("hoverword", underline=True, foreground=PAPER_ACCENT)
        text.tag_raise("sel")
        text.bind("<Motion>", lambda event: self._hover_word(text, event))
        text.bind("<Leave>", lambda event: text.tag_remove("hoverword", "1.0", "end"))
        text.configure(state="disabled")
        return text

    def _open_notes(self):
        from .notes_ui import NotesWindow
        if self.notes_window and self.notes_window.window.winfo_exists():
            self.notes_window.window.deiconify()
            self.notes_window.window.lift()
            self.notes_window.reload()
        else:
            self.notes_window = NotesWindow(self)

    def _window_chrome(self):
        try:
            import os
            import ctypes
            from ctypes import wintypes
            if os.name != "nt" or self.closed:
                return
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user.GetAncestor.restype = wintypes.HWND
            user.GetParent.argtypes = [wintypes.HWND]
            user.GetParent.restype = wintypes.HWND
            user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user.GetWindowThreadProcessId.restype = wintypes.DWORD
            user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user.SetWindowPos.restype = wintypes.BOOL
            # Tk's widget ID can precede its outer wrapper during initial mapping.
            # Resolve the mapped WM frame, then its GA_ROOT, on both delayed calls.
            frame = int(self.root.wm_frame(), 0)
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
            def colorref(color):
                red, green, blue = (int(color[offset:offset+2], 16) for offset in (1, 3, 5))
                return red | green << 8 | blue << 16
            caption = ctypes.c_uint(colorref(BG))
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))
            title = ctypes.c_uint(colorref(INK))
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(title), ctypes.sizeof(title))
            border = ctypes.c_uint(colorref(LINE))
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
            # Refresh only this frame; preserve size, position, z-order and focus.
            user.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
        except (OSError, AttributeError, ValueError, tk.TclError):
            pass

    def _toggle_provenance(self):
        if not self.current_entry:
            return
        self.show_provenance = not self.show_provenance
        self._show_entry(self.current_entry)
        if self.show_provenance:
            self.reading.see("end")

    def _hover_word(self, text, event):
        text.tag_remove("hoverword", "1.0", "end")
        index = text.index(f"@{event.x},{event.y}")
        if "clickword" in text.tag_names(index):
            bounds = text.tag_prevrange("clickword", f"{index}+1c")
            if bounds and text.compare(bounds[0], "<=", index) and text.compare(index, "<", bounds[1]):
                text.tag_add("hoverword", *bounds)
                text.configure(cursor="hand2")
                return
        text.configure(cursor="xterm")

    def _submit(self, work, success, label=None, mutation=False, failure=None):
        if mutation and self.operation_busy:
            self.status.set("上一项操作仍在进行，请稍候。")
            return
        if label:
            self.status.set(label)
        if mutation:
            self.operation_busy = True
            self._button_states()
        future = self.executor.submit(work)

        def done(result):
            try:
                value = result.result()
                self.events.put((success, (value,), mutation))
            except Exception as error:
                self.events.put((failure or self._error, (str(error),), mutation))
        future.add_done_callback(done)

    def _drain(self):
        if self.closed:
            return
        for _ in range(40):
            try:
                callback, args, mutation = self.events.get_nowait()
            except queue.Empty:
                break
            if mutation:
                self.operation_busy = False
                self._button_states()
            try:
                callback(*args)
            except Exception as error:
                self._error(str(error))
        if self.pending_launch is not None and not self.operation_busy:
            game_id, self.pending_launch = self.pending_launch, None
            self._request_launch_main(game_id)
        self.root.after(70, self._drain)

    def _error(self, error):
        self.status.set("未完成：" + brief(error, 160))
        messagebox.showerror("操作未完成", error, parent=self.root)

    def _load_games(self, selected=None):
        selected = self.game_id if selected is None else selected
        self.games_generation += 1
        generation = self.games_generation
        self._submit(self.service.games, lambda games: self._show_games(games, selected) if generation == self.games_generation else None)

    def _show_games(self, games, selected):
        first = self.initial_load
        self.initial_load = False
        self.game_rows = {str(item["id"]): item for item in games}
        if selected is None and self.game_id in self.game_rows:
            selected = self.game_id
        self.game_ids = [None, *self.game_rows]
        self.game_picker.configure(values=["全部游戏", *[game["title"] for game in self.game_rows.values()]])
        choice = str(selected) if selected is not None and str(selected) in self.game_rows else None
        self.game_picker.current(self.game_ids.index(choice))
        count = sum(int(game.get("count", 0)) for game in games)
        self.stat_text.set(f"离线文本库  ·  {len(games)} 款游戏  ·  {count:,} 句")
        self.status.set("点击英文单词查释义，选中词组后可直接查询。")
        self._game_selected()
        if hasattr(self.service, "session_info"):
            ids = [game["id"] for game in games if self._supports_native(game)]
            def session_specs():
                specs = {}
                for game_id in ids:
                    try:
                        specs[game_id] = self.service.session_info(game_id)
                    except Exception:
                        continue
                return specs
            self._submit(session_specs, self._prepare_sessions)
        if first and not games and getattr(self.service, "config", {}).get("roots"):
            self._submit(self.service.refresh, self._initial_refresh_done, "首次启动：正在建立游戏文本索引…", mutation=True)

    def _initial_refresh_done(self, report):
        self._load_games()
        if report.get("errors"):
            messagebox.showwarning("部分资料未能导入", "\n\n".join(str(error) for error in report["errors"]), parent=self.root)

    def _game_selected(self, event=None):
        index = self.game_picker.current()
        self.game_id = self.game_ids[index] if 0 <= index < len(self.game_ids) else None
        game = self.game_rows.get(self.game_id)
        if game:
            if game.get("panel_backend") == "electron":
                mode = "  ·  Electron 内置会话需从工作台启动"
            elif not self._supports_native(game):
                mode = "  ·  仅桌面检索与复习"
            else:
                mode = ""
            self.game_summary.configure(text=f"{game.get('count', 0):,} 条文本  ·  {game.get('paired', 0):,} 条中英对照" + mode)
        else:
            self.game_summary.configure(text="跨游戏检索  ·  选择单款游戏，即可启动或设置面板")
        self.category.set("全部类别")
        gid = self.game_id
        self._submit(lambda: self.service.categories(gid), lambda values: self.category_box.configure(values=["全部类别"] + values) if gid == self.game_id else None)
        self._button_states()
        if game and self._supports_native(game):
            self.native_health.pack(fill="x", padx=20, pady=(0, 8))
        else:
            self.native_health.pack_forget()
        self.native_health.select(self.game_id, self._supports_native_panel(game or {}))
        self._search(reset=True)

    def _button_states(self):
        game = self.game_rows.get(self.game_id, {})
        supported = self._supports_native(game)
        for label in ("启动游戏", "检查接入状态"):
            self.game_menu.entryconfigure(label, state="normal" if supported and not self.operation_busy else "disabled")
        for label in ("启用游戏双语", "恢复原版显示"):
            self.game_menu.entryconfigure(label, state="normal" if game.get("supports_patch", supported) and not self.operation_busy else "disabled")
        self.game_menu.entryconfigure("查看完整支持范围", state="normal" if game else "disabled")
        self.game_menu.entryconfigure("查看面板运行检查…", state="normal" if game else "disabled")
        for label in ("游戏内面板与主题…", "移除游戏内检索面板"):
            self.game_menu.entryconfigure(label, state="normal" if game and not self.operation_busy else "disabled")
        for button in self.mutation_buttons:
            button.configure(state="disabled" if self.operation_busy else "normal")
        self.launch_button.configure(state="normal" if supported and not self.operation_busy else "disabled")
        profile_supported = bool(game.get("supports_profile", game.get("adapter") == "configured"))
        self.library_menu.entryconfigure("导出当前游戏接入配置…", state="normal" if profile_supported else "disabled")
        self.library_menu.entryconfigure("创建双语游戏启动入口", state="normal" if game.get("supports_launcher", profile_supported) else "disabled")
        self.library_menu.entryconfigure("从接入配置文件导入…", state="normal")

    @staticmethod
    def _supports_native(game):
        if "supports_native" in game:
            return bool(game["supports_native"])
        # Compatibility with the original two adapters; new adapters declare capability.
        return game.get("adapter") in {"lis", "subnautica"}

    @staticmethod
    def _supports_native_panel(game):
        return Desktop._supports_native(game) and (game.get("panel_backend") == "electron" or not game.get("corpus_only", False))

    def _coverage(self):
        game = self.game_rows.get(self.game_id)
        if not game:
            self.status.set("请先选择单款游戏，查看它的文本覆盖和双语支持范围。")
            return
        coverage = str(game.get("coverage", "尚无覆盖说明。"))
        from .native_overlay import installed
        native = installed(self.service, self.game_id)
        if game.get("panel_backend") == "electron":
            panel = ("在面板设置中启用后，从工作台启动游戏。F8 打开检索并可切换原位双语。"
                     "原位译文仅覆盖可可靠匹配的可见文字。")
        else:
            panel = "已接入游戏内半透明检索：F8 打开/收起，Esc 关闭。" if native else "可从「游戏 → 游戏内面板与主题」检查接入条件并安装；兼容性需要首次实机验证。"
        messagebox.showinfo(game["title"] + " · 支持范围", f"{game.get('status', '')}\n\n{coverage}\n\n来源目录：{game.get('root', '')}\n\n{panel}\n检索不自动跟随实时对白。", parent=self.root)

    def _debounce(self, *_):
        if self.search_after is not None:
            self.root.after_cancel(self.search_after)
        self.search_after = self.root.after(300, lambda: self._search(reset=True))

    def _clear_filters(self):
        self.category.set("全部类别")
        self.query.set("")
        self._search(reset=True)
        self.search_box.focus_set()

    def _focus_results(self, event=None):
        if self.result_cards.items:
            self.result_cards.canvas.focus_set()
            if self.result_cards.selected_id is None:
                self.result_cards.select(self.result_cards.items[0]["id"])
        return "break"

    def _search(self, reset=False):
        if self.search_after is not None:
            self.root.after_cancel(self.search_after)
            self.search_after = None
        if reset:
            self.offset = 0
        self.search_generation += 1
        generation = self.search_generation
        query, game_id, offset = self.query.get().strip(), self.game_id, self.offset
        category = "" if self.category.get() == "全部类别" else self.category.get()
        self.result_label.configure(text="正在查找原句…")
        self._submit(lambda: self.service.search(query=query, game_id=game_id, category=category, limit=self.PAGE_SIZE, offset=offset), lambda result: self._show_results(result, generation))

    def _show_results(self, result, generation):
        if generation != self.search_generation:
            return
        self.detail_generation += 1
        self.results = {}
        self.total = int(result.get("total", 0))
        for entry in result.get("items", []):
            iid = str(entry["id"])
            self.results[iid] = entry
        self.result_cards.set_items(list(self.results.values()))
        self.result_label.configure(text=f"{self.total:,} 条匹配原句" if self.query.get().strip() else f"{self.total:,} 条可读文本")
        page_count = max(1, (self.total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page_label.configure(text=f"{self.offset // self.PAGE_SIZE + 1} / {page_count}")
        self.prev_button.configure(state="normal" if self.offset > 0 else "disabled")
        self.next_button.configure(state="normal" if self.offset + self.PAGE_SIZE < self.total else "disabled")
        self.current_entry = None
        if self.results and (self.game_id is not None or self.query.get().strip()):
            self.result_cards.select(next(iter(self.results)))
        elif self.results:
            self._empty_detail("在左侧输入中文或英文关键词，\n或选择一款游戏，浏览它的全部文本。")
        else:
            self._empty_detail("没有找到匹配文本。\n\n试试中文关键词、英文单词，或清空搜索查看全部内容。")

    def _page(self, direction):
        self.offset = max(0, self.offset + direction * self.PAGE_SIZE)
        self._search()

    def _entry_selected(self, selected_id=None):
        identifier = self.results.get(str(selected_id), {}).get("id")
        if identifier is None:
            return
        self.detail_generation += 1
        generation = self.detail_generation
        self.current_entry = None
        self._clear_lookup_note()
        self._note_buttons()
        self.copy_button.configure(state="disabled")
        self._submit(lambda: self.service.entry(identifier), lambda entry: self._show_entry(entry) if generation == self.detail_generation else None)

    def _empty_detail(self, message):
        self.detail_context.configure(text="")
        self.provenance_button.configure(state="disabled", text="来源与核对信息  ▸")
        self.copy_button.configure(state="disabled")
        self.save_sentence_button.configure(state="disabled")
        for widget in (self.reading, self.vocabulary):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("end", "选择原句开始阅读\n", "welcome")
            widget.insert("end", message + "\n", "guidance")
            widget.configure(state="disabled")
        self.lookup_query.set("")
        self.lookup_text.configure(state="normal")
        self.lookup_text.delete("1.0", "end")
        self.lookup_text.insert("end", "点击英文单词，即可在这里查看释义。\n也可以直接输入单词或词组查询。", "hint")
        self.lookup_text.configure(state="disabled")

    def _show_entry(self, entry):
        if not self.current_entry or self.current_entry.get("id") != entry.get("id"):
            self.show_provenance = False
            self.lookup_query.set("")
            self.lookup_text.configure(state="normal")
            self.lookup_text.delete("1.0", "end")
            self.lookup_text.insert("end", "点击上方英文单词查看释义，\n或选中词组后点击「查选中」。", "hint")
            self.lookup_text.configure(state="disabled")
        self.current_entry = entry
        self.copy_button.configure(state="normal")
        self._note_buttons()
        text = self.reading
        text.configure(state="normal")
        text.delete("1.0", "end")
        game = self.game_rows.get(entry.get("game_id"), {}).get("title") if not self.game_id else ""
        line = "  ·  ".join(str(v) for v in (game, entry.get("category"), entry.get("speaker")) if v)
        self.detail_context.configure(text=brief(line or "双语阅读", 32))
        start = text.index("end-1c")
        english = display_text(entry.get("en")) or "（尚无英文文本）"
        text.insert("end", english + "\n", "english")
        for match in re.finditer(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*", english):
            text.tag_add("clickword", f"{start}+{match.start()}c", f"{start}+{match.end()}c")
        text.insert("end", (display_text(entry.get("zh")) or "（尚无中文文本，未自动编造译文）") + "\n", "chinese")
        note = entry.get("note")
        if note:
            text.insert("end", "句子讲解\n", "section")
            text.insert("end", str(note) + "\n")
        text.insert("end", "词组与表达\n", "section")
        phrases = entry.get("phrases") or []
        if not phrases:
            text.insert("end", "该句暂无词组注释。可选中短语后查询词典。\n", "muted")
        for item in phrases:
            text.insert("end", str(item.get("term", "")) + "\n", "term")
            text.insert("end", str(item.get("meaning", "")) + "\n")
            if item.get("source"):
                text.insert("end", self._source_label(item["source"]) + "\n", "muted")
        warnings = entry.get("warnings", [])
        warning_count = f" · {len(warnings)} 项提示" if warnings else ""
        self.provenance_button.configure(state="normal", text="来源与核对信息" + warning_count + ("  ▾" if self.show_provenance else "  ▸"))
        if self.show_provenance:
            text.insert("end", "来源与核对信息\n", "eyebrow")
            for warning in warnings:
                text.insert("end", str(warning) + "\n", "warning")
            for label, key in (("资源", "source"), ("命名空间", "namespace"), ("文本标识", "key")):
                if entry.get(key):
                    text.insert("end", label + "：" + str(entry[key]) + "\n", "muted")
        text.configure(state="disabled")
        text.yview_moveto(0)
        words = self.vocabulary
        words.configure(state="normal")
        words.delete("1.0", "end")
        entries = entry.get("words") or []
        words.insert("end", f"本句单词 · {len(entries)} 项\n", "eyebrow")
        words.insert("end", "词典与术语释义；多义词请结合原句理解。\n\n", "muted")
        if not entries:
            words.insert("end", "暂无可用词典注释，可在下方输入单词查询。", "muted")
        for item in entries:
            words.insert("end", str(item.get("term", "")) + "\n", "term")
            words.insert("end", str(item.get("meaning", "")) + "\n")
            if item.get("source"):
                words.insert("end", self._source_label(item["source"]) + "\n\n", "muted")
        words.configure(state="disabled")
        words.yview_moveto(0)

    def _word_clicked(self, event):
        if self.reading.tag_ranges("sel"):
            return
        index = self.reading.index(f"@{event.x},{event.y}")
        if "clickword" not in self.reading.tag_names(index):
            return
        for start, end in zip(self.reading.tag_ranges("clickword")[::2], self.reading.tag_ranges("clickword")[1::2]):
            if self.reading.compare(start, "<=", index) and self.reading.compare(index, "<", end):
                self.reading.tag_remove("pickedword", "1.0", "end")
                self.reading.tag_add("pickedword", start, end)
                self.lookup_query.set(self.reading.get(start, end))
                self._lookup()
                break

    def _lookup_selection(self):
        active = self.vocabulary if self.notebook.index("current") == 1 else self.reading
        try:
            selected = active.get("sel.first", "sel.last").strip()
            self.reading.tag_remove("pickedword", "1.0", "end")
            if active is self.reading:
                self.reading.tag_add("pickedword", "sel.first", "sel.last")
            self.lookup_query.set(selected)
            self._lookup()
        except tk.TclError:
            self.status.set("请先在原句中拖动选中一个单词或词组。")

    def _lookup(self):
        term = self.lookup_query.get().strip()
        if not term:
            return
        marked = self.reading.tag_ranges("pickedword")
        if marked and self.reading.get(*marked[:2]).strip().casefold() != term.casefold():
            self.reading.tag_remove("pickedword", "1.0", "end")
        game_id = self.current_entry.get("game_id") if self.current_entry else self.game_id
        generation = self.detail_generation
        self._submit(lambda: self.service.lookup(term, game_id),
                     lambda result: self._show_lookup(term, result) if generation == self.detail_generation else None)

    def _show_lookup(self, term, result):
        if self.lookup_query.get().strip() != term:
            return
        def normalized(value):
            return " ".join(value.casefold().replace("’", "'").split())

        context = [item for item in (self.current_entry or {}).get("context_phrases", [])
                   if item.get("meaning") and normalized(item.get("term", "")) == normalized(term)]
        meanings = {item["meaning"] for item in context}
        hits = [(item, True) for item in context]
        hits.extend((item, False) for item in result if item.get("meaning") not in meanings
                    and (not context or item.get("source") != "未收录"))
        self.lookup_text.configure(state="normal")
        self.lookup_text.delete("1.0", "end")
        if hits:
            for index, (item, in_context) in enumerate(hits):
                if index:
                    self.lookup_text.insert("end", "\n")
                heading = str(item.get("term", term)) + ("  ·  原句注释" if in_context else "")
                self.lookup_text.insert("end", heading + "\n", "headword")
                self.lookup_text.insert("end", str(item.get("meaning", "")) + "\n")
                self.lookup_text.insert("end", self._source_label(item.get("source", "来源未标注")) + "\n", "source")
        else:
            self.lookup_text.insert("end", term + "\n", "headword")
            self.lookup_text.insert("end", "本地词典暂无释义，可尝试原形或更短的词组。", "hint")
        self.lookup_text.configure(state="disabled")
        self.lookup_text.yview_moveto(0)
        self.lookup_note = None
        entry = self.current_entry
        key = note_key(term)
        if entry and key and f" {key} " in f" {note_key(display_text(entry['en']))} ":
            item = hits[0][0] if hits else {}
            self.lookup_note = (entry["id"], term, key, item.get("meaning", "") if item.get("source") != "未收录" else "",
                                item.get("source", "原句注释") if item and item.get("source") != "未收录" else "")
        self._note_buttons()

    def _clear_lookup_note(self, *_):
        self.lookup_note = None
        if hasattr(self, "save_term_button"):
            self.save_term_button.configure(state="disabled")

    def _note_buttons(self):
        self.save_sentence_button.configure(state="normal" if self.current_entry and not self.note_busy else "disabled")
        same = self.lookup_note and self.current_entry and self.lookup_note[0] == self.current_entry["id"]
        self.save_term_button.configure(state="normal" if same and not self.note_busy else "disabled")

    def _save_sentence(self):
        if self.current_entry:
            self._save_note("sentence", display_text(self.current_entry["en"]), "",
                            self.current_entry.get("note") or "", "原句注释" if self.current_entry.get("note") else "")

    def _save_term(self):
        if self.current_entry and self.lookup_note and self.lookup_note[0] == self.current_entry["id"]:
            _, term, key, meaning, source = self.lookup_note
            self._save_note("phrase" if " " in key else "word", term, key, meaning, source)

    def _save_note(self, kind, target, key, meaning, source):
        if self.note_busy or not self.current_entry:
            return
        entry = dict(self.current_entry)
        title = self.game_rows.get(entry["game_id"], {}).get("title") or "游戏文本"
        # Copy the exact visible entry; the worker must not read changing UI state.
        snapshot = dict(entry, en=display_text(entry["en"]), zh=display_text(entry["zh"]))
        self.note_busy = True
        self._note_buttons()

        def finished(created):
            self.note_busy = False
            self._note_buttons()
            self.status.set("已收藏，可在「收藏与复习」查看。" if created else "这条内容已在收藏中。")

        def failed(error):
            self.note_busy = False
            self._note_buttons()
            self.status.set("收藏未保存，请重试：" + brief(error, 100))

        self._submit(lambda: Notebook(self.service.data_dir).capture(snapshot, title, kind, target, key, meaning, source),
                     finished, failure=failed)

    @staticmethod
    def _source_label(value):
        source = str(value)
        if "词形候选" in source:
            return ("ECDICT · " if "ECDICT" in source else "") + "词形候选 " + source.split("词形候选", 1)[1].strip("；; ")
        if "ECDICT" in source:
            return "ECDICT · 通用词典释义，需结合原句"
        if "通用词典" in source:
            return "通用词典释义 · 需结合原句"
        return brief(source, 90)

    def _copy(self):
        if not self.current_entry:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(str(self.current_entry.get(key) or "") for key in ("en", "zh")))
        self.status.set("已复制中英文原句。")

    def _operation_done(self, result, selected=None):
        if selected is None and isinstance(result, dict):
            selected = result.get("id")
        self._load_games(selected)
        if isinstance(result, dict):
            if "games" in result:
                lines = [f"{game.get('title', '文本库')}：{game.get('count', 0):,} 条文本，{game.get('paired', 0):,} 条中英配对。" for game in result.get("games", [])]
                lines.append(f"新增本地词典释义：{result.get('dictionary_added', 0):,} 项。")
                if result.get("errors"):
                    lines.extend(["\n以下资料未能更新：", *[str(error) for error in result["errors"]]])
                summary = "\n".join(lines)
            elif "count" in result:
                summary = f"{result.get('title', '文本库')}\n\n已导入 {result.get('count', 0):,} 条文本，其中 {result.get('paired', 0):,} 条中英配对。\n需留意的记录：{result.get('warnings', 0):,} 条。\n\n{result.get('coverage', '')}"
                if result.get("message"):
                    summary += "\n\n" + str(result["message"])
                if result.get("launcher"):
                    summary += "\n\n双语启动入口：\n" + str(result["launcher"])
            else:
                summary = result.get("message") or json.dumps(result, ensure_ascii=False, indent=2)
        else:
            summary = str(result)
        if isinstance(result, dict) and result.get("errors"):
            messagebox.showwarning("更新完成，部分资料需检查", summary, parent=self.root)
        else:
            messagebox.showinfo("操作完成", summary, parent=self.root)

    def _action(self, action):
        if not self.game_id:
            return
        gid = self.game_id
        if action == "launch":
            self.request_launch(gid)
            return
        labels = {"enable": "正在启用双语，请保持游戏关闭…", "disable": "正在恢复原版显示，请保持游戏关闭…", "launch": "正在启动游戏…", "status": "正在检查接入状态…"}
        self._submit(lambda: self.service.action(gid, action), lambda result: self._operation_done(result, gid), labels[action], mutation=True)

    def _show_native_health(self, game_id=None):
        """Explicit details action, also used by the native-panel setup window."""
        gid = str(game_id) if game_id is not None else self.game_id
        if gid is None:
            self.status.set("请先在顶部选择要检查的游戏。")
            return
        if gid in self.game_ids and gid != self.game_id:
            self.game_picker.current(self.game_ids.index(gid))
            self._game_selected()
        self.native_health.select(gid, self._supports_native_panel(self.game_rows.get(gid, {})))
        self.native_health.show_details()

    def _native_panel(self, action):
        if self.operation_busy:
            return
        if not self.game_id:
            if action == "install":
                self._choose_native_game()
            return
        if action == "install" and not self._supports_native(self.game_rows.get(self.game_id, {})):
            self._choose_native_game()
            return
        from . import native_overlay
        gid = self.game_id
        if action == "install":
            from .native_setup import open_setup
            self._submit(lambda: native_overlay.inspect(self.service, gid),
                         lambda report: open_setup(self, gid, report), "正在检查游戏内面板接入条件…", mutation=True)
            return
        label = "正在安装游戏内半透明检索…" if action == "install" else "正在移除游戏内检索…"
        self._submit(lambda: getattr(native_overlay, action)(self.service, gid),
                     lambda result: self._operation_done(result, gid), label, mutation=True)

    def _choose_native_game(self):
        window = tk.Toplevel(self.root)
        window.title("为游戏加入内置面板")
        window.configure(bg=BG)
        window.resizable(False, False)
        window.transient(self.root)
        body = ttk.Frame(window, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="选择要加入内置面板的游戏", font=(FONT, 14, "bold")).pack(anchor="w")
        hint = ("当前文本库尚未关联游戏程序，请先接入游戏目录。\n也可选择下面已有的游戏，为它安装内置面板。"
                if self.game_id and not self._supports_native(self.game_rows.get(self.game_id, {})) else
                "工作台会检查接入条件，并将面板安装到所选游戏。\n安装后启动游戏，按 F8 打开半透明检索。")
        ttk.Label(body, text=hint,
                  style="Muted.TLabel", wraplength=460).pack(anchor="w", pady=(8, 16))
        games = [(gid, row) for gid, row in self.game_rows.items() if self._supports_native(row)]
        picker = ttk.Combobox(body, state="readonly", width=38, values=[row["title"] for _, row in games])
        picker.pack(fill="x", pady=(0, 18))
        if games:
            picker.current(0)
        else:
            ttk.Label(body, text="先接入游戏并建立文本库，即可继续安装。", style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")

        def connect():
            window.destroy()
            self._inspect_new_game()

        def next_step():
            index = picker.current()
            if not 0 <= index < len(games):
                return
            gid = games[index][0]
            window.destroy()
            self.game_picker.current(self.game_ids.index(gid))
            self._game_selected()
            self._native_panel("install")

        ttk.Button(buttons, text="＋ 接入新游戏…", command=connect).pack(side="left")
        ttk.Button(buttons, text="检查并安装…", style="Accent.TButton", command=next_step,
                   state="normal" if games else "disabled").pack(side="right")
        window.bind("<Escape>", lambda _: window.destroy())
        picker.focus_set()

    def _inspect_new_game(self):
        if self.operation_busy:
            return
        path = filedialog.askdirectory(title="选择要加入双语模式的游戏目录", parent=self.root)
        if path:
            self._submit(lambda: self.service.inspect_game(path), self._show_connect_wizard,
                         "正在识别游戏与中英语言资源…", mutation=True)

    @staticmethod
    def _lines(value):
        if isinstance(value, (list, tuple)):
            return "\n".join(str(item) for item in value)
        return str(value or "")

    @staticmethod
    def _probe_details(probe):
        details = probe.get("details")
        if not isinstance(details, dict):
            return Desktop._lines(details) or "将按文本标识配对中英文，保留已有游戏术语。"
        lines = []
        if details.get("archive_count"):
            lines.append(f"已检查 {details['archive_count']} 个游戏资源包。")
        if details.get("packs"):
            lines.append(f"已检查 {len(details['packs'])} 个游戏资源包。")
        if details.get("project_version"):
            lines.append("游戏资源版本：" + brief(details["project_version"], 120))
        if "native_loader_known" in details:
            lines.append("已识别游戏支持的模组加载方式。" if details["native_loader_known"] else "尚未确认游戏如何加载双语资源，需要补充接入配置。")
        lines.append("双语显示覆盖本次识别的语言资源；贴图中的文字和运行时生成内容需单独适配。")
        return "\n".join(lines)

    def _show_connect_diagnostics(self, probe):
        from .onboarding import describe

        title, advice, engine = describe(probe)
        window = tk.Toplevel(self.root)
        window.title("新游戏接入 · 诊断")
        window.configure(bg=BG)
        window.transient(self.root)
        window.geometry("640x370")
        window.minsize(540, 350)
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=engine, style="Warning.Badge.TLabel").pack(anchor="w")
        ttk.Label(frame, text=title, style="ReaderTitle.TLabel").pack(anchor="w", pady=(10, 6))
        summary = ttk.Label(frame, text=advice, style="Muted.TLabel", justify="left")
        summary.pack(fill="x", pady=(0, 10))
        summary.bind("<Configure>", lambda event: summary.configure(wraplength=max(1, event.width - 4)))
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(12, 0))

        def follow(callback):
            window.destroy()
            callback()

        ttk.Button(buttons, text="重选目录", style="Primary.TButton",
                   command=lambda: follow(self._inspect_new_game)).pack(side="left")
        ttk.Button(buttons, text="导入接入配置",
                   command=lambda: follow(self._import_profile)).pack(side="left", padx=6)
        ttk.Button(buttons, text="仅导入文本",
                   command=lambda: follow(self._import_menu)).pack(side="left")
        close = ttk.Button(buttons, text="关闭", command=window.destroy)
        close.pack(side="right")
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        details = tk.Text(body, bg=SURFACE, fg=INK, relief="flat", wrap="word",
                          padx=12, pady=10, font=(FONT, 10), height=5)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=details.yview)
        scrollbar.pack(side="right", fill="y")
        details.pack(side="left", fill="both", expand=True)
        details.configure(yscrollcommand=scrollbar.set)
        notes = ["所选目录：" + str(probe.get("root", "")), "本次仅检查，未修改游戏文件。"]
        if probe.get("details"):
            notes.append(self._probe_details(probe))
        if probe.get("executables"):
            notes.append("候选启动程序：" + "、".join(probe["executables"][:6]))
        notes.extend(self._lines(probe.get("warnings")).splitlines()[:8])
        notes.extend(f"{source}：{message}" for source, message in probe.get("issues", [])[:8])
        details.insert("1.0", "\n\n".join(notes)[:5000])
        details.configure(state="disabled")
        window.bind("<Escape>", lambda event: window.destroy())
        window.grab_set()
        close.focus_set()
        self.status.set("未完成游戏接入 · 未修改游戏文件")

    def _show_connect_wizard(self, probe):
        if not isinstance(probe, dict):
            raise ValueError("游戏识别结果格式不正确。")
        if probe.get("connectable") is False:
            self._show_connect_diagnostics(probe)
            return
        probe = copy.deepcopy(probe)
        corpus_only = bool(probe.get("corpus_only"))
        window = tk.Toplevel(self.root)
        window.title("新游戏接入 · 确认语料" if corpus_only else "新游戏接入 · 确认双语显示")
        window.configure(bg=PANEL)
        window.geometry(f"760x{min(700, max(560, self.root.winfo_screenheight() - 110))}")
        window.minsize(650, 540)
        window.transient(self.root)
        window.grab_set()
        outer = ttk.Frame(window, padding=18)
        outer.pack(fill="both", expand=True)
        heading = ttk.Label(outer, text="为这款游戏建立双语学习语料" if corpus_only else "为这款游戏加入双语模式", style="Section.TLabel")
        heading.pack(anchor="w", pady=(0, 8))
        ttk.Label(outer, text=brief(probe.get("root", ""), 110), style="Muted.TLabel").pack(anchor="w", pady=(0, 12))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        title = tk.StringVar(value=str(probe.get("title") or Path(probe.get("root") or "新游戏").name))
        executables = [str(item) for item in probe.get("executables", []) if item]
        executable_default = str(probe.get("executable") or (executables[0] if executables else ""))
        window_default = str(probe.get("window_executable") or executable_default)
        executables = list(dict.fromkeys([*executables, *[item for item in (executable_default, window_default) if item]]))
        executable = tk.StringVar(value=executable_default)
        window_executable = tk.StringVar(value=window_default)
        options = probe.get("options") if isinstance(probe.get("options"), dict) else {}
        order_values = {"中文在前，英文在后": "zh-en", "英文在前，中文在后": "en-zh"}
        layout_values = {"自动：按文本类型选择": "auto", "分成两行": "newline", "显示在同一行": "inline"}
        order = tk.StringVar(value=next((label for label, value in order_values.items() if value == options.get("order")), next(iter(order_values))))
        layout = tk.StringVar(value=next((label for label, value in layout_values.items() if value == options.get("layout")), next(iter(layout_values))))
        fields = []
        field_specs = [
            ("游戏名称", title, None), ("启动程序", executable, executables),
            ("游戏窗口程序", window_executable, executables),
        ]
        if not corpus_only:
            field_specs.extend([("中英顺序", order, list(order_values)), ("文字排列", layout, list(layout_values))])
        for index, (label, variable, values) in enumerate(field_specs):
            ttk.Label(form, text=label).grid(row=index, column=0, sticky="w", padx=(0, 14), pady=4)
            if values is None:
                field = ttk.Entry(form, textvariable=variable)
            else:
                field = ttk.Combobox(form, textvariable=variable, values=values, state="readonly")
            field.grid(row=index, column=1, sticky="ew", pady=4)
            fields.append((field, "normal" if values is None else "readonly"))
        source = probe.get("source_locale") or "未识别"
        target = probe.get("target_locale") or "未识别"
        ttk.Label(form, text="识别到的语言").grid(row=len(field_specs), column=0, sticky="w", padx=(0, 14), pady=5)
        ttk.Label(form, text=f"英文：{source}    中文：{target}", style="Muted.TLabel").grid(row=len(field_specs), column=1, sticky="w", pady=5)
        ttk.Label(outer, text="游戏窗口程序用于返回游戏；若有启动器，请选实际运行游戏的程序。", style="Muted.TLabel", wraplength=700).pack(anchor="w", pady=(7, 10))

        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        progress = tk.StringVar(value="确认后只读建立词句库；不修改游戏文件。" if corpus_only else "确认后生成双语资源、建立词句库并创建启动入口。")
        ttk.Label(footer, textvariable=progress, style="Muted.TLabel", wraplength=700).pack(anchor="w", pady=(0, 8))
        button_row = ttk.Frame(footer)
        button_row.pack(fill="x")
        cancel = ttk.Button(button_row, text="取消", command=window.destroy)
        cancel.pack(side="left")
        connect = ttk.Button(button_row, text="接入双语语料" if corpus_only else "接入并启用双语", style="Accent.TButton")
        connect.pack(side="right")
        preview_frame = ttk.Frame(outer)
        preview_frame.pack(fill="both", expand=True)
        preview = tk.Text(preview_frame, bg=BG, fg=INK, wrap="word", relief="flat", font=(FONT, 10), padx=12, pady=10, height=7)
        preview.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(preview_frame, orient="vertical", command=preview.yview)
        scrollbar.pack(side="right", fill="y")
        preview.configure(yscrollcommand=scrollbar.set)
        pairs = probe.get("pairs") or []
        details = self._probe_details(probe)
        from .onboarding import describe
        next_step, route, engine = describe(probe)
        explanation = [f"{engine}  ·  已识别 {len(pairs)} 组候选语言资源。", next_step + "\n" + route, details]
        if probe.get("warnings"):
            explanation.extend(["\n需要留意", self._lines(probe["warnings"])])
        explanation.append("\n游玩中可用 Ctrl+Shift+B 呼出桌面检索面板，Esc 收起并返回游戏。建议窗口化或无边框显示；面板不自动跟随实时对白。")
        preview.insert("1.0", "\n\n".join(explanation))
        preview.configure(state="disabled")
        connecting = {"active": False}

        def close_wizard():
            if not connecting["active"]:
                window.destroy()

        def failed(error):
            if not window.winfo_exists():
                self._error(error)
                return
            connecting["active"] = False
            connect.configure(state="normal")
            cancel.configure(state="normal")
            for field, state in fields:
                field.configure(state=state)
            progress.set("未完成接入，请根据提示调整后重试。")
            self.status.set("接入未完成：" + brief(error, 120))
            messagebox.showerror("接入未完成", error, parent=window)

        def connected(result):
            connecting["active"] = False
            if window.winfo_exists():
                window.grab_release()
                window.destroy()
            self._operation_done(result, result.get("id") if isinstance(result, dict) else None)
            if isinstance(result, dict) and result.get("id"):
                from . import native_overlay
                from .native_setup import open_setup
                gid = result["id"]
                self._submit(lambda: native_overlay.inspect(self.service, gid),
                             lambda report: open_setup(self, gid, report), "正在检查内置面板接入条件…")

        def submit():
            if connecting["active"] or self.operation_busy:
                return
            if not title.get().strip() or not executable.get().strip():
                messagebox.showerror("尚缺少游戏信息", "请填写游戏名称并选择启动程序。", parent=window)
                return
            payload = copy.deepcopy(probe)
            payload.update(title=title.get().strip(), executable=executable.get(),
                           window_executable=window_executable.get() or executable.get())
            payload.setdefault("options", {})
            if not isinstance(payload["options"], dict):
                payload["options"] = {}
            if not corpus_only:
                payload["options"].update(order=order_values[order.get()], layout=layout_values[layout.get()])
            connecting["active"] = True
            connect.configure(state="disabled")
            cancel.configure(state="disabled")
            for field, _ in fields:
                field.configure(state="disabled")
            progress.set("正在只读提取并配对中英文语料…" if corpus_only else "正在生成与安装双语资源，请保持游戏关闭…")
            self._submit(lambda: self.service.connect_game(payload), connected,
                         "正在接入新游戏并建立词句库…", mutation=True, failure=failed)

        window.protocol("WM_DELETE_WINDOW", close_wizard)
        window.bind("<Escape>", lambda event: close_wizard())
        cancel.configure(command=close_wizard)
        connect.configure(command=submit)

    def _import_profile(self):
        path = filedialog.askopenfilename(title="选择游戏接入配置", filetypes=[("游戏接入配置", "*.json"), ("全部文件", "*.*")], parent=self.root)
        if not path:
            return

        def loaded(probe):
            root = probe.get("root") if isinstance(probe, dict) else None
            if root and Path(root).is_dir():
                self._show_connect_wizard(probe)
                return
            root = filedialog.askdirectory(title="为这份配置选择本机游戏目录", parent=self.root)
            if root:
                self._submit(lambda: self.service.read_profile(path, root=root), self._show_connect_wizard,
                             "正在匹配配置与本机游戏目录…", mutation=True)

        self._submit(lambda: self.service.read_profile(path), loaded, "正在读取游戏接入配置…", mutation=True)

    def _export_profile(self):
        if not self.game_id:
            self.status.set("请先选择要导出接入配置的游戏。")
            return
        gid = self.game_id
        game = self.game_rows.get(gid, {})
        name = re.sub(r'[<>:"/\\|?*]', "_", game.get("title", "游戏"))
        path = filedialog.asksaveasfilename(title="导出可移植的游戏接入配置", initialfile=name + "-接入配置.json", defaultextension=".json", filetypes=[("游戏接入配置", "*.json")], parent=self.root)
        if path:
            self._submit(lambda: self.service.export_profile(gid, path),
                         lambda _: messagebox.showinfo("接入配置已导出", "配置已保存：\n" + path + "\n\n在另一台电脑导入后，选择本机游戏目录即可重新接入。", parent=self.root),
                         "正在导出游戏接入配置…", mutation=True)

    def _create_launcher(self):
        if not self.game_id:
            self.status.set("请先选择要创建启动入口的游戏。")
            return
        gid = self.game_id
        from .native_overlay import installed
        tip = "F8 打开游戏内半透明检索。" if installed(self.service, gid) else "Ctrl+Shift+B 呼出桌面词句检索。"
        self._submit(lambda: self.service.create_launcher(gid),
                     lambda path: messagebox.showinfo("双语启动入口已创建", str(path) + "\n\n从这个入口启动游戏后，" + tip, parent=self.root),
                     "正在创建双语游戏启动入口…", mutation=True)

    def _prepare_sessions(self, specs):
        for game_id, info in specs.items():
            executable = (info.get("window_executable") or info.get("executable")) if isinstance(info, dict) else None
            if executable:
                current = self.sessions.get(str(game_id))
                candidate = GameSession(executable)
                if not current or current.exe_path != candidate.exe_path:
                    self.sessions[str(game_id)] = candidate

    def request_launch(self, game_id):
        """Thread-safe entry point for the launch shortcut / single-instance IPC."""
        if not self.closed:
            self.events.put((self._request_launch_main, (str(game_id),), None))

    def _request_launch_main(self, game_id):
        if self.launching_game == game_id:
            return
        if self.operation_busy:
            self.pending_launch = game_id
            self.status.set("正在完成当前操作，随后将启动所选游戏。")
            return
        self.launching_game = game_id
        self.game_id = game_id
        game = self.game_rows.get(game_id, {})
        self.native_health.select(game_id, supported=self._supports_native_panel(game) if game else True, restart=True)

        def launch():
            info = self.service.session_info(game_id) if hasattr(self.service, "session_info") else {}
            result = self.service.action(game_id, "launch")
            from .native_overlay import installed
            try:
                if game.get("panel_backend") == "electron":
                    from .electron_panel import enabled
                    native = enabled(self.service, game_id)
                else:
                    native = installed(self.service, game_id, verify=False)
            except Exception:
                # A failed runtime check must not change a successful game launch.
                native = True
            return {"info": info, "result": result, "native": native}

        def failed(error):
            self.launching_game = None
            self.status.set("游戏未能启动：" + brief(error, 140))
            self.native_health.launch_failed(game_id, error)
            if self.root.state() == "withdrawn":
                self.root.iconify()

        def launched(data):
            self.launching_game = None
            self._prepare_sessions({game_id: data.get("info", {})})
            session = self.sessions.get(game_id)
            self.return_session = None  # Set only when a hotkey captures a visible game.
            result = data.get("result")
            if session and isinstance(result, dict) and result.get("pid"):
                session.set_process(result["pid"])
            self._load_games(self.game_id)
            self.status.set(str(data.get("result", "游戏已启动。")))
            # The launch callback can precede the first game window. Don't
            # report it as a failed return, or reopen the workbench over it.
            native = data.get("native", False)
            self._hide_windows(keep_taskbar=native)
            if session and not native:
                session.restore_focus()

        self._submit(launch, launched, "正在启动游戏与双语面板…", mutation=True, failure=failed)

    def _add_root(self):
        path = filedialog.askdirectory(title="选择游戏目录或已有双语项目目录", parent=self.root)
        if path:
            self._submit(lambda: self.service.add_game_root(path), self._operation_done, "正在识别游戏并导入文本…", mutation=True)

    def _import_menu(self):
        window = tk.Toplevel(self.root)
        window.title("导入游戏文本")
        window.configure(bg=PANEL)
        window.resizable(False, False)
        window.transient(self.root)
        frame = ttk.Frame(window, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="选择已有中英文本的组织方式", style="Section.TLabel").pack(anchor="w", pady=(0, 12))
        ttk.Label(frame, text="导入后可检索与查词。游戏内显示仍需匹配的游戏适配器。", style="Muted.TLabel").pack(anchor="w", pady=(0, 16))
        ttk.Button(frame, text="单文件：双语 JSON / CSV / TSV", command=lambda: (window.destroy(), self._import_file())).pack(fill="x", pady=4)
        ttk.Button(frame, text="成对文件：英文 + 中文 JSON / locres", command=lambda: (window.destroy(), self._import_pair())).pack(fill="x", pady=4)
        window.grab_set()

    def _import_file(self):
        path = filedialog.askopenfilename(title="选择双语文本文件", filetypes=[("双语数据", "*.json *.csv *.tsv"), ("全部文件", "*.*")], parent=self.root)
        if not path:
            return
        title = simpledialog.askstring("文本库名称", "这份文本属于哪款游戏？", initialvalue=Path(path).stem, parent=self.root)
        if title and title.strip():
            self._submit(lambda: self.service.import_file(path, title.strip()), self._operation_done, "正在导入双语文本…", mutation=True)

    def _import_dictionary(self):
        path = filedialog.askopenfilename(title="选择 ECDICT 格式离线词典", filetypes=[("ECDICT 词典", "*.csv"), ("全部文件", "*.*")], parent=self.root)
        if path:
            self._submit(lambda: self.service.import_dictionary(path), self._operation_done,
                         "正在读取离线词典并补充游戏词义…", mutation=True)

    def _import_pair(self):
        files = [("语言文件", "*.json *.locres"), ("全部文件", "*.*")]
        english = filedialog.askopenfilename(title="第一步：选择英文语言文件", filetypes=files, parent=self.root)
        if not english:
            return
        chinese = filedialog.askopenfilename(title="第二步：选择中文语言文件", filetypes=files, parent=self.root)
        if not chinese:
            return
        title = simpledialog.askstring("文本库名称", "这份文本属于哪款游戏？", initialvalue=Path(english).parent.name, parent=self.root)
        if title and title.strip():
            self._submit(lambda: self.service.import_pair(english, chinese, title.strip()), self._operation_done, "正在按文本标识配对中英文…", mutation=True)

    def _export(self):
        if not self.game_id:
            self.status.set("请先在顶部选择要导出的游戏。")
            return
        game = self.game_rows[self.game_id]
        name = re.sub(r'[<>:"/\\|?*]', "_", game["title"])
        path = filedialog.asksaveasfilename(title="导出双语文本库", initialfile=name + "-双语文本.json", defaultextension=".json", filetypes=[("JSON 文本库", "*.json")], parent=self.root)
        if path:
            gid = self.game_id
            self._submit(lambda: self.service.export_game(gid, path), lambda count: messagebox.showinfo("导出完成", f"已导出 {count:,} 条文本。\n{path}", parent=self.root), "正在导出…", mutation=True)

    def _refresh(self):
        self._submit(self.service.refresh, self._operation_done, "正在更新已接入游戏的文本库…", mutation=True)

    def _hide_windows(self, *, keep_taskbar=False):
        # Keep note drafts, but never leave their window floating over the game.
        if self.notes_window and self.notes_window.window.winfo_exists():
            self.notes_window.window.withdraw()
        if self.hotkey.active and not keep_taskbar:
            self.root.withdraw()
        else:
            self.root.iconify()

    def hide(self):
        # Try the captured game while the hint can still be seen. A failed
        # attempt consumes the return target: the next Esc just dismisses us.
        session, self.return_session = self.return_session, None
        if session is not None and not session.restore_focus():
            self.status.set("未切回游戏。可用 Alt+Tab 手动切换，或再按 Esc 收起。")
            return
        self._hide_windows()

    def toggle(self):
        captured_game = None
        for game_id, session in self.sessions.items():
            if session.capture_foreground():
                # Ctrl+Shift+B is the explicit desktop fallback, even if
                # an installed F8 panel fails to load in this session.
                captured_game = game_id
                self.return_session = session
                break
        if self.root.state() in ("withdrawn", "iconic") or captured_game:
            if not captured_game:
                # Opening from the desktop must not later redirect focus to an old game.
                self.return_session = None
            if captured_game and captured_game != self.game_id and captured_game in self.game_ids:
                self.game_picker.current(self.game_ids.index(captured_game))
                self._game_selected()
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.search_box.focus_set()
        else:
            self.hide()

    def close(self):
        if self.closed:
            return
        if self.operation_busy:
            self.status.set("游戏文件操作仍在进行，完成后即可退出。")
            return
        if self.notes_window and self.notes_window.window.winfo_exists() and not self.notes_window.close():
            return
        self.closed = True
        self.native_health.close()
        self.hotkey.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def run(service, launch_game=None, on_ready=None):
    app = Desktop(service, hidden=launch_game is not None)
    if on_ready is not None:
        on_ready(app)
    if launch_game is not None:
        app.request_launch(launch_game)
    app.root.mainloop()
