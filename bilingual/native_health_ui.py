"""Quiet per-game runtime checks; Tk is updated only by the desktop event queue."""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk

from .ui_design import BG, PANEL, SURFACE, INK, MUTED, ACCENT, LINE, WARN, FONT, style_titlebar


_STAGE_NAMES = {"游戏进程": "游戏运行", "插件加载": "面板组件", "图形挂钩": "画面连接",
                "绘制回调": "面板渲染", "键鼠输入": "键鼠操作", "语料读取": "游戏文本"}


def _plain(value):
    text = str(value or "")
    for original, label in _STAGE_NAMES.items():
        text = text.replace(original, label)
    return text.replace("语料", "游戏文本")


def _explanation(value):
    """Keep raw diagnostics available in the disclosure, outside the summary."""
    text = str(value or "").split("技术详情：", 1)[0].strip()
    return _plain(re.sub(r"（[a-z_]+）\s*$", "", text))


def _tone(state):
    if state in {"ready", "panel_drawn"}:
        return "Success", ACCENT
    if state in {"failed", "needs_update"}:
        return "Warning", WARN
    if state in {"starting", "waiting"}:
        return "Info", "#A5D5FB"
    return "Neutral", MUTED


def _short(value, limit=62):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


class NativeHealthCard(ttk.Frame):
    INTERVAL = 2000

    def __init__(self, desktop, parent):
        super().__init__(parent, style="Health.TFrame", padding=(14, 9))
        self.desktop = desktop
        self.root = desktop.root
        self.game_id = None
        self.supported = False
        self.generation = 0
        self.context = None
        self.timer = None
        self.future = None
        self.closed = False
        self.detail_window = None
        self.detail_text = None
        self.detail_content = None
        self.detail_canvas = None
        self.detail_layout_after = None
        self.technical_open = False
        self.step_rows = []
        self.action_rows = []
        self.wrap_labels = []
        self.result = {}
        self.columnconfigure(1, weight=1)
        self.badge = ttk.Label(self, text="待选择", style="Neutral.Badge.TLabel", anchor="center", width=7)
        self.badge.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 12), pady=(2, 0))
        controls = ttk.Frame(self, style="Health.TFrame")
        controls.grid(row=0, column=2, rowspan=2, sticky="e", padx=(14, 0))
        self.details_button = ttk.Button(controls, text="检查详情", style="Quiet.TButton", command=self.show_details)
        self.details_button.pack(side="left")
        self.retry_button = ttk.Button(controls, text="重试", style="Quiet.TButton", command=self.retry)
        self.retry_button.pack(side="left", padx=(4, 0))
        self.title = tk.StringVar()
        self.detail = tk.StringVar()
        self.title_label = ttk.Label(self, textvariable=self.title, style="HealthTitle.TLabel", width=1, anchor="w")
        self.title_label.grid(row=0, column=1, sticky="ew")
        progress = self.progress = ttk.Frame(self, style="Health.TFrame")
        progress.grid(row=1, column=1, sticky="ew", pady=(5, 0))
        self.progress_labels = []
        for label in ("游戏", "画面", "检索"):
            item = tk.Label(progress, text="○ " + label, bg=SURFACE, fg=MUTED,
                            font=(FONT, 8), padx=6, pady=2)
            item.pack(side="left", padx=(0, 5))
            self.progress_labels.append(item)
        ttk.Label(progress, textvariable=self.detail, style="HealthDetail.TLabel", width=1,
                  anchor="w").pack(side="left", fill="x", expand=True, padx=(5, 0))
        self._display(self._idle())

    @staticmethod
    def _idle():
        return dict(state="idle", title="选择游戏后自动检查面板加载",
                    detail="这里显示游戏实际运行中的加载结果，安装完成不代表已经加载。",
                    steps=[], actions=[], pid=None, elapsed=0, terminal=False)

    def select(self, game_id, supported=True, restart=False):
        """Called only on the Tk thread. Each selection owns its own monitor."""
        game_id = str(game_id) if game_id is not None else None
        if not restart and game_id == self.game_id and supported == self.supported:
            return
        self.generation += 1
        self.game_id, self.supported = game_id, bool(supported)
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        if self.future is not None:
            self.future.cancel()
            self.future = None
        self.context = {"game_id": game_id, "monitor": None, "busy": False, "launch_error": None}
        self.detail_content = None
        if game_id is None:
            self._display(self._idle())
        elif not supported:
            self._display(dict(state="not_installed", title="这份文本库尚未接入游戏内面板",
                               detail="从「游戏内面板与主题」检查接入条件。", steps=[], actions=[], terminal=False))
        else:
            self._display(dict(state="waiting", title="正在检查游戏内面板",
                               detail="会自动跟踪这款游戏的启动，检查过程不会切换游戏焦点。",
                               steps=[], actions=[], terminal=False))
            self._tick()

    def retry(self):
        if self.game_id and not self.closed:
            self.select(self.game_id, self.supported, restart=True)

    def launch_failed(self, game_id, error):
        if str(game_id) != self.game_id or self.context is None:
            return
        self.context["launch_error"] = str(error)
        self._display(self._launch_failure(error))

    @staticmethod
    def _launch_failure(error):
        return dict(state="failed", title="游戏未能启动", detail=str(error), pid=None,
                    steps=[dict(label="启动游戏", state="failed", detail=str(error))],
                    actions=["检查游戏目录和启动程序，再从「游戏 → 启动游戏」重试。"], terminal=True)

    def _tick(self):
        self.timer = None
        if self.closed or self.desktop.closed or not self.game_id or not self.supported:
            return
        context, generation = self.context, self.generation
        if not context["busy"]:
            context["busy"] = True
            try:
                self.future = self.desktop.executor.submit(self._poll, context)
            except RuntimeError:
                context["busy"] = False
                return

            def completed(future):
                if self.closed or self.desktop.closed or future.cancelled():
                    return
                try:
                    result = future.result()
                except Exception as error:
                    result = dict(state="failed", title="运行检查暂未完成", detail=str(error),
                                  steps=[dict(label="读取运行信息", state="failed", detail=str(error))],
                                  actions=["点击「重试」重新检查；详细原因见上方。"], terminal=True)
                self.desktop.events.put((self._accept, (generation, context, result), None))

            self.future.add_done_callback(completed)
        self.timer = self.root.after(self.INTERVAL, self._tick)

    def _poll(self, context):
        # This function and monitor.poll() always run in the shared executor.
        if context["monitor"] is None:
            if self.desktop.game_rows.get(context["game_id"], {}).get("panel_backend") == "electron":
                from .electron_panel import HealthMonitor
                context["monitor"] = HealthMonitor(self.desktop.service, context["game_id"])
                return context["monitor"].poll()
            from .native_overlay import installed
            if not installed(self.desktop.service, context["game_id"], verify=False):
                return dict(state="not_installed", title="尚未安装游戏内面板",
                            detail="完成「游戏内面板与主题」接入后，这里会自动跟踪实际加载。",
                            steps=[dict(label="面板接入", state="waiting", detail="这款游戏尚未安装内置面板。")],
                            actions=["打开「游戏内面板与主题」，检查并安装。"], terminal=False)
            from .native_health import NativeHealthMonitor
            context["monitor"] = NativeHealthMonitor(self.desktop.service, context["game_id"], startup_timeout=90)
        # A terminal result is a conclusion for this stage, not a reason to stop:
        # a late startup or a subsequent game process may still become ready.
        return context["monitor"].poll()

    def _accept(self, generation, context, result):
        if self.closed or generation != self.generation or context is not self.context:
            return
        context["busy"] = False
        if context["launch_error"] and result.get("state") == "waiting" and not result.get("pid"):
            result = self._launch_failure(context["launch_error"])
        elif result.get("pid"):
            context["launch_error"] = None
        self._display(result)

    def _display(self, result):
        self.result = dict(result)
        state = result.get("state", "waiting")
        title = _plain(result.get("title") or "正在检查游戏内面板")
        description = _explanation(result.get("detail"))
        badge = {"idle": "待选择", "not_installed": "未接入", "waiting": "等待中", "starting": "加载中",
                 "failed": "需处理", "needs_update": "待更新", "exited": "已退出"}.get(state, "检查中")
        if state in {"ready", "panel_drawn"}:
            shown = result.get("panel_drawn") or state == "panel_drawn"
            title = "已检测到面板显示" if shown else "游戏内面板已就绪"
            badge = "已显示" if shown else "已就绪"
            description = "F8 开合面板  ·  Esc 收起"
        elif state == "idle":
            title, description = "选择一款游戏，查看内置面板状态", "启动后自动检查"
        elif state == "not_installed":
            title, description = "这款游戏尚未接入内置面板", "接入后自动检查加载"
        elif state == "waiting" and not result.get("pid") and result.get("backend") != "electron":
            badge = "检查中" if "正在" in title else "待启动"
            description = "启动游戏后自动检查" if badge == "待启动" else "正在确认接入状态"
        elif state == "starting":
            description = "正在连接，请稍候"
        elif state == "exited":
            description = "下次启动会重新检查"
        elif state in {"failed", "needs_update"}:
            description = "查看详情中的处理建议"
        self.title.set(_short(title, 40))
        self.detail.set(description)
        if result.get("pid") or state in {"failed", "needs_update", "starting"}:
            self.progress.grid()
        else:
            self.progress.grid_remove()
        tone, _ = _tone(state)
        self.badge.configure(text=badge, style=tone + ".Badge.TLabel")
        steps = {step.get("label"): step.get("state") for step in result.get("steps") or []}
        groups = (("游戏", ("游戏进程",)), ("画面", ("游戏页面",)), ("检索", ("按键与语料",))) if result.get("backend") == "electron" else (
            ("游戏", ("游戏进程",)), ("画面", ("插件加载", "图形挂钩", "绘制回调")), ("检索", ("键鼠输入", "语料读取")))
        for item, (label, keys) in zip(self.progress_labels, groups):
            values = [steps.get(key) for key in keys]
            complete = all(value == "ready" for value in values)
            failed = any(value in {"failed", "needs_update"} for value in values)
            symbol, color = ("✓", ACCENT) if complete else ("!", WARN) if failed else ("○", MUTED)
            item.configure(text=symbol + " " + label, fg=color)
        self.details_button.configure(state="normal" if self.game_id else "disabled")
        self.retry_button.configure(state="normal" if self.game_id and self.supported else "disabled")
        self._update_details()

    def show_details(self):
        """User-initiated only; automatic status changes never map or focus windows."""
        if self.closed or not self.game_id:
            return
        if self.detail_window is not None and self.detail_window.winfo_exists():
            self.detail_window.deiconify()
            self.detail_window.lift()
            self.detail_window.focus_set()
            return
        window = self.detail_window = tk.Toplevel(self.root)
        window.title("游戏内面板 · 运行检查")
        window.geometry("680x620")
        window.minsize(530, 380)
        window.configure(bg=BG)
        window.transient(self.root)
        window.after(200, lambda: style_titlebar(window))
        window.after(950, lambda: style_titlebar(window))
        heading = tk.Frame(window, bg=BG, padx=22, pady=18)
        heading.pack(fill="x")
        tk.Label(heading, text="运行检查", bg=BG, fg=INK, font=(FONT, 18, "bold")).pack(anchor="w")
        self.detail_game = tk.Label(heading, text="", bg=BG, fg=MUTED, font=(FONT, 10), anchor="w")
        self.detail_game.pack(anchor="w", fill="x", pady=(5, 0))
        footer = ttk.Frame(window, style="Outer.TFrame", padding=(22, 12))
        footer.pack(side="bottom", fill="x")
        ttk.Button(footer, text="重新检查", command=self.retry).pack(side="left")
        ttk.Button(footer, text="完成", style="Accent.TButton", command=self._close_details).pack(side="right")
        area = ttk.Frame(window, style="Outer.TFrame", padding=(22, 0, 12, 0))
        area.pack(fill="both", expand=True)
        canvas = self.detail_canvas = tk.Canvas(area, bg=BG, highlightthickness=0, borderwidth=0, width=1)
        scrollbar = ttk.Scrollbar(area, command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=(0, 8))
        canvas.configure(yscrollcommand=scrollbar.set)
        self.detail_body = tk.Frame(canvas, bg=BG)
        self.detail_body_id = canvas.create_window((0, 0), window=self.detail_body, anchor="nw")
        self.detail_body.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", self._detail_resize)
        self.wrap_labels = []
        self.step_rows, self.action_rows = [], []

        summary = tk.Frame(self.detail_body, bg=PANEL, highlightthickness=1, highlightbackground=LINE, padx=16, pady=15)
        summary.pack(fill="x", pady=(0, 12))
        summary_top = tk.Frame(summary, bg=PANEL)
        summary_top.pack(fill="x")
        self.detail_badge = ttk.Label(summary_top, style="Neutral.Badge.TLabel")
        self.detail_badge.pack(side="right", padx=(12, 0))
        self.detail_title = tk.Label(summary_top, bg=PANEL, fg=INK, font=(FONT, 13, "bold"), anchor="w", justify="left", width=1)
        self.detail_title.pack(side="left", fill="x", expand=True)
        self.detail_summary = tk.Label(summary, bg=PANEL, fg=MUTED, font=(FONT, 10), anchor="w", justify="left", wraplength=500)
        self.detail_summary.pack(fill="x", pady=(9, 0))
        self.wrap_labels.append((self.detail_summary, 36))
        self.action_card = tk.Frame(self.detail_body, bg=PANEL, highlightthickness=1, highlightbackground=LINE, padx=16, pady=14)
        tk.Label(self.action_card, text="建议先这样做", bg=PANEL, fg=WARN, font=(FONT, 11, "bold")).pack(anchor="w", pady=(0, 10))
        self.actions_body = tk.Frame(self.action_card, bg=PANEL)
        self.actions_body.pack(fill="x")

        self.stages_card = tk.Frame(self.detail_body, bg=PANEL, highlightthickness=1, highlightbackground=LINE, padx=16, pady=14)
        self.stages_card.pack(fill="x", pady=(0, 12))
        stage_heading = tk.Frame(self.stages_card, bg=PANEL)
        stage_heading.pack(fill="x", pady=(0, 10))
        tk.Label(stage_heading, text="连接进度", bg=PANEL, fg=INK, font=(FONT, 11, "bold")).pack(side="left")
        self.stage_count = tk.Label(stage_heading, bg=PANEL, fg=MUTED, font=(FONT, 9))
        self.stage_count.pack(side="right")
        self.steps_body = tk.Frame(self.stages_card, bg=PANEL)
        self.steps_body.pack(fill="x")
        self.steps_empty = tk.Label(self.steps_body, text="启动游戏后，会逐项显示连接进度。", bg=PANEL, fg=MUTED,
                                    font=(FONT, 10), justify="left", anchor="w", wraplength=460)
        self.wrap_labels.append((self.steps_empty, 36))

        disclosure = ttk.Frame(self.detail_body, style="Outer.TFrame")
        disclosure.pack(fill="x", pady=(0, 8))
        self.technical_button = ttk.Button(disclosure, text="查看技术信息  ▸", style="Header.TButton", command=self._toggle_technical)
        self.technical_button.pack(anchor="w")
        self.technical_frame = tk.Frame(self.detail_body, bg=SURFACE, highlightthickness=1, highlightbackground=LINE)
        text = self.detail_text = tk.Text(self.technical_frame, wrap="word", relief="flat", bg=SURFACE, fg=MUTED,
                                        font=(FONT, 9), padx=12, pady=10, height=10, spacing3=4,
                                        insertbackground=INK, selectbackground=LINE, highlightthickness=0, takefocus=True)
        text.pack(fill="both", expand=True)
        text.configure(state="disabled")
        self.detail_note = tk.Label(self.detail_body, text="● 自动更新中 · 关闭此页不会中断游戏", bg=BG, fg=MUTED,
                                    font=(FONT, 9), anchor="w", justify="left", wraplength=460)
        self.detail_note.pack(fill="x", pady=(6, 12))
        self.wrap_labels.append((self.detail_note, 8))
        self.technical_open = False
        self.detail_content = None
        self._update_details()
        window.protocol("WM_DELETE_WINDOW", self._close_details)
        window.bind("<Escape>", lambda event: (self._close_details(), "break")[-1])
        window.bind("<MouseWheel>", self._detail_scroll)

    def _close_details(self):
        if self.detail_layout_after is not None:
            self.root.after_cancel(self.detail_layout_after)
            self.detail_layout_after = None
        if self.detail_window is not None and self.detail_window.winfo_exists():
            self.detail_window.destroy()
        self.detail_window = self.detail_canvas = self.detail_text = None

    def _detail_resize(self, event):
        self.detail_canvas.itemconfigure(self.detail_body_id, width=event.width)
        for widget, inset in self.wrap_labels:
            if widget.winfo_exists():
                widget.configure(wraplength=max(210, event.width - inset))

    def _detail_scroll(self, event):
        if event.widget is self.detail_text:
            return None
        if event.delta:
            self.detail_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _toggle_technical(self):
        self.technical_open = not self.technical_open
        self.technical_button.configure(text="收起技术信息  ▾" if self.technical_open else "查看技术信息  ▸")
        if self.technical_open:
            self.technical_frame.pack(fill="x", pady=(0, 8), before=self.detail_note)
        else:
            self.technical_frame.pack_forget()

    @staticmethod
    def _step_description(step, overall_state):
        label, ready = step.get("label"), step.get("state") == "ready"
        if step.get("state") in {"failed", "needs_update"}:
            return _explanation(step.get("detail")) or "请按上方建议处理后重新检查。"
        if label == "语料读取" and ready:
            return _plain(step.get("detail") or "游戏文本已可读取。")
        if label == "面板显示":
            return "已收到游戏内实际显示记录。" if ready else (
                "尚未打开，回到游戏按 F8 即可。" if overall_state in {"ready", "panel_drawn"} else "面板就绪后，按 F8 打开。")
        descriptions = {"游戏进程": ("已找到这次启动的游戏。", "启动游戏后继续检查。"),
                        "安装与配置": ("安装文件与文本库路径正常。", "接入配置需要检查。"),
                        "插件加载": ("组件已载入游戏。", "等待面板组件载入。"),
                        "图形挂钩": ("已连接到游戏画面。", "等待游戏画面连接。"),
                        "绘制回调": ("游戏内渲染已确认。", "进入主菜单后继续检查。"),
                        "键鼠输入": ("键盘与鼠标已连接。", "等待键盘与鼠标连接。"),
                        "语料读取": ("游戏文本已可读取。", "等待本机文本库就绪。")}
        return descriptions.get(label, ("检查已完成。", "等待检查结果。"))[0 if ready else 1]

    def _update_details(self):
        if self.detail_window is None or not self.detail_window.winfo_exists():
            return
        result = self.result
        game = self.desktop.game_rows.get(self.game_id, {})
        steps = result.get("steps") or []
        actions = result.get("actions") or []
        state = result.get("state", "waiting")
        # A heartbeat should not reflow the page or disturb text selection.
        content = (self.game_id, game.get("title"), state, result.get("title"),
                   result.get("detail"), result.get("pid"), result.get("panel_drawn"),
                   tuple(str(action) for action in actions),
                   tuple((step.get("label"), step.get("state"), step.get("detail")) for step in steps))
        if content == self.detail_content:
            return
        top = max(0, self.detail_canvas.canvasy(0)) if self.detail_content is not None else 0
        self.detail_content = content
        self.detail_game.configure(text=_short(game.get("title", "所选游戏"), 56))
        self.detail_title.configure(text=self.title.get())
        self.detail_badge.configure(text=self.badge.cget("text"), style=self.badge.cget("style"))
        summary = _explanation(result.get("detail"))
        if state in {"ready", "panel_drawn"}:
            summary = ("已确认游戏内面板显示。回到游戏，按 F8 开合面板，按 Esc 收起。"
                       if result.get("panel_drawn") or state == "panel_drawn" else
                       "加载与连接均已完成。回到游戏按 F8 打开面板；实际显示后，这里会自动确认。")
        self.detail_summary.configure(text=summary or "启动游戏后，这里会自动显示连接结果。")
        self.detail_note.configure(text="● 自动更新中 · 关闭此页不会中断游戏" if self.supported else
                                   "接入内置面板后，将自动检查这款游戏的运行状态。")
        width = max(460, self.detail_canvas.winfo_width())
        if actions:
            self.action_card.pack(fill="x", pady=(0, 12), before=self.stages_card)
        else:
            self.action_card.pack_forget()
        for index, action in enumerate(actions):
            if index >= len(self.action_rows):
                row = tk.Frame(self.actions_body, bg=PANEL)
                number = tk.Label(row, text=str(index + 1).zfill(2), bg=PANEL, fg=WARN,
                                  font=(FONT, 9, "bold"), width=3, anchor="w")
                number.pack(side="left", anchor="n", padx=(0, 8))
                label = tk.Label(row, bg=PANEL, fg=INK, font=(FONT, 10), anchor="w",
                                 justify="left", wraplength=width - 76)
                label.pack(side="left", fill="x", expand=True)
                self.action_rows.append((row, label))
                self.wrap_labels.append((label, 76))
            row, label = self.action_rows[index]
            label.configure(text=_plain(action))
            row.pack(fill="x", pady=(0, 7))
        for row, _ in self.action_rows[len(actions):]:
            row.pack_forget()

        core_steps = [step for step in steps if step.get("label") != "面板显示"]
        completed = sum(step.get("state") == "ready" for step in core_steps)
        self.stage_count.configure(text=f"已通过 {completed} / {len(core_steps)} 项" if core_steps else "等待检查")
        labels = {"ready": "已完成", "starting": "进行中", "waiting": "等待中",
                  "failed": "需处理", "needs_update": "待更新", "exited": "已退出"}
        self.steps_empty.pack_forget()
        for index, step in enumerate(steps):
            if index >= len(self.step_rows):
                row = tk.Frame(self.steps_body, bg=PANEL)
                row.columnconfigure(1, weight=1)
                icon = tk.Label(row, bg=PANEL, fg=MUTED, font=(FONT, 12), width=2, anchor="w")
                icon.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 7), pady=(1, 0))
                title = tk.Label(row, bg=PANEL, fg=INK, font=(FONT, 10, "bold"), anchor="w")
                title.grid(row=0, column=1, sticky="ew")
                badge = ttk.Label(row, style="Neutral.Badge.TLabel", anchor="center", width=6)
                badge.grid(row=0, column=2, rowspan=2, sticky="ne", padx=(10, 0))
                detail = tk.Label(row, bg=PANEL, fg=MUTED, font=(FONT, 9), anchor="w",
                                  justify="left", wraplength=width - 160)
                detail.grid(row=1, column=1, sticky="ew", pady=(3, 0))
                self.step_rows.append((row, icon, title, badge, detail))
                self.wrap_labels.append((detail, 160))
            row, icon, title, badge, detail = self.step_rows[index]
            step_state = step.get("state", "waiting")
            tone, color = _tone(step_state)
            if step_state == "waiting":
                tone, color = "Neutral", MUTED
            icon.configure(text="✓" if step_state == "ready" else "!" if step_state in {"failed", "needs_update"} else "○", fg=color)
            title.configure(text=_plain(step.get("label", "检查阶段")))
            status = labels.get(step_state, "等待中")
            if step.get("label") == "面板显示" and step_state == "waiting" and state in {"ready", "panel_drawn"}:
                status = "待打开"
            badge.configure(text=status, style=tone + ".Badge.TLabel")
            detail.configure(text=self._step_description(step, state))
            row.pack(fill="x", pady=(5, 8))
        for row, *_ in self.step_rows[len(steps):]:
            row.pack_forget()
        if not steps:
            self.steps_empty.pack(fill="x", pady=(0, 4))

        lines = [str(game.get("title", "所选游戏")), f"游戏标识：{self.game_id}",
                 f"进程：{result.get('pid') or '尚未运行'}", "", str(result.get("title", "")),
                 str(result.get("detail", "")), "", "原始检查信息"]
        for step in steps:
            lines.append(f"{step.get('label', '检查阶段')} · {labels.get(step.get('state'), '等待中')}")
            lines.append(str(step.get("detail", "")))
            lines.append("")
        position = self.detail_text.yview()[0]
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", "\n".join(lines))
        self.detail_text.configure(state="disabled")
        self.detail_text.yview_moveto(position)
        if self.detail_layout_after is not None:
            self.root.after_cancel(self.detail_layout_after)

        def restore_scroll():
            self.detail_layout_after = None
            if self.detail_canvas is not None and self.detail_canvas.winfo_exists():
                bounds = self.detail_canvas.bbox("all")
                if bounds:
                    self.detail_canvas.configure(scrollregion=bounds)
                    self.detail_canvas.yview_moveto(top / max(1, bounds[3]))

        self.detail_layout_after = self.root.after_idle(restore_scroll)

    def close(self):
        self.closed = True
        self.generation += 1
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        if self.future is not None:
            self.future.cancel()
