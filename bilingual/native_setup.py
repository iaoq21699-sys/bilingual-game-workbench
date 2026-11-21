"""Per-game panel setup: readable status, local appearance preview, fixed actions."""
from pathlib import Path
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from . import native_overlay, themes
from .ui_design import BG, PANEL, INK, MUTED, ACCENT, LINE, WARN, FONT, tooltip, style_titlebar


def _mix(front, back, amount):
    return "#" + "".join(f"{round(int(front[i:i+2], 16) * amount + int(back[i:i+2], 16) * (1-amount)):02X}"
                          for i in (1, 3, 5))


def _round_box(canvas, left, top, right, bottom, radius=10, **options):
    radius = min(radius, (right-left)/2, (bottom-top)/2)
    points = (left+radius, top, right-radius, top, right, top, right, top+radius,
              right, bottom-radius, right, bottom, right-radius, bottom,
              left+radius, bottom, left, bottom, left, bottom-radius, left, top+radius, left, top)
    return canvas.create_polygon(points, smooth=True, splinesteps=20, **options)


def open_setup(desktop, game_id, report):
    desktop.status.set("接入条件检查完成。可在设置页查看安装状态和调整外观。")
    window = tk.Toplevel(desktop.root)
    window.title(report["title"] + " · 面板与外观")
    window.geometry("900x820")
    window.minsize(720, 560)
    window.configure(bg=BG)
    window.transient(desktop.root)
    window.after(200, lambda: style_titlebar(window))
    window.after(950, lambda: style_titlebar(window))
    electron = report.get("backend") == "electron"
    appearance = native_overlay.theme(desktop.service, game_id)
    opacity = tk.DoubleVar(window, value=appearance["Opacity"])
    renderer = tk.StringVar(window, value=report["renderer"])
    status = tk.StringVar(window, value="主题只用于这款游戏；截图取色在本机完成。")
    install_status = tk.StringVar(window)
    refreshing = False
    submitting = False

    # The action area never enters the scrolling region, including at 720x560.
    footer = ttk.Frame(window, style="Card.TFrame", padding=(22, 12, 22, 16))
    footer.pack(side="bottom", fill="x")
    tk.Frame(window, bg=LINE, height=1).pack(side="bottom", fill="x")
    footer_status = ttk.Label(footer, textvariable=install_status, style="Caption.TLabel", justify="left")
    footer_status.pack(fill="x", pady=(0, 10))
    actions = ttk.Frame(footer, style="Card.TFrame")
    actions.pack(fill="x")
    actions.columnconfigure(1, weight=1)

    scroll_area = ttk.Frame(window, style="Outer.TFrame")
    scroll_area.pack(fill="both", expand=True)
    viewport = tk.Canvas(scroll_area, bg=BG, highlightthickness=0, bd=0, yscrollincrement=20)
    scrollbar = ttk.Scrollbar(scroll_area, orient="vertical", command=viewport.yview)
    scrollbar.pack(side="right", fill="y")
    viewport.pack(side="left", fill="both", expand=True)
    viewport.configure(yscrollcommand=scrollbar.set)
    body = ttk.Frame(viewport, style="Outer.TFrame", padding=(22, 22, 22, 22))
    content = viewport.create_window((0, 0), window=body, anchor="nw")
    wrapped_labels = []

    def label(parent, text=None, variable=None, color=INK, size=10, bold=False, background=PANEL):
        value = tk.Label(parent, text=text, textvariable=variable, bg=background, fg=color,
                         font=(FONT, size, "bold" if bold else "normal"), anchor="w", justify="left", bd=0)
        wrapped_labels.append(value)
        return value

    def resize(event):
        viewport.itemconfigure(content, width=event.width)
        for widget in wrapped_labels:
            widget.configure(wraplength=max(220, event.width-92))
        footer_status.configure(wraplength=max(220, window.winfo_width()-48))

    body.bind("<Configure>", lambda _: viewport.configure(scrollregion=viewport.bbox("all")))
    viewport.bind("<Configure>", resize)

    def wheel(event):
        if event.widget.winfo_toplevel() != window or event.widget.winfo_class() == "TCombobox":
            return
        bounds = viewport.bbox("all")
        if bounds and bounds[3] > viewport.winfo_height():
            delta = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
            viewport.yview_scroll(delta * 2, "units")
            return "break"

    window.bind("<MouseWheel>", wheel)
    label(body, "游戏内阅读", color=ACCENT, size=9, bold=True, background=BG).pack(anchor="w")
    label(body, "面板与外观", size=22, bold=True, background=BG).pack(anchor="w", pady=(4, 7))
    label(body, report["title"] + "  ·  设置按游戏分别保存", color=MUTED, background=BG).pack(anchor="w", pady=(0, 20))

    def card():
        border = tk.Frame(body, bg=LINE, padx=1, pady=1)
        border.pack(fill="x", pady=(0, 16))
        frame = ttk.Frame(border, style="Card.TFrame", padding=18)
        frame.pack(fill="both", expand=True)
        return frame

    connection = card()
    status_header = ttk.Frame(connection, style="Card.TFrame")
    status_header.pack(fill="x")
    badge = ttk.Label(status_header, text="检查接入", style="Info.Badge.TLabel")
    badge.pack(side="left")
    refresh_button = ttk.Button(status_header, text="重新检查", style="Quiet.TButton", command=lambda: refresh())
    refresh_button.pack(side="right")
    connection_title = label(connection, "正在检查接入条件", size=15, bold=True)
    connection_title.pack(fill="x", pady=(12, 6))
    connection_detail = label(connection, "", color=MUTED)
    connection_detail.pack(fill="x")
    reasons = ttk.Frame(connection, style="Card.TFrame")
    reasons.pack(fill="x", pady=(8, 0))
    details = ttk.Frame(connection, style="Card.TFrame")
    details_open = tk.BooleanVar(window, value=False)

    def toggle_details():
        opened = not details_open.get()
        details_open.set(opened)
        details_toggle.configure(text="收起高级选项  −" if opened else "高级选项与检测详情  ＋")
        if opened:
            details.pack(fill="x", pady=(12, 0))
        else:
            details.pack_forget()

    connection_links = ttk.Frame(connection, style="Card.TFrame")
    connection_links.pack(fill="x", pady=(6, 0))
    details_toggle = ttk.Button(connection_links, text="高级选项与检测详情  ＋", style="Quiet.TButton", command=toggle_details)
    details_toggle.pack(side="left")

    def show_health():
        window.destroy()
        desktop._show_native_health(game_id)

    health_button = ttk.Button(connection_links, text="查看加载情况", style="Quiet.TButton", command=show_health)
    health_button.pack(side="right")
    ttk.Separator(details).pack(fill="x", pady=(0, 12))
    interface_row = ttk.Frame(details, style="Card.TFrame")
    interface_row.pack(fill="x")
    ttk.Label(interface_row, text="图形接口").pack(side="left", padx=(0, 12))
    renderer_labels = {"自动识别": "auto", "DirectX 10": "D3D10", "DirectX 11": "D3D11", "DirectX 12": "D3D12"}
    renderer_display = tk.StringVar(window, value=next((name for name, value in renderer_labels.items() if value == renderer.get()), "自动识别"))
    renderer_box = ttk.Combobox(interface_row, textvariable=renderer_display,
                               values=list(renderer_labels), state="readonly", width=17)
    renderer_box.pack(side="left")
    tooltip(renderer_box, "通常保留自动识别。手动选择只指定面板接入接口，不会切换游戏的图形模式。")
    if electron:
        interface_row.pack_forget()
    technical = label(details, "", color=MUTED, size=9)
    technical.pack(fill="x", pady=(10, 0))

    appearance_card = card()
    appearance_header = ttk.Frame(appearance_card, style="Card.TFrame")
    appearance_header.pack(fill="x")
    ttk.Label(appearance_header, text="阅读起来，也合你的眼缘", style="Section.TLabel").pack(side="left")
    preview_name = ttk.Label(appearance_header, text=appearance["Name"], style="Neutral.Badge.TLabel")
    preview_name.pack(side="right")
    label(appearance_card, "先选一套配色，或从游戏截图找到灵感。", color=MUTED).pack(fill="x", pady=(7, 14))
    preview = tk.Canvas(appearance_card, height=250, highlightthickness=1, highlightbackground=LINE, bd=0)
    preview.pack(fill="x")
    label(appearance_card, "双栏阅读示例  ·  透明效果会随游戏画面变化", color=MUTED, size=9).pack(fill="x", pady=(8, 16))
    palette = themes.presets()
    preset_row = ttk.Frame(appearance_card, style="Card.TFrame")
    preset_row.pack(fill="x", before=preview, pady=(0, 12))
    preset_buttons = {}
    palette_images = []
    swatches = {}

    def draw():
        width = max(500, preview.winfo_width())
        background = _mix(appearance["Background"], "#29403F", opacity.get())
        card_color = _mix(appearance["Panel"], background, 0.94)
        preview.configure(bg=background)
        preview.delete("all")
        preview.create_text(18, 18, text="双语阅读", anchor="nw", fill=appearance["Accent"], font=(FONT, 11, "bold"))
        preview.create_text(width-18, 20, text="F8  打开 / 收起", anchor="ne", fill=appearance["Muted"], font=(FONT, 9))
        split = max(170, int(width * 0.31))
        _round_box(preview, 14, 52, split, 234, fill=card_color, outline=appearance["Border"])
        _round_box(preview, 25, 64, split-11, 97, radius=7, fill=background, outline=appearance["Border"])
        preview.create_text(35, 79, text="搜索原句或单词…", anchor="w", fill=appearance["Muted"], font=(FONT, 9))
        _round_box(preview, 25, 111, split-11, 164, radius=7, fill=background, outline=appearance["Accent"])
        preview.create_text(36, 122, text="A new journey…", anchor="nw", fill=appearance["Text"], font=(FONT, 10), width=split-62)
        preview.create_text(36, 146, text="新的旅程…", anchor="w", fill=appearance["Muted"], font=(FONT, 9))
        preview.create_text(36, 190, text="Every word tells a story.", anchor="w", fill=appearance["Muted"], font=(FONT, 9), width=split-56)
        left = split+26
        right_width = max(200, width-left-22)
        preview.create_text(left, 63, text="A new journey begins.", anchor="nw", fill=appearance["Text"], font=(FONT, 16, "bold"), width=right_width)
        preview.create_text(left, 108, text="新的旅程，从这里开始。", anchor="nw", fill=appearance["Text"], font=(FONT, 12), width=right_width)
        preview.create_line(left, 151, width-19, 151, fill=appearance["Border"])
        preview.create_text(left, 167, text="journey", anchor="nw", fill=appearance["Accent"], font=(FONT, 11, "bold"))
        preview.create_text(left, 196, text="旅行；旅程", anchor="nw", fill=appearance["Muted"], font=(FONT, 10))
        for key, (chip, value_label) in swatches.items():
            chip.configure(bg=appearance[key])
            value_label.configure(text=appearance[key])
        for name, button in preset_buttons.items():
            selected = appearance.get("Source") == "preset:" + name
            button.configure(style="Accent.TButton" if selected else "TButton",
                             text=("✓  " if selected else "") + palette[name]["Name"])
        preview_name.configure(text=appearance["Name"] or "自定义")

    def apply(value):
        normalized = themes.normalize_theme(value)
        appearance.clear()
        appearance.update(normalized)
        opacity.set(appearance["Opacity"])
        draw()

    def preset(name):
        apply(dict(palette[name], Opacity=opacity.get()))
        status.set("已预览“" + appearance["Name"] + "”。满意后保存到这款游戏。")

    for column, (name, value) in enumerate(palette.items()):
        preset_row.columnconfigure(column, weight=1, uniform="presets")
        image = tk.PhotoImage(master=window, width=96, height=8)
        for index, key in enumerate(("Background", "Muted", "Accent")):
            image.put(value[key], to=(index*32, 0, (index+1)*32, 8))
        palette_images.append(image)
        button = ttk.Button(preset_row, text=value["Name"], image=image, compound="top", command=lambda name=name: preset(name))
        button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0 if column == 2 else 5))
        preset_buttons[name] = button
    window._native_setup_palette_images = palette_images

    def from_image():
        path = filedialog.askopenfilename(parent=window, title="选择游戏截图", filetypes=[("截图", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not path:
            return
        try:
            apply(dict(themes.theme_from_image(Path(path)), Opacity=opacity.get()))
            status.set("已从截图生成配色。截图仅在本机读取，配色不会随实时画面变化。")
        except Exception as exc:
            messagebox.showerror("截图配色", str(exc), parent=window)

    ttk.Button(appearance_card, text="从游戏截图配色…", style="Quiet.TButton", command=from_image).pack(anchor="w", pady=(10, 16))
    opacity_header = ttk.Frame(appearance_card, style="Card.TFrame")
    opacity_header.pack(fill="x")
    ttk.Label(opacity_header, text="背景不透明度").pack(side="left")
    percent = ttk.Label(opacity_header, text=f"{opacity.get():.0%}", style="Info.Badge.TLabel")
    percent.pack(side="right")
    slider = ttk.Scale(appearance_card, variable=opacity, from_=0.35, to=0.95)
    slider.pack(fill="x", pady=(8, 4))
    tooltip(slider, "数值越低，透出的游戏画面越多。这里的预览使用示例背景，实际阅读效果取决于游戏场景。")
    scale_captions = ttk.Frame(appearance_card, style="Card.TFrame")
    scale_captions.pack(fill="x")
    ttk.Label(scale_captions, text="更通透", style="Caption.TLabel").pack(side="left")
    ttk.Label(scale_captions, text="更清晰", style="Caption.TLabel").pack(side="right")

    def opacity_changed(*_):
        percent.configure(text=f"{opacity.get():.0%}")
        draw()

    opacity.trace_add("write", opacity_changed)
    fine_colors = ttk.Frame(appearance_card, style="Card.TFrame")
    colors_open = tk.BooleanVar(window, value=False)

    def toggle_colors():
        opened = not colors_open.get()
        colors_open.set(opened)
        colors_toggle.configure(text="收起颜色微调  −" if opened else "微调 6 种颜色  ＋")
        if opened:
            fine_colors.pack(fill="x", pady=(8, 12), before=appearance_note)
        else:
            fine_colors.pack_forget()

    colors_toggle = ttk.Button(appearance_card, text="微调 6 种颜色  ＋", style="Quiet.TButton", command=toggle_colors)
    colors_toggle.pack(anchor="w", pady=(12, 0))

    def custom(key):
        selected = colorchooser.askcolor(appearance[key], parent=window, title="调整这款游戏的颜色")[1]
        if selected:
            apply(dict(appearance, **{key: selected, "Name": "自定义", "Source": "manual", "Opacity": opacity.get()}))
            status.set("已更新预览。文字对比度会自动保护，实际透明效果仍受游戏画面影响。")

    for index, (key, name) in enumerate((("Background", "背景"), ("Panel", "卡片"), ("Text", "文字"),
                                        ("Muted", "次要文字"), ("Accent", "强调色"), ("Border", "边线"))):
        column = index % 3
        fine_colors.columnconfigure(column, weight=1, uniform="colors")
        item = ttk.Frame(fine_colors, style="Card.TFrame")
        item.grid(row=index//3, column=column, sticky="ew", padx=(0, 10), pady=(4, 6))
        button = ttk.Button(item, text=name, style="Quiet.TButton", command=lambda key=key: custom(key))
        button.pack(anchor="w")
        sample = ttk.Frame(item, style="Card.TFrame")
        sample.pack(anchor="w", padx=9, pady=(2, 0))
        chip = tk.Label(sample, width=2, height=1, bg=appearance[key], bd=0, cursor="hand2")
        chip.pack(side="left", padx=(0, 7))
        chip.bind("<Button-1>", lambda _, key=key: custom(key))
        value_label = ttk.Label(sample, text=appearance[key], style="Caption.TLabel")
        value_label.pack(side="left")
        swatches[key] = (chip, value_label)
    appearance_note = label(appearance_card, variable=status, color=MUTED, size=9)
    appearance_note.pack(fill="x", pady=(12, 0))
    launch_tip = ("启用后请从工作台启动游戏，按 F8 打开。工作台会报告本次加载情况。" if electron else
                  "安装后正常启动游戏，按 F8 打开。实际加载情况可在工作台查看。")
    label(body, launch_tip, color=MUTED, size=9, background=BG).pack(fill="x", pady=(0, 2))

    def commit(installing):
        nonlocal submitting
        if submitting or refreshing or desktop.operation_busy:
            return
        selected = dict(appearance, Opacity=opacity.get())
        selected_renderer = renderer.get()
        submitting = True
        def operation():
            native_overlay.save_preferences(desktop.service, game_id, selected, selected_renderer)
            return native_overlay.install(desktop.service, game_id) if installing else "已保存这款游戏的面板主题。"
        window.destroy()
        desktop._submit(operation, lambda result: desktop._operation_done(result, game_id), "正在保存面板设置…", mutation=True)

    ttk.Button(actions, text="关闭", style="Quiet.TButton", command=window.destroy).grid(row=0, column=0, sticky="w")
    save = ttk.Button(actions, text="保存主题", command=lambda: commit(False))
    save.grid(row=0, column=2, sticky="e", padx=(10, 8))
    install = ttk.Button(actions, text="保存并安装", style="Accent.TButton", command=lambda: commit(True))
    install.grid(row=0, column=3, sticky="e")

    def render_report(value):
        nonlocal report
        report = value
        for child in reasons.winfo_children():
            child.destroy()
        wrapped_labels[:] = [widget for widget in wrapped_labels if widget.winfo_exists()]
        blockers = report["blockers"]
        if report["running"]:
            headline, badge_text, badge_style = "先退出游戏，再保存设置", "游戏运行中", "Warning.Badge.TLabel"
        elif report["needs_update"]:
            headline, badge_text, badge_style = "更新面板后即可继续", "需要更新", "Warning.Badge.TLabel"
        elif blockers:
            headline, badge_text, badge_style = "完成下面的事项后即可安装", "暂不能安装", "Warning.Badge.TLabel"
        elif electron and report["installed"]:
            headline, badge_text, badge_style = "游戏内双语与面板已启用", "已启用", "Success.Badge.TLabel"
        elif report["installed"]:
            headline, badge_text, badge_style = "面板已经安装", "已安装", "Success.Badge.TLabel"
        elif electron:
            headline, badge_text, badge_style = "可以启用游戏内双语与面板", "检查通过", "Success.Badge.TLabel"
        else:
            headline, badge_text, badge_style = "可以安装游戏内面板", "检查通过", "Success.Badge.TLabel"
        badge.configure(text=badge_text, style=badge_style)
        connection_title.configure(text=headline)
        count = f"{report['count']:,} 条文本可供检索"
        description = "。从工作台启动时加载原位双语。F8 打开检索。" if electron else (
            "。调整主题后保存，下次启动生效。" if report["installed"] else "。先选好配色，再安装到这款游戏。")
        connection_detail.configure(text=count + description)
        for number, reason in enumerate(blockers, 1):
            text = str(reason).replace("请在上方选择", "请在高级选项中选择")
            item = label(reasons, f"{number}.  {text}", color=WARN)
            item.configure(wraplength=max(220, viewport.winfo_width()-92))
            item.pack(fill="x", pady=(3, 6))
        engine_detail = "渲染方式  Electron / Chromium 游戏页面" if electron else (
            "图形接口：" + (", ".join(report["apis"]) or "尚未识别") + "   ·   程序架构：" + report["architecture"]
            + "\n识别依据：" + (report.get("renderer_source") or "未提供"))
        technical.configure(text=engine_detail + "\n实际游戏程序  " + report["executable"] + "\n" + report["compatibility"])
        action_text = ("已启用" if report["installed"] else "保存并启用") if electron else (
            "已安装" if report["installed"] else "保存并安装")
        install.configure(state="normal" if report["ready"] and not report["installed"] else "disabled",
                          text=action_text)
        save.configure(state="disabled" if report["running"] or report["needs_update"] else "normal",
                       style="Accent.TButton" if report["installed"] else "TButton")
        health_button.configure(state="normal" if report["installed"] else "disabled")
        if blockers:
            install_status.set("暂不能安装 · " + " ".join(str(blockers[0]).split())[:120])
        elif electron:
            install_status.set("启用后从工作台启动游戏，按 F8 打开。")
        elif report["installed"]:
            install_status.set("主题将保存到这款游戏；已安装的面板不需要重复安装。")
        else:
            install_status.set("准备就绪 · 保存并安装后，启动游戏按 F8。")

    def refresh(_=None):
        nonlocal refreshing
        if refreshing:
            return
        refreshing = True
        refresh_button.configure(state="disabled", text="正在检查…")
        renderer_box.configure(state="disabled")
        save.configure(state="disabled")
        install.configure(state="disabled")
        chosen_renderer = renderer.get()
        def success(value):
            nonlocal refreshing
            refreshing = False
            if window.winfo_exists():
                refresh_button.configure(state="normal", text="重新检查")
                renderer_box.configure(state="readonly")
                render_report(value)
        def failure(error):
            nonlocal refreshing
            refreshing = False
            if window.winfo_exists():
                refresh_button.configure(state="normal", text="重新检查")
                renderer_box.configure(state="readonly")
                badge.configure(text="检查未完成", style="Warning.Badge.TLabel")
                connection_title.configure(text="暂时无法完成检查")
                connection_detail.configure(text=str(error))
                install_status.set("检查未完成，请查看上方说明后重试。")
                install.configure(state="disabled")
                save.configure(state="disabled")
        desktop._submit(lambda: native_overlay.inspect(desktop.service, game_id, chosen_renderer), success,
                        "正在检查面板接入条件…", failure=failure)

    def renderer_selected(_=None):
        renderer.set(renderer_labels[renderer_display.get()])
        refresh()

    def shortcut_save(_):
        if "disabled" not in save.state():
            commit(False)
        return "break"

    def close_shortcut(event):
        if event.widget.winfo_class() != "TCombobox":
            window.destroy()
            return "break"

    renderer_box.bind("<<ComboboxSelected>>", renderer_selected)
    preview.bind("<Configure>", lambda _: draw())
    window.bind("<Control-s>", shortcut_save)
    window.bind("<Escape>", close_shortcut)
    tooltip(save, "保存这款游戏的主题。快捷键 Ctrl+S。")
    render_report(report)  # The caller already obtained this report in its worker.
    draw()
    return window
