from datetime import datetime
import re
import sqlite3
import tkinter as tk
from tkinter import ttk

from .core import display_text
from .notes import Notebook
from .ui import ResultCards
from .ui_design import (BG, FONT, MUTED, ENGLISH_FONT, PAPER, PAPER_INK, PAPER_MUTED,
                        PAPER_ACCENT, PAPER_MARK, PAPER_LINE, PAPER_INPUT, book_mark, style_titlebar)


KINDS = {"sentence": "整句", "word": "单词", "phrase": "词组"}


def snapshot_text(raw):
    return re.sub(r"<(?:/?[A-Za-z][^<>\n]{0,88}|/)>", "", display_text(raw))


def saved_time(stamp, fmt):
    return datetime.fromtimestamp(stamp / 1000).strftime(fmt)


class NotesWindow:
    def __init__(self, desktop):
        self.book = Notebook(desktop.service.data_dir)
        self.window = win = tk.Toplevel(desktop.root)
        win.title("复习笔记 · 双语游戏工作台")
        win.configure(bg=BG)
        width = min(1060, win.winfo_screenwidth() - 60)
        height = min(650, win.winfo_screenheight() - 90)
        win.geometry(f"{width}x{height}")
        win.minsize(min(740, width), min(480, height))
        self.current = None
        self.recalling = False
        self.revealed = False
        self.note_ids = []
        self.game_id = desktop.game_id
        self.games = {key: value["title"] for key, value in desktop.game_rows.items()}
        self.game_ids = []
        self.feedback = tk.StringVar(master=win)
        self.count = tk.StringVar(master=win)

        bar = ttk.Frame(win, style="Outer.TFrame", padding=(22, 16))
        bar.pack(fill="x")
        book_mark(bar).pack(side="left", padx=(0, 10))
        ttk.Label(bar, text="复习笔记", style="Title.TLabel", font=(FONT, 17, "bold")).pack(side="left")
        ttk.Button(bar, text="刷新", style="Header.TButton", command=self.reload).pack(side="right")
        self.picker = ttk.Combobox(bar, state="readonly", width=21, style="Filter.TCombobox")
        self.picker.pack(side="right", padx=(8, 10))
        self.picker.bind("<<ComboboxSelected>>", self._filter)
        tk.Label(bar, text="游戏", bg=BG, fg=MUTED, font=(FONT, 10)).pack(side="right")

        review = ttk.Frame(win, style="Inset.TFrame", padding=(14, 9))
        review.pack(fill="x", padx=16, pady=(0, 10))
        review.columnconfigure(0, weight=1)
        review.columnconfigure(1, weight=0)
        self.review_hint = tk.StringVar(master=win)
        self.review_position = tk.StringVar(master=win)
        self.review_title = ttk.Label(review, text="收藏浏览", style="ReviewTitle.TLabel")
        self.review_title.grid(row=0, column=0, sticky="w")
        ttk.Label(review, textvariable=self.review_hint, style="ReviewHint.TLabel",
                  wraplength=360).grid(row=1, column=0, sticky="w", pady=(3, 0))
        review_actions = ttk.Frame(review, style="Inset.TFrame")
        review_actions.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Label(review_actions, textvariable=self.review_position,
                  style="ReviewHint.TLabel").pack(side="left", padx=(0, 9))
        self.previous_button = ttk.Button(review_actions, width=3, text="←", command=lambda: self._step(-1))
        self.next_button = ttk.Button(review_actions, width=3, text="→", command=lambda: self._step(1))
        self.review_toggle = ttk.Button(review_actions, text="开始回想", style="Primary.TButton", command=self._toggle_review)
        self.review_toggle.pack(side="right")

        body = ttk.Frame(win, style="Outer.TFrame", padding=(16, 0, 16, 14))
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=3, uniform="pages")
        body.columnconfigure(1, weight=7, uniform="pages")
        left = ttk.Frame(body, padding=(12, 14))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        heading = ttk.Frame(left)
        heading.pack(fill="x", pady=(0, 12))
        ttk.Label(heading, text="收藏列表", font=(FONT, 13, "bold")).pack(side="left")
        ttk.Label(heading, textvariable=self.count, style="Caption.TLabel").pack(side="right")
        self.list = ResultCards(left, self._selected, compact=True)
        self.list.pack(fill="both", expand=True)

        right = tk.Frame(body, bg=PAPER, padx=24, pady=18)
        right.grid(row=0, column=1, sticky="nsew")
        answer = tk.Frame(right, bg=PAPER)
        answer.pack(fill="x", pady=(0, 9))
        self.answer_label = tk.Label(answer, text="原句 · 译文 · 释义", bg=PAPER, fg=PAPER_ACCENT,
                                     font=(FONT, 10, "bold"))
        self.answer_label.pack(side="left")
        self.reveal_button = ttk.Button(answer, text="显示译文与释义", style="Paper.TButton",
                                        command=self._toggle_answer)
        content = tk.Frame(right, bg=PAPER)
        content.pack(fill="both", expand=True)
        self.detail = tk.Text(content, wrap="word", width=1, height=5, bg=PAPER, fg=PAPER_INK,
                              selectbackground=PAPER_ACCENT, selectforeground="#FFFFFF", exportselection=False,
                              borderwidth=0, highlightthickness=0, font=(FONT, 12), cursor="xterm")
        scroll = ttk.Scrollbar(content, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.detail.pack(side="left", fill="both", expand=True)
        for name, options in {
            "meta": dict(font=(FONT, 10), foreground=PAPER_MUTED, spacing3=18),
            "english": dict(font=(ENGLISH_FONT, 23), spacing2=4, spacing3=9),
            "target": dict(foreground=PAPER_ACCENT, background=PAPER_MARK, underline=True),
            "translation": dict(foreground=PAPER_MUTED, font=(FONT, 13), spacing2=3, spacing3=18),
            "label": dict(foreground=PAPER_ACCENT, font=(FONT, 11, "bold"), spacing1=8, spacing3=5),
            "meaning": dict(foreground=PAPER_INK, font=(FONT, 13), spacing2=3, spacing3=8),
            "source": dict(foreground=PAPER_MUTED, font=(FONT, 10), spacing3=4),
        }.items():
            self.detail.tag_configure(name, **options)
        self.detail.tag_raise("sel")
        self.remark_separator = tk.Frame(right, bg=PAPER_LINE, height=1)
        self.remark_separator.pack(fill="x", pady=(10, 12))
        remark_header = tk.Frame(right, bg=PAPER)
        remark_header.pack(fill="x", pady=(0, 7))
        tk.Label(remark_header, text="个人备注", bg=PAPER, fg=PAPER_INK, font=(FONT, 12, "bold")).pack(side="left")
        tk.Label(remark_header, text="Ctrl + Enter 保存", bg=PAPER, fg=PAPER_MUTED, font=("Segoe UI", 9)).pack(side="right")
        self.remark_header = remark_header
        self.remark = tk.Text(right, wrap="word", height=3, width=1, font=(FONT, 11), bg=PAPER_INPUT,
                              fg=PAPER_INK, insertbackground=PAPER_ACCENT, relief="flat", padx=10, pady=8,
                              highlightthickness=1, highlightbackground=PAPER_LINE, highlightcolor=PAPER_ACCENT, undo=True)
        self.remark.pack(fill="x")
        actions = self.remark_actions = tk.Frame(right, bg=PAPER)
        actions.pack(fill="x", pady=(8, 0))
        self.save_button = ttk.Button(actions, text="保存备注", style="Paper.TButton", command=self.save)
        self.save_button.pack(side="right")
        tk.Label(actions, textvariable=self.feedback, bg=PAPER, fg=PAPER_ACCENT, font=(FONT, 10),
                 anchor="w", wraplength=250).pack(side="left")
        self.remark.bind("<Control-Return>", self._save_shortcut)
        self.remark.bind("<<Modified>>", self._remark_changed)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda _: self.close())
        win.after(150, lambda: style_titlebar(win))
        self.reload()

    def _save_shortcut(self, _):
        self.save()
        return "break"

    def _remark_changed(self, _):
        if not self.remark.edit_modified():
            return
        self.remark.edit_modified(False)
        if self.current:
            changed = self.remark.get("1.0", "end-1c") != self.current["personal_note"]
            self.save_button.configure(state="normal" if changed else "disabled")
            self.feedback.set("备注有修改" if changed else "")

    def save(self):
        if self.current is None:
            return True
        text = self.remark.get("1.0", "end-1c")
        if text != self.current["personal_note"]:
            try:
                self.book.save_remark(self.current["id"], text)
            except (OSError, ValueError, sqlite3.Error) as exc:
                self.feedback.set(f"备注未保存，请重试。{exc}")
                return False
            self.current["personal_note"] = text
            self.save_button.configure(state="disabled")
            self.feedback.set("备注已保存")
        return True

    def _filter(self, _):
        if self.save():
            self.game_id = self.game_ids[self.picker.current()]
            self.reload()
        else:
            self.picker.current(self.game_ids.index(self.game_id))

    def reload(self):
        if not self.save():
            return
        try:
            games, rows = self.book.listing(self.game_id)
        except (OSError, sqlite3.Error) as exc:
            self.feedback.set(f"读取失败，请刷新重试。{exc}")
            return
        self.games.update((g["game_id"], g["game_title"]) for g in games)
        self.game_ids = [None, *sorted(self.games, key=self.games.get)]
        self.picker["values"] = ["全部游戏", *(self.games[key] for key in self.game_ids[1:])]
        self.picker.current(self.game_ids.index(self.game_id) if self.game_id in self.game_ids else 0)
        old_id = str(self.current["id"]) if self.current else ""
        self.current = None
        cards = []
        for row in rows:
            original = " ".join(snapshot_text(row["en"]).split())
            label = row["target"] if row["kind"] != "sentence" else original
            context = row["game_title"] if not self.game_id else saved_time(row["saved_at_ms"], "%m/%d")
            cards.append(dict(id=row["id"], category=f"{KINDS[row['kind']]}  ·  {context}",
                              en=label or "（缺少英文）", zh=snapshot_text(row["zh"]) or "暂无译文"))
        self.note_ids = [str(row["id"]) for row in rows]
        self.list.set_items(cards)
        self.count.set(f"{len(rows)} 条")
        if rows:
            selected = old_id if any(str(row["id"]) == old_id for row in rows) else rows[0]["id"]
            self.list.select(selected)
        else:
            self._display(None)

    def _selected(self, selected):
        if self.current and str(self.current["id"]) == selected:
            return
        if not self.save():
            if self.current:
                self.list.select(self.current["id"], notify=False)
            return
        try:
            row = self.book.get(int(selected))
        except (OSError, sqlite3.Error) as exc:
            if self.current:
                self.list.select(self.current["id"], notify=False)
            self.feedback.set(f"读取失败，请刷新重试。{exc}")
            return
        self._display(dict(row) if row else None)

    def _toggle_review(self):
        if (not self.current and not self.recalling) or not self.save():
            return
        self.recalling = not self.recalling
        self.revealed = False
        self.list.set_secondary_visible(not self.recalling)
        self._update_review()
        self._render_detail(self.current)

    def _toggle_answer(self):
        if self.recalling and self.current:
            if self.revealed and not self.save():
                return
            self.revealed = not self.revealed
            self._update_review()
            self._render_detail(self.current)

    def _step(self, step):
        index = self._index()
        if index is not None and 0 <= index + step < len(self.note_ids):
            self.list.select(self.note_ids[index + step])

    def _index(self):
        return self.note_ids.index(str(self.current["id"])) if self.current and str(self.current["id"]) in self.note_ids else None

    def _update_review(self):
        index = self._index()
        showing = not self.recalling or self.revealed
        self.review_title.configure(text="主动回想" if self.recalling else "收藏浏览")
        self.review_hint.set("先用英文原句回想意思，再核对译文与释义。" if self.recalling else
                             "重读游戏里的原句、词组与个人备注。")
        self.review_position.set(f"{index + 1} / {len(self.note_ids)}" if index is not None else "")
        self.review_toggle.configure(text="结束回想" if self.recalling else "开始回想",
                                     state="normal" if self.recalling or index is not None else "disabled")
        if self.recalling:
            self.previous_button.pack(side="left", padx=(0, 4))
            self.next_button.pack(side="left", padx=(0, 10))
            self.previous_button.configure(state="normal" if index is not None and index > 0 else "disabled")
            self.next_button.configure(state="normal" if index is not None and index + 1 < len(self.note_ids) else "disabled")
        else:
            self.previous_button.pack_forget()
            self.next_button.pack_forget()
        self.answer_label.configure(text="先读英文原句" if not showing else "原句 · 译文 · 释义")
        if self.recalling and index is not None:
            self.reveal_button.configure(text="再次隐藏" if self.revealed else "显示译文与释义")
            self.reveal_button.pack(side="right")
        else:
            self.reveal_button.pack_forget()
        if showing and self.current:
            self.remark_separator.pack(fill="x", pady=(10, 12))
            self.remark_header.pack(fill="x", pady=(0, 7))
            self.remark.pack(fill="x")
            self.remark_actions.pack(fill="x", pady=(8, 0))
        else:
            for widget in (self.remark_separator, self.remark_header, self.remark, self.remark_actions):
                widget.pack_forget()

    def _display(self, row):
        self.current = row
        self.revealed = False
        self.feedback.set("")
        self.remark.configure(state="normal")
        self.remark.delete("1.0", "end")
        self.remark.insert("1.0", row["personal_note"] if row else "")
        self.remark.edit_reset()
        self.remark.edit_modified(False)
        self.save_button.configure(state="disabled")
        self._update_review()
        self._render_detail(row)

    def _render_detail(self, row):
        text = self.detail
        text.configure(state="normal")
        text.delete("1.0", "end")
        if row:
            text.insert("end", f"{row['game_title']}   ·   {saved_time(row['saved_at_ms'], '%Y-%m-%d %H:%M')}\n", "meta")
            english = snapshot_text(row["en"])
            start = text.index("end-1c")
            text.insert("end", (english or "未收录英文原句") + "\n", "english")
            if row["target"] and row["kind"] != "sentence":
                words = row["target"].replace("’", "'").split()
                pattern = r"(?<![\w'])" + r"\s+".join(map(re.escape, words)) + r"(?![\w'])"
                match = re.search(pattern, english.replace("’", "'"), re.IGNORECASE)
                if match:
                    text.tag_add("target", f"{start}+{match.start()}c", f"{start}+{match.end()}c")
            if not self.recalling or self.revealed:
                text.insert("end", (snapshot_text(row["zh"]) or "未收录中文译文") + "\n", "translation")
                origin, _, source = row["meaning_source"].partition(" · ")
                heading = origin or "释义"
                if row["target"]:
                    heading += "   /   " + row["target"]
                text.insert("end", heading + "\n", "label")
                text.insert("end", (row["meaning"] or "这条原句没有附带释义") + "\n", "meaning")
                if source:
                    text.insert("end", source, "source")
            else:
                text.insert("end", "试着说出这句话的意思，再点「显示译文与释义」。", "source")
        else:
            text.insert("end", "还没有收藏的笔记\n", "english")
            text.insert("end", "在游戏里按 F8 找到原句，点「记到笔记」即可收藏。", "source")
        text.configure(state="disabled")
        text.yview_moveto(0)

    def close(self):
        if self.save():
            self.window.destroy()
            return True
        return False
